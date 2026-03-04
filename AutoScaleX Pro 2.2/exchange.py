"""
Интеграция с биржей BingX (Spot API).

CircuitBreaker: защита от каскадных ошибок (5 неудач → OPEN, 60с таймаут).
RateLimiter: 90 req/min на экземпляр.
Retry: 3 попытки с экспоненциальной задержкой.

BingXSpotAsync: асинхронная обёртка — вызовы выполняются в thread pool,
чтобы не блокировать event loop при большом числе пользователей.
"""
import asyncio
import time
import uuid
import hmac
import hashlib
import logging
import requests
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Optional, Dict, List

log = logging.getLogger("exchange")
error_log = logging.getLogger("api_errors")

try:
    import config as _config
    _CB_NOTIFY_COOLDOWN = getattr(_config, "ERROR_NOTIFY_COOLDOWN", 300)
except ImportError:
    _CB_NOTIFY_COOLDOWN = 300

try:
    from error_handling import is_non_critical_api_error, is_telegram_critical, get_user_friendly_message
except ImportError:
    is_non_critical_api_error = lambda m: False
    is_telegram_critical = lambda m: True
    get_user_friendly_message = lambda e, c="": None


class CircuitState(Enum):
    """Состояния circuit breaker"""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker для защиты от постоянных ошибок API"""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60, success_threshold: int = 2):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.success_threshold = success_threshold
        self.failure_count = 0
        self.success_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = None
        self.lock = threading.Lock()
    
    def call(self, func, *args, **kwargs):
        """Выполнить функцию с защитой circuit breaker"""
        with self.lock:
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time > self.timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                else:
                    raise RuntimeError("Circuit breaker is OPEN. API requests are temporarily blocked.")
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            error_msg = str(e)
            self._on_failure(error_msg)
            raise
    
    def _on_success(self):
        """Обработка успешного запроса"""
        with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    log.info("Circuit breaker closed after successful recovery")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0
    
    def _on_failure(self, error_msg: str = ""):
        """Обработка ошибки"""
        if "Incorrect apiKey" in error_msg or "api key" in error_msg.lower():
            log.warning(f"API key error (not counted as circuit breaker failure): {error_msg}")
            return
        
        with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                log.warning("Circuit breaker opened after failure in half-open state")
            elif self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                log.error(f"Circuit breaker opened after {self.failure_count} failures")
    
    def reset(self):
        """Сбросить circuit breaker"""
        with self.lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = None
            log.info("Circuit breaker reset")

# Параметры rate limit (на экземпляр)
RATE_LIMIT_DEFAULT = 90
RATE_INTERVAL = 60


class RateLimiter:
    """Rate limiter на экземпляр — каждый пользователь (API ключ) имеет свой лимит"""
    
    def __init__(self, rate_limit: int = RATE_LIMIT_DEFAULT, interval: int = RATE_INTERVAL):
        self.tokens = rate_limit
        self.rate_limit = rate_limit
        self.interval = interval
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._refill_thread = threading.Thread(target=self._refill_loop, daemon=True)
        self._refill_thread.start()
    
    def _refill_loop(self):
        while not self._stop_event.wait(timeout=self.interval):
            with self.lock:
                self.tokens = self.rate_limit
    
    def wait(self):
        """Ждать один токен. Lock держим только на время проверки/уменьшения, не во время sleep — иначе _refill не сможет пополнить (deadlock)."""
        while True:
            with self.lock:
                if self.tokens > 0:
                    self.tokens -= 1
                    return
            time.sleep(0.05)  # вне lock: refill может взять lock и пополнить tokens
    
    def stop(self):
        self._stop_event.set()


class BingXSpot:
    """Клиент для работы с BingX Spot API"""
    
    def __init__(self, key, secret, telegram_notifier=None):
        self.key = key
        self.secret = secret
        self.sess = requests.Session()
        self._symbol_info = {}
        self._cache = {}
        self._cache_time = 3
        self.base_url = "https://open-api.bingx.com"
        self.circuit_breaker = CircuitBreaker()
        self.telegram_notifier = telegram_notifier
        self.rate_limiter = RateLimiter()
        self._last_cb_notify_time = 0.0

    def close(self):
        """Корректное завершение: остановить rate limiter"""
        try:
            self.rate_limiter.stop()
        except Exception:
            pass

    def __del__(self):
        """Остановить rate limiter при уничтожении экземпляра"""
        try:
            if hasattr(self, 'rate_limiter') and self.rate_limiter:
                self.rate_limiter.stop()
        except Exception:
            pass

    def _sign(self, payload: dict) -> dict:
        payload = {k: str(v) for k, v in sorted(payload.items()) if v is not None}
        query = "&".join([f"{k}={v}" for k, v in payload.items()])
        payload["signature"] = hmac.new(
            self.secret.encode(), 
            query.encode(), 
            hashlib.sha256
        ).hexdigest()
        return payload

    def _request(self, method, endpoint, params=None):
        """Выполнить запрос с circuit breaker и экспоненциальной задержкой"""
        
        def _make_request():
            self.rate_limiter.wait()
            url = self.base_url + endpoint
            headers = {"X-BX-APIKEY": self.key}
            
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    params_copy = params.copy() if params else {}
                    params_copy["timestamp"] = int(time.time() * 1000)
                    params_copy["recvWindow"] = 5000
                    params_copy = self._sign(params_copy)
                    method_upper = method.upper()
                    if method_upper == "GET":
                        r = self.sess.get(url, params=params_copy, headers=headers, timeout=10)
                    elif method_upper == "DELETE":
                        r = self.sess.delete(url, params=params_copy, headers=headers, timeout=10)
                    else:
                        r = self.sess.post(url, data=params_copy, headers=headers, timeout=10)
                    r.raise_for_status()
                    data = r.json()
                    if data.get("code") != 0:
                        raise RuntimeError(data.get("msg"))
                    # Адаптивная пауза по заголовкам BingX (лимит по эндпоинту, сброс через Expire)
                    try:
                        remain_str = r.headers.get("X-RateLimit-Requests-Remain")
                        expire_str = r.headers.get("X-RateLimit-Requests-Expire")
                        if remain_str is not None and expire_str is not None:
                            remain = int(remain_str)
                            expire = int(expire_str)
                            # BingX может отдавать Expire в мс; при значении > 120 трактуем как миллисекунды
                            if expire > 120:
                                expire = expire // 1000
                            if remain < 5 and expire > 0:
                                wait = min(expire + 0.5, 60.0)  # не более 60 с, чтобы не блокировать бота
                                log.info("BingX rate limit window low (remain=%s), waiting %.1fs", remain, wait)
                                time.sleep(wait)
                    except (ValueError, TypeError):
                        pass
                    return data.get("data", data)
                except Exception as e:
                    error_msg = str(e)
                    is_api_key_error = "Incorrect apiKey" in error_msg or "api key" in error_msg.lower()
                    if is_api_key_error:
                        error_log.warning("API key rejected (no retry)")
                        if self.telegram_notifier:
                            try:
                                self.telegram_notifier(
                                    "❌ Ошибка API ключа!\n\n"
                                    "Проверьте правильность API ключа и секрета.\n"
                                    "Убедитесь, что ключ имеет разрешение 'Spot Trading'.\n\n"
                                    "Используйте команду /set_api для обновления ключей."
                                )
                            except Exception:
                                pass
                        raise RuntimeError(error_msg)
                    error_type = type(e).__name__
                    is_timeout_error = (
                        "ReadTimeout" in error_type or
                        "ReadTimeoutError" in error_type or
                        "ConnectionTimeout" in error_type or
                        "timeout" in error_msg.lower() or
                        "timed out" in error_msg.lower()
                    )
                    is_rate_limit = (
                        "rate limit" in error_msg.lower()
                        or "too many requests" in error_msg.lower()
                        or "frequency limit" in error_msg.lower()  # BingX code 100410
                    )
                    is_non_critical_error = is_non_critical_api_error(error_msg)
                    # Rate limit ретраим с длинной паузой, не бросаем сразу
                    if is_non_critical_error and not is_rate_limit:
                        error_log.debug("API (non-critical)")
                        raise RuntimeError(error_msg)
                    if is_rate_limit:
                        error_log.warning(f"API rate limited (attempt {attempt}/{max_attempts})")
                    elif is_timeout_error:
                        error_log.warning(f"API timeout (attempt {attempt}/{max_attempts})")
                    else:
                        error_log.error(f"API failed (attempt {attempt}/{max_attempts}), error type: {type(e).__name__}")
                    if attempt < max_attempts:
                        if is_rate_limit:
                            delay = 18 * attempt  # 18, 36, 54 сек — даём бирже время снять лимит (масштаб 100+ юзеров)
                            error_log.info(f"Waiting {delay}s before retry (rate limit)...")
                        else:
                            delay = 5 * attempt if is_timeout_error else 2 ** attempt
                            if is_timeout_error:
                                error_log.info(f"Waiting {delay}s before retry due to timeout...")
                        time.sleep(delay)
                    else:
                        if is_timeout_error:
                            error_msg_full = (
                                f"⚠️ Превышено время ожидания ответа от API BingX после {max_attempts} попыток.\n\n"
                                f"Возможные причины:\n"
                                f"• Проблемы с интернет-соединением\n"
                                f"• Медленная сеть\n"
                                f"• Временные проблемы на стороне биржи BingX\n\n"
                                f"Бот продолжит работу после восстановления соединения."
                            )
                        elif is_rate_limit:
                            error_msg_full = (
                                f"API BingX: превышен лимит запросов (rate limit) после {max_attempts} попыток. "
                                f"Повторите действие через минуту."
                            )
                        else:
                            error_msg_full = f"BingX API request failed after {max_attempts} attempts"
                        
                        is_non_critical = is_non_critical_api_error(error_msg_full) or is_rate_limit
                        send_to_telegram = self.telegram_notifier and is_telegram_critical(error_msg_full)
                        if send_to_telegram:
                            try:
                                friendly = get_user_friendly_message(e, "API")
                                self.telegram_notifier(friendly or f"🚨 Критическая ошибка API: {error_msg_full}")
                            except Exception:
                                pass
                        if is_timeout_error:
                            error_log.warning(error_msg_full)
                        elif is_non_critical:
                            error_log.debug("API non-critical")
                        
                        if is_timeout_error:
                            return None
                        raise RuntimeError(error_msg_full)
        
        try:
            return self.circuit_breaker.call(_make_request)
        except Exception as e:
            if self.telegram_notifier and self.circuit_breaker.state == CircuitState.OPEN:
                now = time.time()
                if now - self._last_cb_notify_time >= _CB_NOTIFY_COOLDOWN:
                    self._last_cb_notify_time = now
                    try:
                        self.telegram_notifier("⚠️ Circuit breaker открыт. API временно недоступен.")
                    except Exception:
                        pass
            raise

    def symbol_info(self, symbol: str):
        """Получить информацию о символе"""
        if symbol not in self._symbol_info:
            raw = self._request("GET", "/openApi/spot/v1/common/symbols")
            if raw is None:
                error_log.warning(f"API недоступен, используем дефолтные значения для {symbol}")
                return {
                    "stepSize": Decimal("0.000001"),
                    "minQty": Decimal("0.000001"),
                    "minNotional": Decimal("0"),
                    "tickSize": Decimal("0.01"),
                    "status": "UNKNOWN"
                }
            for item in raw.get("symbols", []):
                if item.get("symbol") == symbol:
                    lot = next((f for f in item.get("filters", []) if f.get("filterType") == "LOT_SIZE"), {})
                    pricef = next((f for f in item.get("filters", []) if f.get("filterType") == "PRICE_FILTER"), {})
                    minn = next((f for f in item.get("filters", []) if f.get("filterType") == "MIN_NOTIONAL"), {})
                    status_raw = item.get("status", 0)
                    if status_raw == 1 or status_raw == "1" or str(status_raw) == "1" or status_raw == "TRADING":
                        status = "TRADING"
                    elif status_raw == 0 or status_raw == "0" or str(status_raw) == "0" or status_raw == "BREAK":
                        status = "BREAK"
                    elif isinstance(status_raw, str) and status_raw.upper() == "TRADING":
                        status = "TRADING"
                    elif isinstance(status_raw, str):
                        status = status_raw.upper()
                    else:
                        log.warning(f"Unknown status format for {symbol}: {status_raw} (type: {type(status_raw)})")
                        status = "UNKNOWN"
                    
                    self._symbol_info[symbol] = {
                        "stepSize": Decimal(lot.get("stepSize", "0.000001")),
                        "minQty": Decimal(lot.get("minQty", "0.000001")),
                        "minNotional": Decimal(minn.get("minNotional", "0")),
                        "tickSize": Decimal(pricef.get("tickSize", "0.01")),
                        "status": status,
                        "baseAsset": item.get("baseAsset", ""),
                        "quoteAsset": item.get("quoteAsset", "")
                    }
                    log.debug(f"Symbol {symbol} info loaded: status={status} (raw={status_raw})")
                    break
            else:
                log.error(f"Symbol {symbol} not found in API response.")
                raise RuntimeError(f"Symbol {symbol} not found")
        return self._symbol_info[symbol]
    
    def is_symbol_trading(self, symbol: str) -> bool:
        """Проверить, доступен ли символ для торговли"""
        try:
            info = self.symbol_info(symbol)
            status = info.get("status", "")
            if status == "TRADING" or (isinstance(status, str) and status.upper() == "TRADING"):
                return True
            if status == 1 or status == "1" or str(status) == "1":
                log.warning(f"Symbol {symbol} has non-normalized status: {status}.")
                return True
            log.debug(f"Symbol {symbol} is not trading. Status: {status}")
            return False
        except RuntimeError:
            log.warning(f"Symbol {symbol} not found in API, but allowing trading")
            return True
        except Exception as e:
            log.error(f"Error checking symbol trading status for {symbol}: {type(e).__name__}")
            return True

    def price(self, symbol: str) -> Decimal:
        """Получить текущую цену"""
        key = ("price", symbol)
        if key not in self._cache or time.time() - self._cache[key][1] > 3:
            data = self._request("GET", "/openApi/spot/v1/ticker/24hr", {"symbol": symbol})
            if data is None:
                if key in self._cache:
                    error_log.warning(f"API недоступен, используем кэшированную цену для {symbol}")
                    return self._cache[key][0]
                raise RuntimeError(f"API недоступен и нет кэшированной цены для {symbol}")
            if not data or not data[0].get("lastPrice"):
                raise RuntimeError("Empty ticker response")
            self._cache[key] = (Decimal(data[0]["lastPrice"]), time.time())
        return self._cache[key][0]

    def balance(self, asset: str) -> Decimal:
        """Получить баланс актива (free + locked)"""
        key = ("balance", asset)
        if key not in self._cache or time.time() - self._cache[key][1] > 3:
            data = self._request("GET", "/openApi/spot/v1/account/balance")
            if data is None:
                if key in self._cache:
                    error_log.warning(f"API недоступен, используем кэшированный баланс для {asset}")
                    return self._cache[key][0]
                error_log.warning(f"API недоступен и нет кэшированного баланса для {asset}, возвращаем 0")
                return Decimal("0")
            free = Decimal(next((b["free"] for b in data["balances"] if b["asset"] == asset), "0"))
            locked = Decimal(next((b["locked"] for b in data["balances"] if b["asset"] == asset), "0"))
            self._cache[key] = (free + locked, time.time())
        return self._cache[key][0]
    
    def available_balance(self, asset: str) -> Decimal:
        """Получить доступный баланс актива (только free, без locked)"""
        key = ("available_balance", asset)
        if key not in self._cache or time.time() - self._cache[key][1] > 3:
            data = self._request("GET", "/openApi/spot/v1/account/balance")
            if data is None:
                if key in self._cache:
                    error_log.warning(f"API недоступен, используем кэшированный доступный баланс для {asset}")
                    return self._cache[key][0]
                error_log.warning(f"API недоступен и нет кэшированного доступного баланса для {asset}, возвращаем 0")
                return Decimal("0")
            free = Decimal(next((b["free"] for b in data["balances"] if b["asset"] == asset), "0"))
            self._cache[key] = (free, time.time())
        return self._cache[key][0]
    
    def invalidate_balance_cache(self, asset: str = None):
        """Инвалидировать кеш баланса для указанного актива или всех активов"""
        if asset:
            keys_to_remove = [k for k in self._cache.keys()
                              if isinstance(k, tuple) and len(k) == 2
                              and k[0] in ("balance", "available_balance") and k[1] == asset]
            for key in keys_to_remove:
                if key in self._cache:
                    del self._cache[key]
        else:
            keys_to_remove = [k for k in self._cache.keys() if isinstance(k, tuple) and len(k) == 2 and (k[0] == "balance" or k[0] == "available_balance")]
            for key in keys_to_remove:
                if key in self._cache:
                    del self._cache[key]

    def open_orders(self, symbol: str):
        """Получить список открытых ордеров"""
        data = self._request("GET", "/openApi/spot/v1/trade/openOrders", {"symbol": symbol})
        if data is None:
            error_log.warning(f"API недоступен, возвращаем пустой список ордеров для {symbol}")
            return []
        return data.get("orders", [])

    def cancel_order(self, symbol: str, order_id: str):
        """Отменить конкретный ордер по ID"""
        try:
            return self._request("GET", "/openApi/spot/v1/trade/cancel", {
                "symbol": symbol,
                "orderId": order_id
            })
        except Exception as e:
            error_msg = str(e).lower()
            if "order not exist" in error_msg or "order does not exist" in error_msg:
                log.debug("Order %s already gone – skip", order_id)
                return None
            else:
                log.warning("Cancel order %s failed: %s", order_id, type(e).__name__)
                raise
    
    def cancel_all(self, symbol: str):
        """Отменить все открытые ордера"""
        orders = self.open_orders(symbol)
        for o in orders:
            side = o.get("side", "").upper()
            type_ = o.get("type", "").upper()
            if not side or not type_ or side not in {"BUY", "SELL"} or type_ not in {"LIMIT", "MARKET"}:
                log.warning("Skip cancel – bad side/type: %s", o)
                continue
            try:
                self.cancel_order(symbol, o["orderId"])
                time.sleep(0.5)
            except Exception as e:
                error_msg = str(e).lower()
                if "order not exist" in error_msg or "order does not exist" in error_msg:
                    log.debug("Order %s already gone – skip", o["orderId"])
                else:
                    log.warning("Cancel %s failed: %s", o["orderId"], type(e).__name__)

    def validate_order(self, symbol: str, side: str, qty: Decimal, price: Decimal, order_type: str = "LIMIT") -> Dict:
        """Валидация параметров ордера перед размещением"""
        result = {
            'valid': True,
            'errors': [],
            'warnings': [],
            'adjusted_qty': qty,
            'adjusted_price': price
        }
        try:
            if not self.is_symbol_trading(symbol):
                try:
                    info = self.symbol_info(symbol)
                    status = info.get('status', 'UNKNOWN')
                except Exception:
                    status = 'ERROR'
                result['valid'] = False
                result['errors'].append(f"Символ {symbol} недоступен для торговли (статус: {status})")
                return result
            
            info = self.symbol_info(symbol)
            step = info["stepSize"]
            tick = info["tickSize"]
            min_qty = info["minQty"]
            min_notional = info["minNotional"]
            
            if price <= 0:
                result['valid'] = False
                result['errors'].append(f"Цена должна быть больше 0, получено: {price}")
                return result
            
            adjusted_price = (price // tick) * tick
            if adjusted_price != price:
                result['warnings'].append(f"Цена округлена: {price} → {adjusted_price}")
            result['adjusted_price'] = adjusted_price
            
            if side.upper() == "BUY":
                adjusted_qty = (qty // step) * step
            else:
                adjusted_qty = ((qty / step).quantize(Decimal('1'), ROUND_UP)) * step
            
            if adjusted_qty != qty:
                result['warnings'].append(f"Количество округлено: {qty} → {adjusted_qty}")
            result['adjusted_qty'] = adjusted_qty
            
            if adjusted_qty < min_qty:
                result['valid'] = False
                result['errors'].append(f"Количество {adjusted_qty} меньше минимального {min_qty}")
            
            notional = adjusted_qty * adjusted_price
            if min_notional > 0 and notional < min_notional:
                result['valid'] = False
                result['errors'].append(f"Номинал {notional} меньше минимального {min_notional}")
            
            if side.upper() == "BUY":
                try:
                    quote_balance = self.balance(symbol.split("-")[1] if "-" in symbol else "USDT")
                    if quote_balance < notional:
                        result['warnings'].append(f"Недостаточно баланса: требуется {notional}, доступно {quote_balance}")
                except Exception as e:
                    log.debug(f"Could not check balance for validation: {e}")
            
            if side.upper() == "SELL":
                try:
                    base_balance = self.balance(symbol.split("-")[0] if "-" in symbol else "BTC")
                    if base_balance < adjusted_qty:
                        result['warnings'].append(f"Недостаточно базовой валюты: требуется {adjusted_qty}, доступно {base_balance}")
                except Exception as e:
                    log.debug(f"Could not check base balance for validation: {e}")
        except Exception as e:
            result['valid'] = False
            result['errors'].append(f"Ошибка валидации: {str(e)}")
            log.error(f"Error validating order: {e}")
        return result
    
    def place_limit(self, symbol: str, side: str, qty: Decimal, price: Decimal, delay: float = 0.2, validate: bool = True):
        """Разместить лимитный ордер"""
        if validate:
            validation = self.validate_order(symbol, side, qty, price, "LIMIT")
            if validation['warnings']:
                for warning in validation['warnings']:
                    log.warning(f"Order validation warning: {warning}")
            if not validation['valid']:
                error_msg = "; ".join(validation['errors'])
                log.error(f"Order validation failed: {error_msg}")
                raise ValueError(f"Ордер не прошел валидацию: {error_msg}")
            qty = validation['adjusted_qty']
            price = validation['adjusted_price']
        
        info = self.symbol_info(symbol)
        step = info["stepSize"]
        tick = info["tickSize"]
        min_qty = info["minQty"]
        min_not = info["minNotional"]

        if price <= 0:
            log.warning("Skip %s – price <= 0", side)
            return None

        if side.upper() == "BUY":
            qty = (qty // step) * step
        else:
            qty = ((qty / step).quantize(Decimal('1'), ROUND_UP)) * step
        price = (price // tick) * tick

        if qty <= 0:
            log.warning("Quantity rounded to 0 – skipping %s order", side)
            return None

        side = side.upper()
        type_ = "LIMIT"
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Bad side: {side}")

        log.debug(">>> %s %s %s %s %s", symbol, side, type_, qty, price)

        cid = uuid.uuid4().hex[:32]
        payload = dict(
            newClientOrderId=cid,
            symbol=symbol,
            side=side,
            type=type_,
            timeInForce="GTC",
            quantity=str(qty),
            price=str(price)
        )

        res = self._request("POST", "/openApi/spot/v1/trade/order", payload)
        if delay:
            time.sleep(delay)
        return res

    def place_market(self, symbol: str, side: str, qty: Decimal, quote_order_qty: Decimal = None):
        """Разместить рыночный ордер"""
        side = side.upper()
        type_ = "MARKET"
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Bad side: {side}")

        payload = dict(symbol=symbol, side=side, type=type_)
        if quote_order_qty and quote_order_qty > 0 and side == "BUY":
            payload["quoteOrderQty"] = str(quote_order_qty)
            log.debug(f"Market BUY order with quoteOrderQty: {quote_order_qty}")
        elif qty and qty > 0:
            payload["quantity"] = str(qty)
            log.debug(f"Market {side} order with quantity: {qty}")
        else:
            log.debug("Skip %s – qty <= 0 and quote_order_qty not provided", side)
            return None

        return self._request("POST", "/openApi/spot/v1/trade/order", payload)
    
    def get_order(self, symbol: str, order_id: str):
        """Получить информацию об ордере"""
        return self._request("GET", "/openApi/spot/v1/trade/query", {
            "symbol": symbol,
            "orderId": order_id
        })
    
    def get_referrals_from_api(self, page: int = 1, page_size: int = 50):
        """Получить список рефералов через Agent API"""
        try:
            endpoints_to_try = [
                "/openApi/agent/partner/invitee/list",
                "/openApi/agent/api/v1/partner/invitee/list",
                "/openApi/api/v1/agent/partner/invitee/list"
            ]
            for endpoint in endpoints_to_try:
                try:
                    data = self._request("GET", endpoint, {"page": page, "pageSize": page_size})
                    if data is not None and ("invitees" in data or "data" in data):
                        invitees = data.get("invitees") or data.get("data", {}).get("invitees", [])
                        return {"total": data.get("total", len(invitees)), "page": data.get("page", page), "pageSize": data.get("pageSize", page_size), "invitees": invitees}
                except Exception as e:
                    log.debug(f"Endpoint {endpoint} failed: {type(e).__name__}")
                    continue
            log.warning("None of the Agent API endpoints worked.")
            return None
        except Exception as e:
            log.error(f"Error getting referrals from API: {type(e).__name__}")
            return None
    
    def get_referral_commissions(self, start_time: int = None, end_time: int = None, page: int = 1, page_size: int = 50):
        """Получить комиссии рефералов через Agent API"""
        try:
            params = {"page": page, "pageSize": page_size}
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time
            endpoints_to_try = [
                "/openApi/agent/partner/invitee/commission",
                "/openApi/agent/api/v1/partner/invitee/commission",
                "/openApi/api/v1/agent/partner/invitee/commission"
            ]
            for endpoint in endpoints_to_try:
                try:
                    data = self._request("GET", endpoint, params)
                    if data is not None:
                        commissions = data.get("commissions") or data.get("data", {}).get("commissions", [])
                        total_commission = data.get("totalCommission") or data.get("data", {}).get("totalCommission", "0")
                        return {"total": data.get("total", len(commissions)), "page": data.get("page", page), "pageSize": data.get("pageSize", page_size), "commissions": commissions, "totalCommission": str(total_commission)}
                except Exception as e:
                    log.debug(f"Commission endpoint {endpoint} failed: {type(e).__name__}")
                    continue
            return None
        except Exception as e:
            log.error(f"Error getting referral commissions from API: {type(e).__name__}")
            return None
    
    def get_all_symbols(self):
        """Получить список всех доступных торговых символов"""
        try:
            raw = self._request("GET", "/openApi/spot/v1/common/symbols")
            if raw is None:
                return []
            symbols_data = raw.get("symbols", [])
            symbols = []
            for item in symbols_data:
                symbol = item.get("symbol", "")
                base_asset = item.get("baseAsset", "")
                quote_asset = item.get("quoteAsset", "")
                status_raw = item.get("status", "")
                if status_raw == 1 or status_raw == "1" or str(status_raw) == "1" or status_raw == "TRADING":
                    status = "TRADING"
                else:
                    status = str(status_raw) if status_raw else "UNKNOWN"
                if status == "TRADING" or status_raw == 1 or str(status_raw) == "1":
                    symbols.append({"symbol": symbol, "baseAsset": base_asset, "quoteAsset": quote_asset, "status": status})
            return sorted(symbols, key=lambda x: x["symbol"])
        except Exception as e:
            log.error(f"Error getting all symbols: {type(e).__name__}")
            return []
    
    def get_popular_symbols(self, quote_asset: str = "USDT", limit: int = 20):
        """Получить популярные торговые пары"""
        try:
            all_symbols = self.get_all_symbols()
            filtered = [s for s in all_symbols if s["quoteAsset"] == quote_asset]
            popular_base = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC", "AVAX", "LINK", "UNI", "LTC", "ATOM", "NEAR"]
            popular = [s for s in filtered if s.get("baseAsset", "") in popular_base]
            others = [s for s in filtered if s.get("baseAsset", "") not in popular_base]
            popular.sort(key=lambda x: (popular_base.index(x["baseAsset"]) if x["baseAsset"] in popular_base else 999, x["symbol"]))
            others.sort(key=lambda x: x["symbol"])
            return (popular + others)[:limit]
        except Exception as e:
            log.error(f"Error getting popular symbols: {type(e).__name__}")
            return []
    
    def get_order_limits(self, symbol: str) -> Dict:
        """Получить информацию о лимитах ордеров для символа"""
        try:
            info = self.symbol_info(symbol)
            return {
                "minQty": info.get("minQty", Decimal("0")),
                "maxQty": info.get("maxQty", Decimal("0")),
                "minNotional": info.get("minNotional", Decimal("0")),
                "stepSize": info.get("stepSize", Decimal("0.000001")),
                "tickSize": info.get("tickSize", Decimal("0.01")),
                "status": info.get("status", "UNKNOWN")
            }
        except Exception as e:
            log.error(f"Error getting order limits: {type(e).__name__}")
            return {
                "minQty": Decimal("0.000001"),
                "maxQty": Decimal("0"),
                "minNotional": Decimal("0"),
                "stepSize": Decimal("0.000001"),
                "tickSize": Decimal("0.01"),
                "status": "UNKNOWN"
            }


class BingXSpotAsync:
    """
    Асинхронная обёртка над BingXSpot. Все вызовы к бирже выполняются
    в thread pool (asyncio.to_thread), чтобы не блокировать event loop.
    Позволяет обслуживать 100+ пользователей без зависаний.
    """
    def __init__(self, sync_client: "BingXSpot"):
        self._sync = sync_client

    @property
    def circuit_breaker(self):
        return self._sync.circuit_breaker

    async def price(self, symbol: str) -> Decimal:
        return await asyncio.to_thread(self._sync.price, symbol)

    async def balance(self, asset: str) -> Decimal:
        return await asyncio.to_thread(self._sync.balance, asset)

    async def available_balance(self, asset: str) -> Decimal:
        return await asyncio.to_thread(self._sync.available_balance, asset)

    async def invalidate_balance_cache(self, asset: str = None):
        return await asyncio.to_thread(self._sync.invalidate_balance_cache, asset)

    async def open_orders(self, symbol: str):
        return await asyncio.to_thread(self._sync.open_orders, symbol)

    async def cancel_order(self, symbol: str, order_id: str):
        return await asyncio.to_thread(self._sync.cancel_order, symbol, order_id)

    async def cancel_all(self, symbol: str):
        return await asyncio.to_thread(self._sync.cancel_all, symbol)

    async def symbol_info(self, symbol: str):
        return await asyncio.to_thread(self._sync.symbol_info, symbol)

    async def place_limit(self, symbol: str, side: str, qty: Decimal, price: Decimal, delay: float = 0.2, validate: bool = True):
        return await asyncio.to_thread(
            self._sync.place_limit, symbol, side, qty, price, delay, validate
        )

    async def place_market(self, symbol: str, side: str, qty: Decimal, quote_order_qty: Decimal = None):
        return await asyncio.to_thread(
            self._sync.place_market, symbol, side, qty, quote_order_qty
        )

    async def get_order(self, symbol: str, order_id: str):
        return await asyncio.to_thread(self._sync.get_order, symbol, order_id)

    async def _request(self, method: str, endpoint: str, params: dict = None):
        return await asyncio.to_thread(self._sync._request, method, endpoint, params)

    @property
    def _cache(self):
        return self._sync._cache
