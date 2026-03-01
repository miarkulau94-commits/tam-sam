"""
AutoScaleX Pro 2.2 - Торговый бот с Grid Trading + Pyramiding + DCA
"""

import asyncio
import logging
import os
import time
from decimal import ROUND_DOWN, Decimal
from enum import IntEnum
from statistics import Statistics
from typing import List

import config
from buy_position import PositionManager
from exchange import BingXSpot, BingXSpotAsync
from order_manager import Order, deduplicate_orders
from persistence import StatePersistence

log = logging.getLogger("trading_bot")
__all__ = ["Order", "TradingBot", "BotState"]


class BotState(IntEnum):
    """Состояния бота"""

    INITIALIZING = 0
    TRADING = 1
    PAUSED = 2
    CRITICAL = 3
    REBALANCING = 4
    STOPPED = 5


class TradingBot:
    """Основной класс торгового бота"""

    def __init__(self, user_id: int, api_key: str, secret: str, telegram_notifier=None, symbol: str = None):
        self.user_id = user_id
        self.telegram_notifier = telegram_notifier  # Сохраняем для использования в main_loop
        sync_ex = BingXSpot(api_key, secret, telegram_notifier)
        self.ex = BingXSpotAsync(sync_ex)
        self.ex.circuit_breaker.reset()
        # Используем переданный символ или дефолтный из config
        self.symbol = symbol or config.SYMBOL
        self.state = BotState.INITIALIZING

        # Балансы
        self.current_deposit = Decimal("0")
        self.base_asset = Decimal("0")

        # Состояние стратегии
        self.orders: List[Order] = []
        self.profit_bank = Decimal("0")
        self.total_executed_buys = 0
        self.total_executed_sells = 0
        self.last_exchange_order_ids = set()  # Сохраняем предыдущий список ID ордеров для сравнения

        # Критический уровень
        self.deposit_requested = False
        self.vwap = Decimal("0")

        # Флаг: отменили 5 BUY для подготовки ребаланса (1 SELL остался), но цена пошла вниз
        self._cancelled_buy_for_rebalance_prep = False

        # Статистика (инициализируем позже, после получения UID)
        self.initial_equity = Decimal("0")
        self.statistics = None  # Будет инициализирован в load_state после получения UID

        # FIFO позиции
        self.position_manager = PositionManager()

        # Настройки пользователя
        self.grid_step_pct = config.GRID_STEP_PCT  # Может быть изменен через Telegram
        self.buy_order_value = config.BUY_ORDER_VALUE  # Может быть изменен через Telegram

        # Получаем BASE и QUOTE из символа
        if "-" in self.symbol:
            self.base_asset_name, self.quote_asset_name = self.symbol.split("-")
        else:
            # Fallback на дефолтные значения
            self.base_asset_name = config.BASE
            self.quote_asset_name = config.QUOTE

        # Сохранение состояния
        self.persistence = StatePersistence()

        # Получаем UID пользователя для сохранения сделок
        state = self.persistence.load_state(self.user_id)
        self.uid = state.get("uid") if state else None

        # Загружаем сохраненное состояние
        self.load_state()

    def load_state(self):
        """Загрузить сохраненное состояние"""
        try:
            state = self.persistence.load_state(self.user_id)
            if state:
                # Получаем UID из состояния
                if not hasattr(self, "uid") or not self.uid:
                    self.uid = state.get("uid")

                # Если UID нет, используем user_id как fallback (для админа и других пользователей)
                if not self.uid:
                    self.uid = str(self.user_id)
                    log.info(f"UID not set, using user_id as fallback: {self.uid}")

                # Инициализируем Statistics с UID и persistence для сохранения сделок
                if not self.statistics:
                    trades_dir = getattr(config, "TRADES_DIR", None) or "."
                    os.makedirs(trades_dir, exist_ok=True)
                    csv_path = os.path.join(trades_dir, f"trades_{self.user_id}.csv")
                    json_path = os.path.join(trades_dir, f"statistics_{self.user_id}.json")
                    self.statistics = Statistics(csv_file=csv_path, json_file=json_path, uid=self.uid, persistence=self.persistence)

                self.profit_bank = Decimal(str(state.get("profit_bank", "0")))
                self.total_executed_buys = state.get("total_executed_buys", 0)
                self.total_executed_sells = state.get("total_executed_sells", 0)
                self.initial_equity = Decimal(str(state.get("initial_equity", "0")))
                saved_grid_step = state.get("grid_step_pct", str(config.GRID_STEP_PCT))
                # Проверяем, что сохраненное значение корректно (должно быть между 0 и 1)
                saved_grid_step_decimal = Decimal(str(saved_grid_step))
                log.debug(f"Loading grid_step_pct: saved={saved_grid_step}, decimal={saved_grid_step_decimal}")

                # Проверяем, если значение >= 1, то это процент (0.75 = 75%), преобразуем в десятичное (0.0075 = 0.75%)
                if saved_grid_step_decimal >= 1:
                    self.grid_step_pct = saved_grid_step_decimal / Decimal("100")
                    log.warning(
                        f"Grid step was saved as percentage >= 1 ({saved_grid_step}), converted to {self.grid_step_pct} ({self.grid_step_pct * 100:.2f}%)"
                    )
                # Проверяем если значение между 0.01 и 1 - возможно это процент в неправильном формате (0.75 вместо 0.0075)
                elif saved_grid_step_decimal >= Decimal("0.01"):
                    # Если это известные значения процентов (0.75, 1.5), преобразуем в десятичные
                    known_percentages = [Decimal("0.75"), Decimal("0.075"), Decimal("1.5"), Decimal("1.50"), Decimal("15")]
                    if saved_grid_step_decimal in known_percentages or (
                        saved_grid_step_decimal >= Decimal("0.05") and saved_grid_step_decimal <= Decimal("5")
                    ):
                        self.grid_step_pct = saved_grid_step_decimal / Decimal("100")
                        log.warning(
                            f"Grid step was saved as percentage ({saved_grid_step}), converted to {self.grid_step_pct} ({self.grid_step_pct * 100:.2f}%)"
                        )
                    else:
                        # Если это уже правильное десятичное значение (0.0075, 0.015), используем как есть
                        self.grid_step_pct = saved_grid_step_decimal
                        log.debug(f"Grid step loaded as decimal: {self.grid_step_pct} ({self.grid_step_pct * 100:.2f}%)")
                elif saved_grid_step_decimal > 0:
                    self.grid_step_pct = saved_grid_step_decimal
                else:
                    self.grid_step_pct = config.GRID_STEP_PCT
                    log.warning(f"Grid step was invalid ({saved_grid_step}), resetting to default {config.GRID_STEP_PCT}")

                log.info(f"Final grid_step_pct: {self.grid_step_pct} ({self.grid_step_pct * 100:.2f}%)")
                saved_buy_order_value = Decimal(str(state.get("buy_order_value", str(config.BUY_ORDER_VALUE))))
                # Проверяем что buy_order_value больше 0, иначе используем значение по умолчанию
                if saved_buy_order_value > 0:
                    self.buy_order_value = saved_buy_order_value
                else:
                    self.buy_order_value = config.BUY_ORDER_VALUE
                    log.warning(f"buy_order_value was {saved_buy_order_value}, resetting to {config.BUY_ORDER_VALUE}")

                # Загружаем символ (если сохранен)
                saved_symbol = state.get("symbol")
                if saved_symbol:
                    self.symbol = saved_symbol
                    if "-" in self.symbol:
                        self.base_asset_name, self.quote_asset_name = self.symbol.split("-")
                    else:
                        # Fallback на дефолтные значения
                        self.base_asset_name = config.BASE
                        self.quote_asset_name = config.QUOTE

                # Загружаем ордера
                orders_data = state.get("orders", [])
                self.orders = [Order.from_dict(o) for o in orders_data]

                # Восстанавливаем состояние бота (TRADING/PAUSED/STOPPED) для авто-рестарта
                if "bot_state" in state:
                    try:
                        v = int(state["bot_state"])
                        if BotState.INITIALIZING.value <= v <= BotState.STOPPED.value:
                            self.state = BotState(v)
                    except (ValueError, TypeError):
                        pass

                log.info(f"State loaded for user {self.user_id}")
            else:
                # Если state пустой, используем user_id как UID
                if not hasattr(self, "uid") or not self.uid:
                    self.uid = str(self.user_id)
                    log.info(f"State empty, using user_id as UID: {self.uid}")

                # Инициализируем Statistics с UID и persistence
                if not self.statistics:
                    trades_dir = getattr(config, "TRADES_DIR", None) or "."
                    os.makedirs(trades_dir, exist_ok=True)
                    csv_path = os.path.join(trades_dir, f"trades_{self.user_id}.csv")
                    json_path = os.path.join(trades_dir, f"statistics_{self.user_id}.json")
                    self.statistics = Statistics(csv_file=csv_path, json_file=json_path, uid=self.uid, persistence=self.persistence)
        except Exception as e:
            log.error(f"Error loading state: {e}")
            # В случае ошибки используем user_id как UID и инициализируем Statistics
            if not hasattr(self, "uid") or not self.uid:
                self.uid = str(self.user_id)
            if not self.statistics:
                trades_dir = getattr(config, "TRADES_DIR", None) or "."
                os.makedirs(trades_dir, exist_ok=True)
                csv_path = os.path.join(trades_dir, f"trades_{self.user_id}.csv")
                json_path = os.path.join(trades_dir, f"statistics_{self.user_id}.json")
                self.statistics = Statistics(csv_file=csv_path, json_file=json_path, uid=self.uid, persistence=self.persistence)

    def save_state(self):
        """Сохранить текущее состояние"""
        try:
            # Проверяем grid_step_pct перед сохранением
            if self.grid_step_pct >= 1:
                log.error(f"grid_step_pct >= 1 before save ({self.grid_step_pct}), fixing to {self.grid_step_pct / Decimal('100')}")
                self.grid_step_pct = self.grid_step_pct / Decimal("100")

            # Убеждаемся, что UID установлен (используем user_id как fallback)
            if not hasattr(self, "uid") or not self.uid:
                self.uid = str(self.user_id)

            state = {
                "uid": self.uid,  # Сохраняем UID в состоянии
                "profit_bank": str(self.profit_bank),
                "total_executed_buys": self.total_executed_buys,
                "total_executed_sells": self.total_executed_sells,
                "initial_equity": str(self.initial_equity),
                "grid_step_pct": str(self.grid_step_pct),  # Сохраняем как десятичное (0.0075 для 0.75%)
                "buy_order_value": str(self.buy_order_value),
                "symbol": self.symbol,  # Сохраняем символ
                "orders": [o.to_dict() for o in self.orders if o.status == "open"],
                "bot_state": int(self.state),  # TRADING=1, PAUSED=2, STOPPED=5 — для авто-восстановления при перезапуске
            }
            log.debug(f"Saving state: grid_step_pct={self.grid_step_pct} ({self.grid_step_pct * 100:.2f}%), uid={self.uid}")
            self.persistence.save_state(self.user_id, state)

            # Сохраняем настройки в файл пользователя по UID
            if self.uid and self.persistence:
                try:
                    settings = {
                        "grid_step_pct": str(self.grid_step_pct),
                        "buy_order_value": str(self.buy_order_value),
                        "symbol": self.symbol,
                        "profit_bank": str(self.profit_bank),
                        "initial_equity": str(self.initial_equity),
                    }
                    self.persistence.save_user_settings(self.uid, settings)
                    log.debug(f"User settings saved for UID {self.uid}")
                except Exception as e:
                    log.warning(f"Error saving user settings for UID {self.uid}: {e}")
        except Exception as e:
            log.error(f"Error saving state: {e}")

    async def get_current_price(self) -> Decimal:
        """Получить текущую цену"""
        price = await self.ex.price(self.symbol)
        if price <= 0:
            raise ValueError(f"Текущая цена {self.symbol} равна нулю или отрицательная: {price}")
        return price

    async def get_total_equity(self, price: Decimal) -> Decimal:
        """Рассчитать общий equity
        Открытые SELL ордера уже заблокированы в балансе base_asset, поэтому они автоматически не учитываются
        """
        # Получаем текущий баланс (открытые SELL ордера уже заблокированы и не учитываются в балансе)
        quote_balance = await self.ex.balance(self.quote_asset_name)
        base_balance = await self.ex.balance(self.base_asset_name)

        # Equity = текущий баланс USDT + текущий баланс base asset * цена
        # (открытые SELL ордера уже заблокированы в base_balance, поэтому не учитываются)
        total_equity = quote_balance + (base_balance * price)

        return total_equity

    async def calculate_vwap(self) -> Decimal:
        """Рассчитать VWAP (среднюю цену покупки)"""
        avg_price = self.position_manager.get_average_price()
        if avg_price > 0:
            return avg_price
        return await self.get_current_price()

    def get_max_buy_orders(self) -> int:
        """Получить максимальное количество BUY ордеров на основе шага сетки
        - При шаге 0.75% (0.0075) -> максимум 125 BUY (всего 125+5=130)
        - При шаге 1.5% (0.015) -> максимум 60 BUY (всего 60+5=65)
        - Для других значений используется линейная интерполяция
        """
        if self.grid_step_pct is None:
            return config.MAX_BUY_ORDERS

        step_pct = float(self.grid_step_pct)

        # 0.75% = 0.0075 -> 125 BUY (итого 130 с SELL)
        if abs(step_pct - 0.0075) < 0.0001:
            return 125
        # 1.5% = 0.015 -> 60 BUY (итого 65 с SELL)
        elif abs(step_pct - 0.015) < 0.0001:
            return 60
        else:
            # Линейная интерполяция: 0.75%->125, 1.5%->60
            # max = 125 - (step - 0.0075) / 0.0075 * 65
            if step_pct <= 0.0075:
                return 125
            elif step_pct >= 0.015:
                return 60
            else:
                max_orders = int(125 - (step_pct - 0.0075) / 0.0075 * 65)
                return max(60, min(125, max_orders))

    def get_min_open_orders_for_protection(self) -> int:
        """Минимальное число открытых ордеров, при котором срабатывает защита «3 BUY → добавить 5».
        Только при «большой» сетке: 1.5% -> 62, 0.75% -> 127 (чтобы не растягивать маленькую сетку)."""
        if self.grid_step_pct is None:
            return 127
        step_pct = float(self.grid_step_pct)
        if abs(step_pct - 0.0075) < 0.0001:
            return 127
        if abs(step_pct - 0.015) < 0.0001:
            return 62
        if step_pct <= 0.0075:
            return 127
        if step_pct >= 0.015:
            return 62
        return int(127 - (step_pct - 0.0075) / 0.0075 * 65)  # интерполяция 127..62

    async def calculate_active_buy_orders_count(self) -> int:
        """Рассчитать количество активных BUY ордеров
        Рассчитывается на основе баланса и прибыли, без фиксированного минимума
        """
        quote_balance = await self.ex.balance(self.quote_asset_name)

        if quote_balance <= 0 or self.buy_order_value is None or self.buy_order_value <= 0:
            return config.MIN_BUY_ORDERS  # Минимум 1 ордер

        # Рассчитываем сколько ордеров можно купить на текущий баланс
        base_count = int(quote_balance / self.buy_order_value)

        # Добавляем дополнительные ордера из profit_bank
        if self.buy_order_value > 0:
            additional = int(self.profit_bank / self.buy_order_value)
        else:
            additional = 0

        total = base_count + additional

        # Ограничиваем только максимумом, минимум всегда 1
        max_buy_orders = self.get_max_buy_orders()
        return min(max(total, 1), max_buy_orders)

    def get_required_notional(self, min_notional: Decimal) -> Decimal:
        """Получить требуемый номинал ордера
        Использует minNotional из API (GET), если оно > 0, иначе использует 0 (биржа не требует минимум)
        """
        if min_notional > 0:
            return min_notional
        return Decimal("0")

    async def _create_grid_do_market_buy(self, price: Decimal) -> Decimal:
        """Шаг 1: рыночная покупка для SELL ордеров. Возвращает текущий баланс base asset."""
        quote_balance = await self.ex.balance(self.quote_asset_name)
        market_buy_amount_usdt = (self.buy_order_value * Decimal("5")) + Decimal("2")
        current_base_balance = await self.ex.balance(self.base_asset_name)

        if quote_balance < market_buy_amount_usdt:
            log.info(f"Insufficient balance for market buy: {quote_balance:.2f} < {market_buy_amount_usdt:.2f}. Will create SELL from existing balance.")
            return await self.ex.balance(self.base_asset_name)

        try:
            log.info(f"Step 1: Performing market buy for SELL orders: {market_buy_amount_usdt} {self.quote_asset_name}")
            market_result = await self.ex.place_market(self.symbol, "BUY", qty=Decimal("0"), quote_order_qty=market_buy_amount_usdt)
            if market_result and market_result.get("orderId"):
                log.info(f"🟩 Market buy successful: orderId={market_result.get('orderId')}")
                await asyncio.sleep(3)
                try:
                    order_info = await self.ex.get_order(self.symbol, market_result.get("orderId"))
                    if order_info:
                        executed_price = Decimal(str(order_info.get("price", price)))
                        executed_qty = Decimal(str(order_info.get("executedQty", "0")))
                        if executed_qty > 0:
                            current_base_balance = await self.ex.balance(self.base_asset_name)
                            self.position_manager.add_position(executed_price, executed_qty)
                            log.info(f"🟩 Added initial position: {executed_qty} {self.base_asset_name} at {executed_price}")
                        else:
                            await asyncio.sleep(1)
                            current_base_balance = await self.ex.balance(self.base_asset_name)
                            if current_base_balance > 0:
                                current_market_price = await self.get_current_price()
                                self.position_manager.add_position(current_market_price, current_base_balance)
                                log.info(f"🟩 Added initial position using balance: {current_base_balance} {self.base_asset_name}")
                except (OSError, RuntimeError) as e:
                    log.warning(f"Failed to get order info after market buy: {type(e).__name__}")
                    await asyncio.sleep(1)
                    current_base_balance = await self.ex.balance(self.base_asset_name)
                    if current_base_balance > 0:
                        current_market_price = await self.get_current_price()
                        self.position_manager.add_position(current_market_price, current_base_balance)
                return current_base_balance
            else:
                log.warning("Market buy failed or no orderId in result")
        except (ValueError, RuntimeError) as e:
            error_msg = str(e)
            if "Permission denied" in error_msg or "Spot Trading permission" in error_msg:
                raise
            if "balance not enough" in error_msg.lower() or "insufficient" in error_msg.lower():
                log.warning("Insufficient balance for market buy")
        except Exception as e:
            log.warning(f"Failed market buy for SELL grid: {type(e).__name__}")
        return await self.ex.balance(self.base_asset_name)

    async def _create_grid_do_sell_orders(
        self, price: Decimal, step: Decimal, tick: Decimal, min_qty: Decimal, min_notional: Decimal
    ) -> int:
        """Шаг 2: создать SELL ордера. Возвращает количество созданных."""
        current_base_balance = await self.ex.balance(self.base_asset_name)
        if current_base_balance <= 0:
            return 0
        open_sell = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
        locked = sum(o.qty for o in open_sell)
        available = current_base_balance - locked
        if available <= 0 or config.SELL_ORDERS_COUNT <= 0:
            return 0
        sell_qty_per_order = available / Decimal(config.SELL_ORDERS_COUNT)
        current_sell_price = price * (Decimal("1") + self.grid_step_pct)
        created = 0
        for i in range(config.SELL_ORDERS_COUNT):
            level_price = (current_sell_price // tick) * tick
            if level_price <= 0:
                current_sell_price = current_sell_price * (Decimal("1") + self.grid_step_pct)
                continue
            qty = sell_qty_per_order.quantize(step, rounding=ROUND_DOWN)
            sell_notional = qty * level_price
            required = self.get_required_notional(min_notional)
            if qty >= min_qty and sell_notional >= required:
                try:
                    result = await self.ex.place_limit(self.symbol, "SELL", qty, level_price, delay=0.1)
                    if result:
                        self.orders.append(
                            Order(order_id=str(result.get("orderId", "")), side="SELL", price=level_price, qty=qty)
                        )
                        created += 1
                        log.info(f"🟥 SELL order {i + 1}: Placed at {level_price:.8f}, qty={qty:.8f}")
                        await asyncio.sleep(0.2)
                except (OSError, RuntimeError, ValueError) as e:
                    log.warning(f"Failed to place SELL order at {level_price}: {type(e).__name__}")
            current_sell_price = current_sell_price * (Decimal("1") + self.grid_step_pct)
        return created

    async def _create_grid_do_buy_orders(
        self, price: Decimal, step: Decimal, tick: Decimal, min_qty: Decimal, min_notional: Decimal
    ) -> int:
        """Шаг 3: создать BUY ордера. Возвращает количество созданных."""
        buy_count = await self.calculate_active_buy_orders_count()
        if buy_count <= 0:
            raise ValueError(f"Количество BUY ордеров должно быть больше 0: {buy_count}")
        current_buy_price = price * (Decimal("1") - self.grid_step_pct)
        created = 0
        for i in range(buy_count):
            if current_buy_price < price * Decimal("0.1"):
                log.warning(f"Buy price too low, stopping at order {i + 1}/{buy_count}")
                break
            level_price = (current_buy_price // tick) * tick
            if level_price <= 0 or level_price < tick:
                current_buy_price = current_buy_price * (Decimal("1") - self.grid_step_pct)
                continue
            balance = await self.ex.balance(self.quote_asset_name)
            if balance < self.buy_order_value:
                log.info(f"Order {i + 1}: Insufficient balance, stopping")
                break
            qty = (self.buy_order_value / level_price).quantize(step, rounding=ROUND_DOWN)
            notional = qty * level_price
            required = self.get_required_notional(min_notional)
            if qty >= min_qty and notional >= required:
                try:
                    result = await self.ex.place_limit(self.symbol, "BUY", qty, level_price, delay=0.1, validate=True)
                    if result and result.get("orderId"):
                        self.orders.append(
                            Order(
                                order_id=str(result.get("orderId", "")), side="BUY", price=level_price, qty=qty,
                                amount_usdt=self.buy_order_value
                            )
                        )
                        created += 1
                        log.info(f"🟩 Order {i + 1}: BUY placed at {level_price:.8f}, qty={qty:.8f}")
                        await asyncio.sleep(0.2)
                except ValueError as ve:
                    err = str(ve)
                    if "Spot Trading" in err or "Permission denied" in err:
                        if self.telegram_notifier:
                            try:
                                await self.telegram_notifier(
                                    "❌ **Ошибка разрешений API**\n\nВключите Spot Trading на https://bingx.com/en/account/api"
                                )
                            except Exception:
                                pass
                        raise
                    log.warning(f"Order {i + 1}: Validation failed: {err[:80]}")
                except (OSError, RuntimeError) as e:
                    log.warning(f"Order {i + 1}: Failed to place BUY at {level_price}: {type(e).__name__}")
            current_buy_price = current_buy_price * (Decimal("1") - self.grid_step_pct)
        return created

    async def create_grid(self):
        """Создать сетку ордеров: сначала покупаем по рынку для SELL ордеров, затем создаем SELL и BUY ордера"""
        try:
            price = await self.get_current_price()
            await self.ex.cancel_all(self.symbol)
            await asyncio.sleep(2)
            self.orders.clear()

            if self.buy_order_value is None or self.buy_order_value <= 0:
                raise ValueError(f"Размер ордера должен быть больше 0. Текущее значение: {self.buy_order_value}")
            if self.grid_step_pct is None or self.grid_step_pct <= 0 or self.grid_step_pct >= 1:
                log.warning(f"grid_step_pct invalid, resetting to default")
                self.grid_step_pct = config.GRID_STEP_PCT

            info = await self.ex.symbol_info(self.symbol)
            step = info.get("stepSize", Decimal("0.000001"))
            tick = info.get("tickSize", Decimal("0.01"))
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))
            log.info(f"Symbol info: step={step}, tick={tick}, minQty={min_qty}")

            # ШАГ 1–3: market buy, SELL ордера, BUY ордера
            await self._create_grid_do_market_buy(price)

            if config.SELL_ORDERS_COUNT <= 0:
                raise ValueError(f"SELL_ORDERS_COUNT должно быть > 0: {config.SELL_ORDERS_COUNT}")

            created_sell_orders = await self._create_grid_do_sell_orders(price, step, tick, min_qty, min_notional)
            created_buy_orders = await self._create_grid_do_buy_orders(price, step, tick, min_qty, min_notional)

            created_sell_orders_final = len([o for o in self.orders if o.side == "SELL"])
            quote_balance_final = await self.ex.balance(self.quote_asset_name)
            base_balance_final = await self.ex.balance(self.base_asset_name)

            if created_buy_orders == 0 and created_sell_orders_final == 0:
                details = []
                if quote_balance_final < self.buy_order_value:
                    details.append(f"Недостаточно {self.quote_asset_name}: {quote_balance_final:.2f}")
                if not details:
                    details.append("Не удалось разместить ордера. Проверьте настройки и баланс.")
                raise ValueError("Не удалось создать ни одного ордера.\n" + "\n".join(details))

            if created_buy_orders > 0 and created_sell_orders_final == 0 and base_balance_final <= 0:
                info_msg = f"✅ BUY ордера выставлены ({created_buy_orders}). SELL будут созданы после исполнения BUY."
                log.info(info_msg)
                if self.telegram_notifier:
                    try:
                        await self.telegram_notifier(info_msg)
                    except Exception:
                        pass

            await asyncio.to_thread(self.save_state)
            log.info(f"🟩 Grid created: {created_buy_orders} BUY + {created_sell_orders_final} SELL orders")

        except (ValueError, RuntimeError, OSError) as e:
            log.error(f"Failed to create grid: {type(e).__name__}")
            raise

    async def create_critical_sell_grid(self):
        """Создать критическую SELL сетку от VWAP (3 ордера)

        Returns:
            dict: Словарь с результатами:
                - created_count: количество созданных ордеров
                - vwap: средняя цена покупки (VWAP)
                - orders_info: список информации о созданных ордерах
        """
        result = {"created_count": 0, "vwap": Decimal("0"), "orders_info": []}

        try:
            # Рассчитываем VWAP (среднюю цену покупки)
            self.vwap = await self.calculate_vwap()
            result["vwap"] = self.vwap

            # Если VWAP = 0, значит нет позиций покупки - используем текущую рыночную цену
            if self.vwap <= 0:
                current_price = await self.get_current_price()
                log.warning(f"VWAP is 0 (no positions), using current market price: {current_price}")
                self.vwap = current_price
                result["vwap"] = self.vwap

            current_base_balance = await self.ex.balance(self.base_asset_name)

            if current_base_balance == 0:
                log.warning("No base asset for critical sell grid")
                return result

            # Отменяем все существующие SELL ордера
            canceled_count = 0
            orders_to_remove = []
            for order in list(self.orders):
                if order.side == "SELL" and order.status == "open":
                    try:
                        await self.ex.cancel_order(self.symbol, order.order_id)
                        canceled_count += 1
                        orders_to_remove.append(order)
                        log.debug(f"Canceled SELL order {order.order_id}")
                    except Exception as e:
                        if "order not exist" in str(e).lower() or "not found" in str(e).lower():
                            orders_to_remove.append(order)
                            log.debug(f"SELL order {order.order_id} already gone from exchange")
                        else:
                            log.warning(f"Failed to cancel SELL order {order.order_id}: {type(e).__name__}")

            for order in orders_to_remove:
                if order in self.orders:
                    self.orders.remove(order)

            if canceled_count > 0:
                log.info(f"Canceled {canceled_count} existing SELL orders")

            open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            locked_base_asset = sum(o.qty for o in open_sell_orders)
            available_base_asset = current_base_balance - locked_base_asset

            if available_base_asset <= 0:
                log.warning(f"No available base asset for SELL orders (total: {current_base_balance}, locked: {locked_base_asset})")
                return result

            info = await self.ex.symbol_info(self.symbol)
            step = info["stepSize"]
            tick = info["tickSize"]
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            sell_qty_per_order = available_base_asset / Decimal(config.CRITICAL_SELL_DIVISIONS)

            log.info(
                f"Creating {config.CRITICAL_SELL_DIVISIONS} SELL orders from VWAP {self.vwap:.8f} with multipliers {config.CRITICAL_GRID_STEP_MULTIPLIER}"
            )

            for i, multiplier in enumerate(config.CRITICAL_GRID_STEP_MULTIPLIER, 1):
                level_price = self.vwap * (Decimal("1") + self.grid_step_pct * Decimal(multiplier))
                level_price = (level_price // tick) * tick

                if level_price <= 0:
                    log.warning(f"SELL order {i}: level_price is zero or negative: {level_price}, skipping")
                    continue

                qty = sell_qty_per_order.quantize(step, rounding=ROUND_DOWN)
                critical_notional = qty * level_price
                critical_required_notional = self.get_required_notional(min_notional)

                if qty < min_qty:
                    log.warning(f"SELL order {i}: qty {qty} < minQty {min_qty}, skipping")
                    continue
                if critical_notional < critical_required_notional:
                    log.warning(f"SELL order {i}: notional {critical_notional} < required {critical_required_notional}, skipping")
                    continue

                try:
                    result_order = await self.ex.place_limit(self.symbol, "SELL", qty, level_price, delay=0.1, validate=True)
                    if result_order and result_order.get("orderId"):
                        order = Order(order_id=str(result_order.get("orderId", "")), side="SELL", price=level_price, qty=qty)
                        self.orders.append(order)
                        result["created_count"] += 1
                        order_info = {
                            "order_id": order.order_id,
                            "price": float(level_price),
                            "qty": float(qty),
                            "multiplier": multiplier,
                            "profit_pct": float(self.grid_step_pct * Decimal(multiplier) * Decimal("100")),
                        }
                        result["orders_info"].append(order_info)
                        log.info(
                            f"🟥 SELL order {i}: Placed at {level_price:.8f} (VWAP * {1 + float(self.grid_step_pct * Decimal(multiplier)):.4f}), qty={qty:.8f}"
                        )
                        await asyncio.sleep(0.2)
                    else:
                        log.warning(f"SELL order {i}: Failed to place - API returned no orderId")
                except ValueError as validation_error:
                    log.warning(f"SELL order {i}: Validation failed: {validation_error}")
                except (OSError, RuntimeError) as e:
                    log.warning(f"SELL order {i}: Failed to place critical SELL at {level_price}: {type(e).__name__}")

            await asyncio.to_thread(self.save_state)
            log.info(f"🟥 Critical sell grid: created {result['created_count']}/{config.CRITICAL_SELL_DIVISIONS} orders from VWAP {self.vwap:.8f}")

        except (OSError, RuntimeError, ValueError) as e:
            log.error(f"Failed to create critical sell grid: {type(e).__name__}")

        return result

    async def handle_buy_filled(self, order: Order, price: Decimal):
        """Обработка исполнения BUY ордера"""
        try:
            if self.state == BotState.STOPPED:
                return
            log.info(f"🟩 Processing BUY order fill: orderId={order.order_id}, price={price:.8f}, qty={order.qty:.8f}")

            self.base_asset = await self.ex.balance(self.base_asset_name)
            self.current_deposit = await self.ex.balance(self.quote_asset_name)

            # Получаем реальное количество после исполнения (с учетом комиссии)
            btc_received = order.qty * (Decimal("1") - config.FEE_RATE)
            self.total_executed_buys += 1

            log.info(
                f"🟩 BUY executed: received={btc_received:.8f} {self.base_asset_name}, balance={self.base_asset:.8f}, deposit={self.current_deposit:.2f} {self.quote_asset_name}"
            )

            self.position_manager.add_position(price, btc_received)
            order.status = "filled"
            order.executed_qty = order.qty

            self.statistics.save_trade(
                {
                    "type": "BUY",
                    "symbol": self.symbol,
                    "price": price,
                    "qty": btc_received,
                    "amount_usdt": order.amount_usdt if order.amount_usdt > 0 else (order.qty * price),
                    "profit": Decimal("0"),
                    "profit_bank": self.profit_bank,
                    "total_equity": await self.get_total_equity(price),
                }
            )

            # После BUY создаем один SELL ордер на сумму покупки плюс шаг сетки
            # Используем реальное количество базового актива с биржи
            # Создаем SELL ордер после BUY
            log.info(f"[BUY FILL] Step 1: Attempting to create SELL order after BUY fill at price {price:.8f}")
            try:
                info = await self.ex.symbol_info(self.symbol)
                step = info["stepSize"]
                tick = info["tickSize"]
                min_qty = info.get("minQty", Decimal("0.000001"))
                min_notional = info.get("minNotional", Decimal("0"))

                # Цена SELL = цена покупки + шаг сетки
                sell_price = price * (Decimal("1") + self.grid_step_pct)
                sell_price = (sell_price // tick) * tick
                log.info(f"[CREATE_SELL_AFTER_BUY] Calculated SELL price: {price:.8f} * (1 + {self.grid_step_pct:.6f}) = {sell_price:.8f}")

                # Используем реальное количество базового актива, полученное после исполнения BUY
                # Проверяем текущий баланс для более точного расчета
                current_base_balance = await self.ex.balance(self.base_asset_name)
                open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
                locked_base_asset = sum(o.qty for o in open_sell_orders)
                available_base_asset = current_base_balance - locked_base_asset

                log.info(
                    f"[CREATE_SELL_AFTER_BUY] Balance check: current={current_base_balance:.8f}, locked={locked_base_asset:.8f}, available={available_base_asset:.8f}, btc_received={btc_received:.8f}"
                )

                # Используем количество из исполненного BUY ордера (реальное количество после комиссии)
                # Но не больше доступного баланса
                sell_qty = min(btc_received, available_base_asset)
                sell_qty = (sell_qty // step) * step  # Округляем до шага

                log.info(
                    f"[CREATE_SELL_AFTER_BUY] Qty calculation: btc_received={btc_received:.8f}, available={available_base_asset:.8f}, sell_qty={sell_qty:.8f}"
                )

                # Проверяем, нет ли уже ордера на этой цене
                existing_sell = any(o.side == "SELL" and abs(o.price - sell_price) < tick and o.status == "open" for o in self.orders)

                # При пирамидировании: BUY исполнился → нужно выставить SELL выше. Лимит SELL_ORDERS_COUNT
                # задаёт только начальную сетку; при добавлении позиции SELL-ов может быть больше 5.
                if existing_sell:
                    log.warning(f"[CREATE_SELL_AFTER_BUY] SKIPPED: SELL order already exists at price {sell_price:.8f}")
                elif sell_qty < min_qty:
                    log.warning(
                        f"[CREATE_SELL_AFTER_BUY] FAILED: SELL order qty too small: {sell_qty:.8f} < {min_qty:.8f} (min_qty), available_base={available_base_asset:.8f}, btc_received={btc_received:.8f}"
                    )
                else:
                    sell_notional = sell_qty * sell_price
                    required_notional = self.get_required_notional(min_notional)
                    log.info(
                        f"[CREATE_SELL_AFTER_BUY] Validation: qty={sell_qty:.8f}, price={sell_price:.8f}, notional={sell_notional:.8f}, required={required_notional:.8f}"
                    )

                    if sell_notional < required_notional:
                        log.warning(f"[CREATE_SELL_AFTER_BUY] FAILED: SELL order notional too small: {sell_notional:.8f} < {required_notional:.8f}")
                    elif available_base_asset < sell_qty:
                        log.warning(
                            f"[CREATE_SELL_AFTER_BUY] FAILED: Insufficient base asset: available={available_base_asset:.8f}, needed={sell_qty:.8f}, current_balance={current_base_balance:.8f}, locked={locked_base_asset:.8f}"
                        )
                    else:
                        if self.state == BotState.STOPPED:
                            return
                        try:
                            log.info(f"[CREATE_SELL_AFTER_BUY] Placing SELL order: price={sell_price:.8f}, qty={sell_qty:.8f}")
                            result = await self.ex.place_limit(self.symbol, "SELL", sell_qty, sell_price, delay=0.1)
                            if result and result.get("orderId"):
                                sell_order = Order(order_id=str(result.get("orderId", "")), side="SELL", price=sell_price, qty=sell_qty)
                                self.orders.append(sell_order)
                                log.info(
                                    f"🟥 ✅ [CREATE_SELL_AFTER_BUY] SUCCESS: Created SELL order after BUY fill: orderId={result.get('orderId')}, price={sell_price:.8f}, qty={sell_qty:.8f}, amount={sell_notional:.2f} {self.quote_asset_name}"
                                )
                            elif result:
                                log.warning(f"[CREATE_SELL_AFTER_BUY] FAILED: API returned result without orderId: {result}")
                            else:
                                log.warning("[CREATE_SELL_AFTER_BUY] FAILED: API returned None or empty result")
                        except Exception as e:
                            log.error(f"[CREATE_SELL_AFTER_BUY] EXCEPTION: Failed to place SELL order at {sell_price:.8f}: {e}", exc_info=True)
            except Exception as e:
                log.error(f"[CREATE_SELL_AFTER_BUY] EXCEPTION: Failed to create SELL order after BUY fill: {e}", exc_info=True)

            # Синхронизируем ордера с биржей после создания новых ордеров
            # Добавляем небольшую задержку, чтобы API биржи успел показать только что созданный ордер
            await asyncio.sleep(1.5)
            log.info("[BUY FILL] Step 2: Syncing orders from exchange")
            await self.sync_orders_from_exchange()

            # Проверяем количество открытых ордеров после синхронизации
            open_sell_after = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            open_buy_after = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            log.info(f"[BUY FILL] After sync: open SELL={len(open_sell_after)}, open BUY={len(open_buy_after)}")

            # Проверяем ребалансировку: если все BUY ордера исполнены, создаем SELL сетку от VWAP
            log.info("[BUY FILL] Step 3: Checking rebalancing after BUY fill")
            await self.check_rebalancing_after_all_buy_filled(price)

            # Если ранее отменили 5 BUY для подготовки ребаланса (1 SELL был), но цена пошла вниз
            # и теперь SELL снова >= 3 — восстанавливаем BUY внизу сетки
            if len(open_sell_after) >= 3 and getattr(self, "_cancelled_buy_for_rebalance_prep", False):
                log.info(
                    f"[BUY FILL] SELL >= 3 ({len(open_sell_after)}), restoring BUY at bottom (price went down after cancel)"
                )
                buy_restored = await self.create_buy_orders_at_bottom(price)
                if buy_restored > 0:
                    log.info(f"[BUY FILL] Restored {buy_restored} BUY orders at bottom of grid")
                self._cancelled_buy_for_rebalance_prep = False

            log.info(f"🟩 [BUY FILL] Processing completed for order {order.order_id}")

            await self.check_critical_level(price)
            if self.profit_bank > 0:
                await self.check_pyramiding()

            await asyncio.to_thread(self.save_state)

        except Exception as e:
            log.error(f"Error handling BUY fill: {e}", exc_info=True)

    async def handle_sell_filled(self, order: Order, price: Decimal):
        """Обработка исполнения SELL ордера"""
        try:
            if self.state == BotState.STOPPED:
                return
            log.info("🟥 ✅ [SELL FILL] ========================================")
            log.info(f"🟥 ✅ [SELL FILL] Processing SELL order fill: orderId={order.order_id}, price={price:.8f}, qty={order.qty:.8f}")
            log.info("🟥 ✅ [SELL FILL] ========================================")

            profit = self.position_manager.calculate_profit_for_sell(order.qty, price, config.FEE_RATE)

            self.base_asset = await self.ex.balance(self.base_asset_name)
            self.current_deposit = await self.ex.balance(self.quote_asset_name)

            self.profit_bank += profit
            self.total_executed_sells += 1

            order.status = "filled"
            order.executed_qty = order.qty

            self.statistics.save_trade(
                {
                    "type": "SELL",
                    "symbol": self.symbol,
                    "price": price,
                    "qty": order.qty,
                    "amount_usdt": order.qty * price,
                    "profit": profit,
                    "profit_bank": self.profit_bank,
                    "total_equity": await self.get_total_equity(price),
                }
            )

            log.info(f"🟥 SELL trade saved: profit={profit:.8f}, profit_bank={self.profit_bank:.8f}, total_executed_sells={self.total_executed_sells}")

            # После SELL создаем новый BUY ордер ниже для поддержания сетки
            # Важно: инвалидируем кеш баланса перед созданием нового BUY ордера,
            # так как после исполнения SELL баланс USDT должен был пополниться
            await self.ex.invalidate_balance_cache(self.quote_asset_name)
            # Небольшая задержка, чтобы биржа успела обновить баланс
            await asyncio.sleep(0.5)

            log.info(f"[SELL FILL] Step 1: Calling create_buy_after_sell with price {price:.8f}")
            buy_created = await self.create_buy_after_sell(price)
            log.info(f"[SELL FILL] Step 1 result: buy_created={buy_created}")

            # Синхронизируем ордера с биржей перед проверкой ребаланса, чтобы check_rebalancing видел только что созданные ордера
            # Добавляем небольшую задержку, чтобы API биржи успел показать только что созданный ордер
            await asyncio.sleep(1.5)
            log.info("[SELL FILL] Step 2: Syncing orders from exchange")
            await self.sync_orders_from_exchange()

            # Проверяем количество открытых ордеров после синхронизации
            open_sell_after = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            open_buy_after = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            log.info(f"[SELL FILL] After sync: open SELL={len(open_sell_after)}, open BUY={len(open_buy_after)}")

            # Если остался 1 SELL ордер (предпоследний исполнился), отменяем 5 последних BUY для освобождения USDT
            if len(open_sell_after) == 1:
                log.info("[SELL FILL] Only 1 SELL order remaining, cancelling 5 last BUY orders to free USDT for new SELL grid")
                canceled_count = await self.cancel_last_n_buy_orders(5)

                if canceled_count > 0:
                    self._cancelled_buy_for_rebalance_prep = True
                    # Инвалидируем кеш баланса после отмены ордеров
                    await self.ex.invalidate_balance_cache(self.quote_asset_name)
                    # Ждем немного, чтобы биржа успела освободить баланс
                    await asyncio.sleep(1)
                    # Обновляем баланс
                    quote_available_after = await self.ex.available_balance(self.quote_asset_name)
                    log.info(
                        f"[SELL FILL] After cancelling {canceled_count} BUY orders: available balance = {quote_available_after:.2f} {self.quote_asset_name}"
                    )

            if self.profit_bank > 0:
                await self.check_pyramiding()

            # Проверяем ребалансировку
            log.info("[SELL FILL] Step 3: Checking rebalancing")
            await self.check_rebalancing(price)

            await asyncio.to_thread(self.save_state)
            log.info(f"🟥 SELL fill processing completed for order {order.order_id}")

        except Exception as e:
            log.error(f"Error handling SELL fill: {e}", exc_info=True)

    async def check_critical_level(self, current_price: Decimal):
        """Проверка на критический уровень"""
        try:
            if self.state == BotState.STOPPED:
                return
            exchange_orders = await self.ex.open_orders(self.symbol)
            exchange_buy_orders = [o for o in exchange_orders if o.get("side") == "BUY"]

            our_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]

            max_buy = self.get_max_buy_orders()
            if (
                len(exchange_buy_orders) == 0
                and len(our_buy_orders) == 0
                and self.total_executed_buys >= max_buy
                and not self.deposit_requested
            ):
                self.deposit_requested = True
                self.state = BotState.CRITICAL
                await self.create_critical_sell_grid()
                log.warning(f"Critical level reached! Executed BUY orders: {self.total_executed_buys}")

        except Exception as e:
            log.error(f"Error checking critical level: {e}")

    async def check_pyramiding(self):
        """Проверка и добавление новых BUY ордеров из profit_bank"""
        try:
            while self.profit_bank >= self.buy_order_value:
                # Проверяем доступный баланс (только free, без locked)
                quote_available = await self.ex.available_balance(self.quote_asset_name)
                quote_total = await self.ex.balance(self.quote_asset_name)

                # Если доступного баланса недостаточно для полного ордера - не создаем
                if quote_available < self.buy_order_value:
                    log.debug(
                        f"[PYRAMIDING] Skipping: insufficient available balance: {quote_available:.2f} < {self.buy_order_value:.2f} (total: {quote_total:.2f})"
                    )
                    break

                open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]

                # Проверяем лимит на основе шага сетки
                max_buy_orders = self.get_max_buy_orders()
                if len(open_buy_orders) >= max_buy_orders:
                    log.debug(
                        f"[PYRAMIDING] Maximum BUY orders reached: {len(open_buy_orders)} >= {max_buy_orders} (grid_step={self.grid_step_pct:.4f})"
                    )
                    break

                if not open_buy_orders:
                    break

                lowest_order = min(open_buy_orders, key=lambda x: x.price)

                new_price = lowest_order.price * (Decimal("1") - self.grid_step_pct)

                info = await self.ex.symbol_info(self.symbol)
                step = info["stepSize"]
                tick = info["tickSize"]
                new_price = (new_price // tick) * tick
                qty = (self.buy_order_value / new_price).quantize(step, rounding=ROUND_DOWN)

                rebalance_notional = qty * new_price
                rebalance_required_notional = self.get_required_notional(info.get("minNotional", Decimal("0")))

                # Дополнительная проверка: убеждаемся, что стоимость ордера не превышает доступный баланс
                if rebalance_notional > quote_available:
                    log.debug(f"[PYRAMIDING] Skipping: calculated notional {rebalance_notional:.2f} > available balance {quote_available:.2f}")
                    break

                if qty >= info.get("minQty", Decimal("0")) and rebalance_notional >= rebalance_required_notional:
                    try:
                        # Перед размещением ордера обновляем баланс (очищаем кеш)
                        await self.ex.invalidate_balance_cache(self.quote_asset_name)
                        current_available = await self.ex.available_balance(self.quote_asset_name)

                        # Финальная проверка доступного баланса перед размещением
                        if rebalance_notional > current_available:
                            log.debug(
                                f"[PYRAMIDING] Skipping: balance changed, notional {rebalance_notional:.2f} > current available {current_available:.2f}"
                            )
                            break

                        result = await self.ex.place_limit(self.symbol, "BUY", qty, new_price, delay=0.1)
                        if result and result.get("orderId"):
                            order = Order(
                                order_id=str(result.get("orderId", "")), side="BUY", price=new_price, qty=qty, amount_usdt=self.buy_order_value
                            )
                            self.orders.append(order)
                            self.profit_bank -= self.buy_order_value
                            await asyncio.to_thread(self.save_state)
                            log.info(
                                f"[PYRAMIDING] ✅ Created BUY order from profit: price={new_price:.8f}, qty={qty:.8f}, orderId={result.get('orderId')}"
                            )
                        elif result:
                            log.warning(f"[PYRAMIDING] FAILED: API returned result without orderId: {result}")
                            break
                        else:
                            log.warning("[PYRAMIDING] FAILED: API returned None or empty result")
                            break
                    except Exception as e:
                        log.warning(f"Failed to add pyramiding BUY order: {e}")
                        break
                else:
                    break

        except Exception as e:
            log.error(f"Error in pyramiding check: {e}")

    async def create_buy_after_sell(self, sell_price: Decimal):
        """Создать новый BUY ордер после исполнения SELL ордера для поддержания сетки
        Новый BUY размещается на месте исполненного SELL или немного ниже по шагу сетки
        Возвращает True если ордер создан, False если нет
        """
        try:
            if self.state == BotState.STOPPED:
                return False
            log.info(f"[CREATE_BUY_AFTER_SELL] Starting: sell_price={sell_price:.8f}")

            # Инвалидируем кеш баланса перед проверкой, чтобы получить актуальное значение
            # После исполнения SELL баланс должен был пополниться
            await self.ex.invalidate_balance_cache(self.quote_asset_name)

            # Проверяем доступный баланс USDT (только free, без locked)
            quote_available = await self.ex.available_balance(self.quote_asset_name)
            quote_total = await self.ex.balance(self.quote_asset_name)

            log.info(
                f"[CREATE_BUY_AFTER_SELL] Balance check: available={quote_available:.2f}, total={quote_total:.2f}, required={self.buy_order_value:.2f}"
            )

            if quote_available < self.buy_order_value:
                log.warning(
                    f"[CREATE_BUY_AFTER_SELL] FAILED: Insufficient available balance: {quote_available:.2f} < {self.buy_order_value:.2f} (total: {quote_total:.2f})"
                )
                return False

            # Проверяем, не превышен ли лимит BUY ордеров (на основе шага сетки)
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            max_buy_orders = self.get_max_buy_orders()
            if len(open_buy_orders) >= max_buy_orders:
                log.warning(
                    f"[CREATE_BUY_AFTER_SELL] FAILED: Maximum BUY orders reached: {len(open_buy_orders)} >= {max_buy_orders} (grid_step={self.grid_step_pct:.4f})"
                )
                return False

            log.info(
                f"Balance check passed: available={quote_available:.2f}, total={quote_total:.2f}, open BUY orders: {len(open_buy_orders)}, max: {max_buy_orders} (grid_step={self.grid_step_pct:.4f})"
            )

            # Рассчитываем цену нового BUY ордера: на месте исполненного SELL минус шаг сетки
            # Это поддерживает сетку - после продажи создаем ордер покупки ниже
            new_buy_price = sell_price * (Decimal("1") - self.grid_step_pct)
            log.info(f"Calculated new BUY price after SELL: {sell_price:.8f} * (1 - {self.grid_step_pct:.6f}) = {new_buy_price:.8f}")

            # Проверяем, не создан ли уже ордер на этой цене
            info = await self.ex.symbol_info(self.symbol)
            tick = info["tickSize"]
            existing_buy = any(o.side == "BUY" and abs(o.price - new_buy_price) < tick and o.status == "open" for o in self.orders)

            if existing_buy:
                # Fallback: при занятой цене пробуем меньший шаг (1% при шаге 1.5%, 0.5% при 0.75%)
                fallback_step_pct = None
                if self.grid_step_pct is not None:
                    step_float = float(self.grid_step_pct)
                    if abs(step_float - 0.015) < 0.0001:
                        fallback_step_pct = Decimal("0.01")  # 1%
                    elif abs(step_float - 0.0075) < 0.0001:
                        fallback_step_pct = Decimal("0.005")  # 0.5%
                if fallback_step_pct is not None:
                    fallback_price = sell_price * (Decimal("1") - fallback_step_pct)
                    existing_at_fallback = any(
                        o.side == "BUY" and abs(o.price - fallback_price) < tick and o.status == "open" for o in self.orders
                    )
                    if not existing_at_fallback:
                        log.info(
                            f"[CREATE_BUY_AFTER_SELL] BUY already at {new_buy_price:.8f}, using fallback step {fallback_step_pct}: price={fallback_price:.8f}"
                        )
                        new_buy_price = fallback_price
                        existing_buy = False
                if existing_buy:
                    log.warning(f"[CREATE_BUY_AFTER_SELL] SKIPPED: BUY order already exists at price {new_buy_price:.8f}")
                    return False

            # Получаем информацию о символе
            step = info["stepSize"]
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            # Рассчитываем количество и цену
            new_buy_price = (new_buy_price // tick) * tick
            qty = (self.buy_order_value / new_buy_price).quantize(step, rounding=ROUND_DOWN)
            buy_notional = qty * new_buy_price
            required_notional = self.get_required_notional(min_notional)

            # Проверяем, что стоимость ордера не превышает доступный баланс
            # Если баланса недостаточно для полного ордера - не создаем ордер
            if buy_notional > quote_available:
                log.warning(
                    f"[CREATE_BUY_AFTER_SELL] FAILED: Calculated notional {buy_notional:.2f} > available balance {quote_available:.2f}. Order will not be created."
                )
                return False

            # Проверяем ограничения
            if qty >= min_qty and buy_notional >= required_notional:
                # Перед размещением ордера обновляем баланс (очищаем кеш)
                # Баланс может измениться между проверкой и размещением
                try:
                    # Очищаем кеш баланса для получения актуального значения
                    cache_key = ("available_balance", self.quote_asset_name)
                    if cache_key in self.ex._cache:
                        del self.ex._cache[cache_key]

                    # Получаем актуальный доступный баланс перед размещением
                    current_available = await self.ex.available_balance(self.quote_asset_name)
                    if buy_notional > current_available:
                        log.warning(
                            f"[CREATE_BUY_AFTER_SELL] FAILED: Balance changed, notional {buy_notional:.2f} > current available {current_available:.2f} (was {quote_available:.2f}). Order will not be created."
                        )
                        return False

                    result = await self.ex.place_limit(self.symbol, "BUY", qty, new_buy_price, delay=0.1)
                    if result and result.get("orderId"):
                        buy_order = Order(
                            order_id=str(result.get("orderId", "")), side="BUY", price=new_buy_price, qty=qty, amount_usdt=self.buy_order_value
                        )
                        self.orders.append(buy_order)
                        log.info(
                            f"🟩 ✅ [CREATE_BUY_AFTER_SELL] SUCCESS: Created BUY order after SELL: price={new_buy_price:.8f}, qty={qty:.8f}, orderId={result.get('orderId')}"
                        )
                        return True
                    elif result:
                        log.warning(f"[CREATE_BUY_AFTER_SELL] FAILED: API returned result without orderId: {result}")
                        return False
                    else:
                        log.warning("[CREATE_BUY_AFTER_SELL] FAILED: API returned None or empty result")
                        return False
                except Exception as e:
                    log.error(f"[CREATE_BUY_AFTER_SELL] EXCEPTION: Failed to create BUY order at {new_buy_price}: {e}", exc_info=True)
                    return False
            else:
                log.warning(
                    f"[CREATE_BUY_AFTER_SELL] VALIDATION FAILED: qty={qty:.8f} (min={min_qty:.8f}), notional={buy_notional:.8f} (required={required_notional:.8f}), sell_price={sell_price:.8f}, new_buy_price={new_buy_price:.8f}"
                )
                return False

        except Exception as e:
            log.error(f"[CREATE_BUY_AFTER_SELL] EXCEPTION: Error creating BUY after SELL: {e}", exc_info=True)
            return False

    async def create_buy_after_buy(self, executed_buy_price: Decimal):
        """Создать новый BUY ордер после исполнения BUY ордера для поддержания сетки"""
        try:
            log.info(f"Attempting to create BUY after BUY at executed price {executed_buy_price:.8f}")

            # Проверяем баланс USDT
            quote_balance = await self.ex.balance(self.quote_asset_name)

            if quote_balance < self.buy_order_value:
                log.warning(f"Insufficient balance for new BUY after BUY: {quote_balance:.2f} < {self.buy_order_value:.2f}")
                return

            # Проверяем, не превышен ли лимит BUY ордеров (зависит от шага сетки)
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            max_buy_orders = self.get_max_buy_orders()
            if len(open_buy_orders) >= max_buy_orders:
                log.warning(f"Maximum BUY orders reached: {len(open_buy_orders)} >= {max_buy_orders}")
                return

            log.info(f"Balance check passed: {quote_balance:.2f}, open BUY orders: {len(open_buy_orders)}, max: {max_buy_orders}")

            # Рассчитываем цену нового BUY ордера: ниже исполненного на шаг сетки
            new_buy_price = executed_buy_price * (Decimal("1") - self.grid_step_pct)
            log.info(f"Calculated new BUY price: {executed_buy_price:.8f} * (1 - {self.grid_step_pct:.6f}) = {new_buy_price:.8f}")

            # Проверяем, не создан ли уже ордер на этой цене
            info = await self.ex.symbol_info(self.symbol)
            tick = info["tickSize"]
            existing_buy = any(o.side == "BUY" and abs(o.price - new_buy_price) < tick and o.status == "open" for o in self.orders)

            if existing_buy:
                log.warning(f"BUY order already exists at price {new_buy_price:.8f}, skipping creation")
                return

            # Получаем информацию о символе
            step = info["stepSize"]
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            # Рассчитываем количество и цену
            new_buy_price = (new_buy_price // tick) * tick
            qty = (self.buy_order_value / new_buy_price).quantize(step, rounding=ROUND_DOWN)
            buy_notional = qty * new_buy_price
            required_notional = self.get_required_notional(min_notional)

            # Проверяем ограничения
            log.info(f"BUY validation: qty={qty:.8f}, min_qty={min_qty:.8f}, notional={buy_notional:.8f}, required={required_notional:.8f}")
            if qty >= min_qty and buy_notional >= required_notional:
                try:
                    log.info(f"Placing BUY order: price={new_buy_price:.8f}, qty={qty:.8f}")
                    result = await self.ex.place_limit(self.symbol, "BUY", qty, new_buy_price, delay=0.1)
                    if result and result.get("orderId"):
                        buy_order = Order(
                            order_id=str(result.get("orderId", "")), side="BUY", price=new_buy_price, qty=qty, amount_usdt=self.buy_order_value
                        )
                        self.orders.append(buy_order)
                        log.info(
                            f"🟩 ✅ Created new BUY order after BUY fill: orderId={result.get('orderId')}, price={new_buy_price:.8f}, qty={qty:.8f} (step below executed BUY at {executed_buy_price:.8f})"
                        )
                    elif result:
                        log.warning(f"Failed to create BUY order after BUY fill: API returned result without orderId: {result}")
                    else:
                        log.warning("Failed to create BUY order after BUY fill: API returned None or empty result")
                except Exception as e:
                    log.error(f"Failed to create BUY order after BUY fill at {new_buy_price}: {e}", exc_info=True)
            else:
                log.warning(
                    f"BUY order validation failed: qty={qty:.8f} (min={min_qty:.8f}), notional={buy_notional:.8f} (required={required_notional:.8f})"
                )

        except Exception as e:
            log.error(f"Error creating BUY after BUY: {e}", exc_info=True)

    async def create_sell_after_sell(self, executed_sell_price: Decimal, executed_qty: Decimal):
        """Создать новый SELL ордер после исполнения SELL ордера для продолжения сетки продаж"""
        try:
            # Проверяем баланс базовой валюты
            base_balance = await self.ex.balance(self.base_asset_name)
            open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            locked_base_asset = sum(o.qty for o in open_sell_orders)
            available_base_asset = base_balance - locked_base_asset

            # Используем количество из исполненного SELL для нового SELL
            sell_qty = executed_qty
            if available_base_asset < sell_qty:
                log.debug(f"Insufficient base asset for new SELL after SELL: available={available_base_asset:.8f}, needed={sell_qty:.8f}")
                return

            # Проверяем лимит SELL ордеров: при достижении лимита новые не выставляем
            if len(open_sell_orders) >= config.SELL_ORDERS_COUNT:
                log.debug(f"Maximum SELL orders reached: {len(open_sell_orders)} >= {config.SELL_ORDERS_COUNT}")
                return

            # Рассчитываем цену нового SELL ордера: выше исполненного на шаг сетки
            new_sell_price = executed_sell_price * (Decimal("1") + self.grid_step_pct)

            # Проверяем, не создан ли уже ордер на этой цене
            info = await self.ex.symbol_info(self.symbol)
            tick = info["tickSize"]
            step = info["stepSize"]
            existing_sell = any(o.side == "SELL" and abs(o.price - new_sell_price) < tick and o.status == "open" for o in self.orders)

            if existing_sell:
                log.debug(f"SELL order already exists at price {new_sell_price:.8f}")
                return

            # Получаем информацию о символе
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            # Рассчитываем цену и проверяем количество
            new_sell_price = (new_sell_price // tick) * tick
            sell_qty = (sell_qty // step) * step  # Округляем до шага

            sell_notional = sell_qty * new_sell_price
            required_notional = self.get_required_notional(min_notional)

            # Проверяем ограничения
            if sell_qty >= min_qty and sell_notional >= required_notional and available_base_asset >= sell_qty:
                try:
                    result = await self.ex.place_limit(self.symbol, "SELL", sell_qty, new_sell_price, delay=0.1)
                    if result and result.get("orderId"):
                        sell_order = Order(order_id=str(result.get("orderId", "")), side="SELL", price=new_sell_price, qty=sell_qty)
                        self.orders.append(sell_order)
                        log.info(
                            f"🟥 ✅ Created new SELL order after SELL fill: price={new_sell_price:.8f}, qty={sell_qty:.8f} (step above executed SELL at {executed_sell_price:.8f})"
                        )
                    else:
                        log.warning("Failed to create SELL order after SELL fill: API returned no orderId")
                except Exception as e:
                    log.warning(f"Failed to create SELL order after SELL fill at {new_sell_price}: {e}")
            else:
                log.debug(
                    f"SELL order validation failed: qty={sell_qty:.8f} (min={min_qty:.8f}), notional={sell_notional:.8f} (required={required_notional:.8f}), available={available_base_asset:.8f}"
                )

        except Exception as e:
            log.error(f"Error creating SELL after SELL: {e}")

    async def _rebalancing_apply_after_market_buy(self, market_result: dict, current_price: Decimal) -> bool:
        """После успешного market buy: обновить позицию, перестроить BUY сетку, создать SELL и при необходимости BUY внизу. Возвращает True при успехе."""
        if not market_result or not market_result.get("orderId"):
            return False
        log.info(f"🟩 Market buy successful: orderId={market_result.get('orderId')}")
        await asyncio.sleep(3)
        try:
            order_info = await self.ex.get_order(self.symbol, market_result.get("orderId"))
            if order_info:
                executed_price = Decimal(str(order_info.get("price", current_price)))
                executed_qty = Decimal(str(order_info.get("executedQty", "0")))
                if executed_qty > 0:
                    self.position_manager.add_position(executed_price, executed_qty)
                    log.info(f"Added position from market buy: {executed_qty} {self.base_asset_name} at {executed_price}")
        except Exception as e:
            log.warning(f"Failed to get order info after market buy: {type(e).__name__}")

        await self.ex.invalidate_balance_cache(self.base_asset_name)
        await self.ex.invalidate_balance_cache(self.quote_asset_name)
        await asyncio.sleep(0.5)
        self.base_asset = await self.ex.balance(self.base_asset_name)
        self.current_deposit = await self.ex.balance(self.quote_asset_name)

        try:
            new_current_price = await self.get_current_price()
            log.info(f"[REBALANCING] Got current price: {new_current_price:.8f}")
        except Exception as e:
            log.error(f"[REBALANCING] Failed to get current price: {type(e).__name__}")
            new_current_price = current_price

        log.info(f"[REBALANCING] Rebuilding BUY grid from new price: {new_current_price:.8f}")
        try:
            await self.rebuild_buy_grid_from_price(new_current_price)
            log.info("🟩 [REBALANCING] ✅ BUY grid rebuilt successfully")
        except Exception as e:
            log.error(f"[REBALANCING] Failed to rebuild BUY grid: {type(e).__name__}", exc_info=True)
            log.warning("[REBALANCING] Continuing with SELL grid creation despite BUY grid rebuild issues")

        try:
            sell_created_count = await self.create_sell_grid_only(new_current_price)
            log.info(f"🟥 [REBALANCING] ✅ SELL grid created: {sell_created_count} orders")
        except Exception as e:
            log.error(f"[REBALANCING] Failed to create SELL grid: {type(e).__name__}", exc_info=True)
            sell_created_count = 0

        if sell_created_count and sell_created_count >= 3:
            log.info(f"[REBALANCING] Creating BUY orders at bottom (sell_created_count={sell_created_count})")
            buy_created_count = await self.create_buy_orders_at_bottom(new_current_price)
            log.info(f"🟩 [REBALANCING] Created {buy_created_count} BUY orders at bottom")
        else:
            log.info(f"[REBALANCING] Skipping BUY at bottom: sell_created_count={sell_created_count} (need >=3)")

        await asyncio.to_thread(self.save_state)
        self._cancelled_buy_for_rebalance_prep = False
        log.info("[REBALANCING] ✅ Rebalancing completed successfully")
        return True

    async def check_rebalancing(self, current_price: Decimal):
        """Проверка на ребаланс (все SELL ордера закрыты) - покупаем по рынку, перестраиваем BUY сетку и создаем новые SELL ордера
        BUY ордера подтягиваются к текущей цене (если цена растет, сетка движется вверх)
        """
        try:
            if self.state == BotState.STOPPED:
                return
            open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]

            log.info(f"[REBALANCING_CHECK] Memory: open SELL={len(open_sell_orders)}, open BUY={len(open_buy_orders)}, state={self.state}")

            # Проверяем, что действительно нет SELL ордеров на бирже (не только в памяти)
            try:
                exchange_orders = await self.ex.open_orders(self.symbol)
                exchange_sell_orders = [o for o in exchange_orders if o.get("side") == "SELL"]
                log.info(f"[REBALANCING_CHECK] Exchange: open SELL={len(exchange_sell_orders)}")
            except Exception as e:
                log.error(f"[REBALANCING_CHECK] Failed to get exchange orders: {e}", exc_info=True)
                exchange_sell_orders = []

            if len(open_sell_orders) == 0 and len(exchange_sell_orders) == 0 and self.state == BotState.TRADING:
                log.info("[REBALANCING_CHECK] ✅ Condition met: All SELL orders executed, starting rebalancing")
                log.info(
                    f"Rebalancing: All SELL orders executed (checked both memory and exchange). Rebuilding BUY grid from current price: {current_price:.8f} ({len(open_buy_orders)} old BUY orders will be cancelled)"
                )

                # Инвалидируем кеш баланса перед проверкой, чтобы получить актуальный баланс после возможной отмены BUY ордеров
                await self.ex.invalidate_balance_cache(self.quote_asset_name)
                await asyncio.sleep(0.5)  # Небольшая задержка для обновления баланса на бирже

                # Все SELL закрыты - покупаем по рынку на фиксированную сумму (5 ордеров × размер ордера + 2 USDT запас)
                # Например: 5 × 10 USDT + 2 = 52 USDT, 5 × 20 USDT + 2 = 102 USDT
                market_buy_amount_usdt = (self.buy_order_value * Decimal("5")) + Decimal("2")
                # Используем available_balance для проверки доступного баланса
                quote_available = await self.ex.available_balance(self.quote_asset_name)
                quote_balance = await self.ex.balance(self.quote_asset_name)

                log.info(f"[REBALANCING] Balance check: available={quote_available}, total={quote_balance}, required={market_buy_amount_usdt}")

                if quote_available >= market_buy_amount_usdt:
                    try:
                        log.info(
                            f"All SELL orders executed. Performing market buy for new SELL orders: {market_buy_amount_usdt} {self.quote_asset_name} (5 × {self.buy_order_value} + 2 USDT reserve)"
                        )

                        # Делаем рыночную покупку на сумму (используя quoteOrderQty)
                        market_result = await self.ex.place_market(self.symbol, "BUY", qty=Decimal("0"), quote_order_qty=market_buy_amount_usdt)

                        if market_result and market_result.get("orderId"):
                            await self._rebalancing_apply_after_market_buy(market_result, current_price)
                        else:
                            log.warning(f"[REBALANCING] Market buy failed or no orderId in result: {market_result}")
                    except Exception as e:
                        error_msg = str(e)
                        log.error(f"[REBALANCING] Exception during market buy: {error_msg}", exc_info=True)
                        if "Permission denied" in error_msg or "Spot Trading permission" in error_msg:
                            raise
                        # Если недостаточно баланса, пытаемся использовать доступный баланс (минус небольшой запас)
                        elif "balance not enough" in error_msg.lower() or "insufficient" in error_msg.lower():
                            log.warning(
                                f"[REBALANCING] Insufficient balance for market buy: available={quote_available:.2f}, required={market_buy_amount_usdt:.2f}"
                            )
                            # Пытаемся использовать доступный баланс (минус 1 USDT запас)
                            if quote_available > Decimal("1"):
                                adjusted_amount = quote_available - Decimal("1")
                                log.info(
                                    f"[REBALANCING] Retrying with adjusted amount: {adjusted_amount:.2f} {self.quote_asset_name} (available - 1 USDT reserve)"
                                )
                                try:
                                    market_result = await self.ex.place_market(self.symbol, "BUY", qty=Decimal("0"), quote_order_qty=adjusted_amount)
                                    if market_result and market_result.get("orderId"):
                                        await self._rebalancing_apply_after_market_buy(market_result, current_price)
                                    else:
                                        log.warning(f"[REBALANCING] Market buy with adjusted amount failed: {market_result}")
                                except Exception as e2:
                                    log.error(f"[REBALANCING] Failed to retry with adjusted amount: {e2}", exc_info=True)
                                    log.warning(
                                        f"[REBALANCING] Cannot create new grid - insufficient balance. Available: {quote_available:.2f}, Required: {market_buy_amount_usdt:.2f}"
                                    )
                            else:
                                log.warning(
                                    f"[REBALANCING] Cannot create new grid - insufficient balance. Available: {quote_available:.2f}, Required: {market_buy_amount_usdt:.2f}"
                                )
                        else:
                            log.warning(f"[REBALANCING] Failed to perform market buy for rebalancing: {e}")
                else:
                    # Недостаточно баланса для полного маркет-бая, но пытаемся использовать доступный баланс
                    log.warning(
                        f"[REBALANCING] Insufficient available balance for full market buy: available={quote_available:.2f}, total={quote_balance:.2f}, required={market_buy_amount_usdt:.2f}"
                    )

                    # Если есть хотя бы минимальный баланс (больше 1 USDT), используем его для создания сетки
                    if quote_available > Decimal("1"):
                        adjusted_amount = quote_available - Decimal("1")  # Оставляем 1 USDT резерв
                        log.info(
                            f"[REBALANCING] Attempting market buy with available balance: {adjusted_amount:.2f} {self.quote_asset_name} (available - 1 USDT reserve)"
                        )

                        try:
                            market_result = await self.ex.place_market(self.symbol, "BUY", qty=Decimal("0"), quote_order_qty=adjusted_amount)

                            if market_result and market_result.get("orderId"):
                                await self._rebalancing_apply_after_market_buy(market_result, current_price)
                            else:
                                log.warning(f"[REBALANCING] Market buy with available balance failed: {market_result}")
                        except Exception as e2:
                            error_msg = str(e2)
                            log.error(f"[REBALANCING] Failed to perform market buy with available balance: {error_msg}", exc_info=True)
                            if "balance not enough" in error_msg.lower() or "insufficient" in error_msg.lower():
                                log.warning(
                                    f"[REBALANCING] Cannot create new grid - insufficient balance even for available amount. Available: {quote_available:.2f}"
                                )
                            else:
                                log.warning(f"[REBALANCING] Market buy failed with error: {error_msg}")
                    else:
                        log.warning(
                            f"[REBALANCING] Cannot create new grid - insufficient balance. Available: {quote_available:.2f} (need > 1 USDT), Required: {market_buy_amount_usdt:.2f}"
                        )
            else:
                log.debug(
                    f"[REBALANCING_CHECK] Condition not met: open_sell_memory={len(open_sell_orders)}, open_sell_exchange={len(exchange_sell_orders)}, state={self.state}"
                )

        except Exception as e:
            log.error(f"[REBALANCING] Error checking rebalancing: {e}", exc_info=True)

    async def check_rebalancing_after_all_buy_filled(self, current_price: Decimal):
        """Проверка ребалансировки после исполнения всех BUY ордеров
        Создает SELL сетку из 3 ордеров от средней цены покупки (VWAP)
        """
        try:
            # Проверяем, что действительно нет BUY ордеров на бирже (не только в памяти)
            exchange_orders = await self.ex.open_orders(self.symbol)
            exchange_buy_orders = [o for o in exchange_orders if o.get("side") == "BUY"]

            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]

            # Если все BUY ордера исполнены и есть позиции (VWAP > 0), создаем SELL сетку от VWAP
            if len(open_buy_orders) == 0 and len(exchange_buy_orders) == 0 and self.state == BotState.TRADING:
                # Рассчитываем VWAP (среднюю цену покупки)
                vwap = await self.calculate_vwap()

                if vwap > 0:
                    log.info(f"[REBALANCING_AFTER_BUY] All BUY orders executed. VWAP: {vwap:.8f}. Creating SELL grid from VWAP")

                    # Используем существующую функцию для создания SELL сетки от VWAP
                    result = await self.create_critical_sell_grid()

                    if result["created_count"] > 0:
                        log.info(f"🟥 [REBALANCING_AFTER_BUY] ✅ Successfully created {result['created_count']} SELL orders from VWAP {vwap:.8f}")
                    else:
                        log.warning(f"[REBALANCING_AFTER_BUY] ⚠️ Failed to create SELL orders from VWAP. Created: {result['created_count']}")
                else:
                    log.warning("[REBALANCING_AFTER_BUY] VWAP is 0, cannot create SELL grid from average price")

        except Exception as e:
            log.error(f"[REBALANCING_AFTER_BUY] Error checking rebalancing after all BUY filled: {e}", exc_info=True)

    async def rebuild_buy_grid_from_price(self, price: Decimal):
        """Перестроить BUY сетку от текущей цены (подтянуть BUY ордера к цене)
        Используется при ребалансировке, когда все SELL ордера исполнены и цена выросла
        """
        try:
            # Отменяем все существующие BUY ордера
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            log.info(f"Rebuilding BUY grid: cancelling {len(open_buy_orders)} old BUY orders")

            for order in list(open_buy_orders):
                try:
                    await self.ex._request("GET", "/openApi/spot/v1/trade/cancel", {"symbol": self.symbol, "orderId": order.order_id})
                except Exception as e:
                    log.warning(f"Failed to cancel BUY order {order.order_id}: {e}")
                self.orders.remove(order)

            await asyncio.sleep(1)  # Небольшая задержка после отмены

            # Получаем информацию о символе
            info = await self.ex.symbol_info(self.symbol)
            step = info.get("stepSize", Decimal("0.000001"))
            tick = info.get("tickSize", Decimal("0.01"))
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            buy_count = await self.calculate_active_buy_orders_count()

            if buy_count <= 0:
                log.warning(f"Cannot rebuild BUY grid: buy_count is zero or negative: {buy_count}")
                return

            log.info(f"Rebuilding BUY grid: creating {buy_count} BUY orders from price {price:.8f}")

            # Создаем новые BUY ордера от текущей цены
            current_buy_price = price * (Decimal("1") - self.grid_step_pct)
            created_buy_orders = 0

            for i in range(buy_count):
                try:
                    # Проверяем что цена не стала слишком маленькой
                    if current_buy_price < price * Decimal("0.1"):
                        log.warning(
                            f"[REBUILD_BUY] Buy price too low ({current_buy_price}), stopping BUY orders creation at order {i + 1}/{buy_count}"
                        )
                        break

                    level_price = (current_buy_price // tick) * tick

                    # Проверяем что level_price больше 0
                    if level_price <= 0 or level_price < tick:
                        log.warning(f"[REBUILD_BUY] Order {i + 1}: level_price is zero or too small: {level_price}, skipping")
                        current_buy_price = current_buy_price * (Decimal("1") - self.grid_step_pct)
                        continue

                    # Проверяем баланс
                    try:
                        current_balance = await self.ex.balance(self.quote_asset_name)
                        if current_balance < self.buy_order_value:
                            log.info(
                                f"[REBUILD_BUY] Order {i + 1}: Insufficient balance for more BUY orders: {current_balance} < {self.buy_order_value}"
                            )
                            break
                    except Exception as e:
                        log.error(f"[REBUILD_BUY] Failed to get balance for order {i + 1}: {e}")
                        break

                    # Рассчитываем количество и номинал
                    qty = (self.buy_order_value / level_price).quantize(step, rounding=ROUND_DOWN)
                    notional = qty * level_price
                    required_notional = self.get_required_notional(min_notional)

                    # Проверяем условия
                    if qty >= min_qty and notional >= required_notional:
                        try:
                            log.debug(f"[REBUILD_BUY] Placing order {i + 1}/{buy_count} at {level_price:.8f}, qty={qty:.8f}")
                            result = await self.ex.place_limit(self.symbol, "BUY", qty, level_price, delay=0.1)
                            if result and result.get("orderId"):
                                order = Order(
                                    order_id=str(result.get("orderId", "")), side="BUY", price=level_price, qty=qty, amount_usdt=self.buy_order_value
                                )
                                self.orders.append(order)
                                created_buy_orders += 1
                                log.info(f"🟩 [REBUILD_BUY] Order {i + 1}/{buy_count}: ✅ Placed at {level_price:.8f}, qty={qty:.8f}")
                                await asyncio.sleep(0.2)
                            else:
                                log.warning(f"[REBUILD_BUY] Order {i + 1}: API returned no orderId: {result}")
                                # Продолжаем, не прерываем цикл
                        except Exception as e:
                            log.error(f"[REBUILD_BUY] Failed to place order {i + 1} at {level_price}: {e}", exc_info=True)
                            # Продолжаем создание ордеров, не прерываем весь процесс
                            await asyncio.sleep(0.5)  # Небольшая задержка перед следующей попыткой
                    else:
                        log.debug(f"[REBUILD_BUY] Order {i + 1}: validation failed (qty={qty:.8f}, notional={notional:.8f})")

                    current_buy_price = current_buy_price * (Decimal("1") - self.grid_step_pct)

                    # Логируем прогресс каждые 10 ордеров
                    if (i + 1) % 10 == 0:
                        log.info(f"[REBUILD_BUY] Progress: {i + 1}/{buy_count} orders processed, {created_buy_orders} created")

                except Exception as e:
                    log.error(f"[REBUILD_BUY] Unexpected error at order {i + 1}: {e}", exc_info=True)
                    # Продолжаем, не прерываем весь процесс

            log.info(f"🟩 [REBUILD_BUY] ✅ BUY grid rebuilt: {created_buy_orders}/{buy_count} orders created from price {price:.8f}")

        except Exception as e:
            log.error(f"Error rebuilding BUY grid: {e}")

    async def check_critical_situation(self):
        """Проверка критической ситуации: все BUY исполнены, но SELL не созданы и рыночная покупка не сработала"""
        try:
            if self.state != BotState.TRADING:
                return

            # Проверяем количество открытых и исполненных ордеров
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            executed_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "filled"]

            # Критическая ситуация: все BUY ордера исполнены, но SELL ордера не созданы
            if len(open_buy_orders) == 0 and len(executed_buy_orders) > 0 and len(open_sell_orders) == 0:
                current_base_balance = await self.ex.balance(self.base_asset_name)
                quote_balance = await self.ex.balance(self.quote_asset_name)
                market_buy_amount_usdt = (self.buy_order_value * Decimal("5")) + Decimal("2")  # 5 ордеров + 2 USDT запас

                # Проверяем баланс для создания SELL ордеров или рыночной покупки
                if current_base_balance <= 0 and quote_balance < market_buy_amount_usdt:
                    # Критическая ситуация: все BUY исполнены, но баланса недостаточно для SELL ордеров
                    error_msg = (
                        f"🚨 **КРИТИЧЕСКАЯ СИТУАЦИЯ**\n\n"
                        f"✅ Все BUY ордера исполнены ({len(executed_buy_orders)} ордеров)\n"
                        f"❌ SELL ордера не созданы\n"
                        f"❌ Рыночная покупка не сработала (недостаточно баланса)\n\n"
                        f"Текущий баланс:\n"
                        f"{self.quote_asset_name}: {quote_balance:.2f} (требуется: {market_buy_amount_usdt:.2f})\n"
                        f"{self.base_asset_name}: {current_base_balance:.8f}\n\n"
                        f"Пополните баланс или вручную создайте SELL ордера."
                    )

                    log.error(error_msg)
                    if self.telegram_notifier:
                        try:
                            await self.telegram_notifier(error_msg)
                        except Exception as e:
                            log.warning(f"Failed to send critical error notification: {e}")

        except Exception as e:
            log.error(f"Error checking critical situation: {e}", exc_info=True)

    async def create_sell_grid_only(self, price: Decimal):
        """Создать только SELL ордера (5 штук) в плюс на шаг сетки от текущей цены
        ВАЖНО: Не отменяет и не трогает BUY ордера!
        """
        try:
            # Отменяем только существующие SELL ордера (не BUY!)
            for order in list(self.orders):
                if order.side == "SELL" and order.status == "open":
                    try:
                        await self.ex._request("GET", "/openApi/spot/v1/trade/cancel", {"symbol": self.symbol, "orderId": order.order_id})
                    except Exception:
                        pass
                    self.orders.remove(order)

            current_base_balance = await self.ex.balance(self.base_asset_name)
            if current_base_balance == 0:
                log.warning("No base asset available for SELL orders")
                return 0

            info = await self.ex.symbol_info(self.symbol)
            step = info["stepSize"]
            tick = info["tickSize"]
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            # После отмены старых SELL, проверяем доступный баланс
            open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]
            locked_base_asset = sum(o.qty for o in open_sell_orders)
            available_base_asset = current_base_balance - locked_base_asset

            if available_base_asset <= 0:
                log.warning("No available base asset for SELL orders (all locked)")
                return 0

            # Первый SELL ордер в плюс на шаг сетки от текущей цены
            current_sell_price = price * (Decimal("1") + self.grid_step_pct)

            # Рассчитываем, сколько ордеров можно создать с учетом минимального объема
            # Используем минимальную цену для расчета (первая цена в сетке)
            first_price = (current_sell_price // tick) * tick
            if first_price <= 0:
                log.warning(f"First SELL price is zero or negative: {first_price}, cannot create SELL grid")
                return 0

            # Рассчитываем минимальный объем для одного ордера (с учетом min_qty и min_notional)
            sell_required_notional = self.get_required_notional(min_notional)
            min_qty_for_price = max(min_qty, sell_required_notional / first_price if first_price > 0 else min_qty)

            # Определяем максимальное количество ордеров, которое можно создать
            max_possible_orders = int(available_base_asset / min_qty_for_price)
            orders_to_create = min(config.SELL_ORDERS_COUNT, max_possible_orders)

            if orders_to_create <= 0:
                log.warning(
                    f"Cannot create SELL orders: available_base={available_base_asset:.8f}, min_qty_per_order={min_qty_for_price:.8f}, min_qty={min_qty:.8f}, min_notional={sell_required_notional:.8f}"
                )
                return 0

            if orders_to_create < config.SELL_ORDERS_COUNT:
                log.warning(
                    f"Available base asset ({available_base_asset:.8f}) is insufficient for {config.SELL_ORDERS_COUNT} SELL orders. Will create {orders_to_create} orders instead (min_qty_per_order={min_qty_for_price:.8f})"
                )

            sell_qty_per_order = available_base_asset / Decimal(orders_to_create)

            log.info(
                f"Creating {orders_to_create} SELL orders starting from {current_sell_price:.8f} (price + {self.grid_step_pct * 100:.2f}%), available_base={available_base_asset:.8f}, qty_per_order={sell_qty_per_order:.8f}"
            )

            created_count = 0
            for i in range(orders_to_create):
                level_price = (current_sell_price // tick) * tick

                if level_price <= 0:
                    log.warning(f"SELL level_price is zero or negative: {level_price}, skipping")
                    current_sell_price = current_sell_price * (Decimal("1") + self.grid_step_pct)
                    continue

                qty = sell_qty_per_order.quantize(step, rounding=ROUND_DOWN)
                sell_notional = qty * level_price
                sell_required_notional = self.get_required_notional(min_notional)

                # Дополнительная проверка: если qty меньше min_qty, пропускаем этот ордер
                if qty < min_qty:
                    log.warning(f"SELL order {i + 1} qty too small after quantization: {qty:.8f} < {min_qty:.8f}, skipping")
                    current_sell_price = current_sell_price * (Decimal("1") + self.grid_step_pct)
                    continue

                if qty >= min_qty and sell_notional >= sell_required_notional:
                    try:
                        result = await self.ex.place_limit(self.symbol, "SELL", qty, level_price, delay=0.1)
                        if result:
                            order = Order(order_id=str(result.get("orderId", "")), side="SELL", price=level_price, qty=qty)
                            self.orders.append(order)
                            created_count += 1
                            log.info(f"🟥 SELL order {i + 1}: ✅ Placed at {level_price:.8f}, qty={qty:.8f}")
                            await asyncio.sleep(0.2)
                    except Exception as e:
                        log.warning(f"Failed to place SELL order at {level_price}: {e}")

                current_sell_price = current_sell_price * (Decimal("1") + self.grid_step_pct)

            log.info(f"🟥 Created {created_count} SELL orders out of {orders_to_create} attempted")

            if created_count == 0:
                log.error(
                    f"Failed to create any SELL orders! Available base: {available_base_asset:.8f}, min_qty: {min_qty:.8f}, min_notional: {sell_required_notional:.8f}"
                )

            return created_count
        except Exception as e:
            log.error(f"Error creating SELL grid: {e}", exc_info=True)
            return 0

    async def cancel_last_n_buy_orders(self, n: int):
        """Отменить последние N BUY ордеров (самые низкие по цене) для освобождения USDT"""
        try:
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]

            if len(open_buy_orders) == 0:
                log.warning("[CANCEL_LAST_BUY] No open BUY orders to cancel")
                return 0

            # Сортируем по цене (от меньшей к большей) - последние N это самые низкие цены
            open_buy_orders_sorted = sorted(open_buy_orders, key=lambda x: x.price)
            orders_to_cancel = open_buy_orders_sorted[:n]  # Берем первые N (самые низкие)

            log.info(
                f"[CANCEL_LAST_BUY] Cancelling {len(orders_to_cancel)} last BUY orders (lowest prices): {[f'{o.price:.8f}' for o in orders_to_cancel]}"
            )

            canceled_count = 0
            for order in orders_to_cancel:
                try:
                    await self.ex._request("GET", "/openApi/spot/v1/trade/cancel", {"symbol": self.symbol, "orderId": order.order_id})
                    if order in self.orders:
                        self.orders.remove(order)
                    canceled_count += 1
                    log.info(f"[CANCEL_LAST_BUY] ✅ Cancelled BUY order {order.order_id} at {order.price:.8f}")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    log.warning(f"[CANCEL_LAST_BUY] Failed to cancel BUY order {order.order_id}: {e}")

            log.info(f"[CANCEL_LAST_BUY] Successfully cancelled {canceled_count} out of {len(orders_to_cancel)} BUY orders")
            return canceled_count
        except Exception as e:
            log.error(f"[CANCEL_LAST_BUY] Error cancelling last BUY orders: {e}", exc_info=True)
            return 0

    async def check_protection_add_five_buy_when_three_left(self) -> int:
        """Защита: при падении цены, если осталось 3 открытых BUY и сетка большая (открытых ордеров > порога),
        добавить до 5 BUY внизу сетки. Если баланса не хватает — ставить сколько хватает.
        Порог: при шаге 1.5% — 62, при шаге 0.75% — 127."""
        try:
            open_buy = [o for o in self.orders if o.side == "BUY" and o.status == "open"]
            open_buy_count = len(open_buy)
            if open_buy_count > 3:
                return 0
            total_open = len([o for o in self.orders if o.status == "open"])
            threshold = self.get_min_open_orders_for_protection()
            if total_open <= threshold:
                log.debug(
                    "[PROTECTION_3_BUY] Skip: total_open=%s <= threshold=%s",
                    total_open,
                    threshold,
                )
                return 0
            current_price = await self.get_current_price()
            log.info(
                "[PROTECTION_3_BUY] open_buy=%s, total_open=%s > threshold=%s -> adding up to 5 BUY at bottom",
                open_buy_count,
                total_open,
                threshold,
            )
            return await self.create_buy_orders_at_bottom(current_price)
        except Exception as e:
            log.error("[PROTECTION_3_BUY] Error: %s", e, exc_info=True)
            return 0

    async def create_buy_orders_at_bottom(self, current_price: Decimal):
        """Создать BUY ордера внизу сетки (ниже всех существующих BUY) на освобожденный баланс"""
        try:
            info = await self.ex.symbol_info(self.symbol)
            step = info.get("stepSize", Decimal("0.000001"))
            tick = info.get("tickSize", Decimal("0.01"))
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            # Находим самую низкую цену среди существующих BUY ордеров
            open_buy_orders = [o for o in self.orders if o.side == "BUY" and o.status == "open"]

            if open_buy_orders:
                lowest_buy_price = min(o.price for o in open_buy_orders)
                start_price = lowest_buy_price * (Decimal("1") - self.grid_step_pct)
                log.info(f"[CREATE_BUY_AT_BOTTOM] Starting from price {start_price:.8f} (lowest existing BUY: {lowest_buy_price:.8f})")
            else:
                # Если нет BUY ордеров, начинаем от текущей цены минус шаг
                start_price = current_price * (Decimal("1") - self.grid_step_pct)
                log.info(f"[CREATE_BUY_AT_BOTTOM] No existing BUY orders, starting from price {start_price:.8f} (current_price: {current_price:.8f})")

            # Проверяем доступный баланс (используем available_balance для точности)
            quote_available = await self.ex.available_balance(self.quote_asset_name)
            quote_balance = await self.ex.balance(self.quote_asset_name)
            log.info(
                f"[CREATE_BUY_AT_BOTTOM] Balance check: available={quote_available:.2f}, total={quote_balance:.2f}, order_value={self.buy_order_value:.2f}"
            )

            if quote_available < self.buy_order_value:
                log.warning(f"[CREATE_BUY_AT_BOTTOM] Insufficient available balance: {quote_available:.2f} < {self.buy_order_value:.2f}")
                return 0

            # Проверяем лимит BUY ордеров: при достижении лимита новые не выставляем
            max_buy_orders = self.get_max_buy_orders()
            if len(open_buy_orders) >= max_buy_orders:
                log.debug(f"[CREATE_BUY_AT_BOTTOM] BUY limit reached: {len(open_buy_orders)} >= {max_buy_orders}")
                return 0

            # Рассчитываем, сколько BUY ордеров можно создать (с учётом лимита и баланса)
            max_orders = min(max_buy_orders - len(open_buy_orders), 5, int(quote_available / self.buy_order_value))

            created_count = 0
            current_buy_price = start_price

            for i in range(max_orders):
                # Проверяем лимит перед каждым ордером (на случай гонок)
                current_open_buy = len([o for o in self.orders if o.side == "BUY" and o.status == "open"])
                if current_open_buy >= max_buy_orders:
                    log.debug(f"[CREATE_BUY_AT_BOTTOM] BUY limit reached during loop: {current_open_buy} >= {max_buy_orders}")
                    break
                # Проверяем доступный баланс перед каждым ордером
                current_available = await self.ex.available_balance(self.quote_asset_name)
                if current_available < self.buy_order_value:
                    log.info(
                        f"[CREATE_BUY_AT_BOTTOM] Insufficient available balance for more orders: {current_available:.2f} < {self.buy_order_value:.2f}"
                    )
                    break

                level_price = (current_buy_price // tick) * tick

                if level_price <= 0:
                    log.warning(f"[CREATE_BUY_AT_BOTTOM] Price too low: {level_price}, stopping")
                    break

                qty = (self.buy_order_value / level_price).quantize(step, rounding=ROUND_DOWN)
                notional = qty * level_price
                required_notional = self.get_required_notional(min_notional)

                if qty >= min_qty and notional >= required_notional:
                    try:
                        result = await self.ex.place_limit(self.symbol, "BUY", qty, level_price, delay=0.1)
                        if result and result.get("orderId"):
                            order = Order(
                                order_id=str(result.get("orderId", "")), side="BUY", price=level_price, qty=qty, amount_usdt=self.buy_order_value
                            )
                            self.orders.append(order)
                            created_count += 1
                            log.info(f"🟩 [CREATE_BUY_AT_BOTTOM] ✅ Created BUY order {i + 1} at {level_price:.8f}, qty={qty:.8f}")
                            await asyncio.sleep(0.2)
                    except Exception as e:
                        log.warning(f"[CREATE_BUY_AT_BOTTOM] Failed to place BUY order at {level_price}: {e}")
                else:
                    log.warning(
                        f"[CREATE_BUY_AT_BOTTOM] Validation failed: qty={qty:.8f} (min={min_qty:.8f}), notional={notional:.8f} (required={required_notional:.8f})"
                    )

                current_buy_price = current_buy_price * (Decimal("1") - self.grid_step_pct)

            log.info(f"🟩 [CREATE_BUY_AT_BOTTOM] Created {created_count} BUY orders at bottom of grid")
            return created_count
        except Exception as e:
            log.error(f"[CREATE_BUY_AT_BOTTOM] Error creating BUY orders at bottom: {e}", exc_info=True)
            return 0

    async def rebalance_buy_grid_from_sell(self, current_price: Decimal):
        """Перестроить BUY сетку от текущей цены"""
        try:
            # Отменяем все существующие BUY ордера
            for order in list(self.orders):
                if order.side == "BUY" and order.status == "open":
                    try:
                        await self.ex._request("GET", "/openApi/spot/v1/trade/cancel", {"symbol": self.symbol, "orderId": order.order_id})
                    except Exception:
                        pass
                    self.orders.remove(order)

            buy_count = await self.calculate_active_buy_orders_count()

            info = await self.ex.symbol_info(self.symbol)
            step = info["stepSize"]
            tick = info["tickSize"]

            open_sell_orders = [o for o in self.orders if o.side == "SELL" and o.status == "open"]

            if open_sell_orders:
                lowest_sell_price = min(o.price for o in open_sell_orders)
                start_buy_price = lowest_sell_price * (Decimal("1") - self.grid_step_pct)
            else:
                start_buy_price = current_price * (Decimal("1") - self.grid_step_pct)

            current_buy_price = start_buy_price

            for i in range(buy_count):
                level_price = (current_buy_price // tick) * tick

                current_balance = await self.ex.balance(self.quote_asset_name)
                if current_balance < self.buy_order_value:
                    break

                qty = (self.buy_order_value / level_price).quantize(step, rounding=ROUND_DOWN)

                buy_create_notional = qty * level_price
                buy_create_required_notional = self.get_required_notional(info.get("minNotional", Decimal("0")))
                if qty >= info.get("minQty", Decimal("0")) and buy_create_notional >= buy_create_required_notional:
                    try:
                        result = await self.ex.place_limit(self.symbol, "BUY", qty, level_price, delay=0.1)
                        if result:
                            order = Order(
                                order_id=str(result.get("orderId", "")), side="BUY", price=level_price, qty=qty, amount_usdt=self.buy_order_value
                            )
                            self.orders.append(order)
                            await asyncio.sleep(0.2)
                    except Exception as e:
                        log.warning(f"Failed to place rebalanced BUY order at {level_price}: {e}")

                current_buy_price = current_buy_price * (Decimal("1") - self.grid_step_pct)

            await asyncio.to_thread(self.save_state)

        except Exception as e:
            log.error(f"Error rebalancing BUY grid: {e}")

    def _deduplicate_orders(self):
        """Удалить дубликаты ордеров по order_id (оставляем первое вхождение)."""
        deduplicate_orders(self.orders, self.user_id, self.symbol)

    async def sync_orders_from_exchange(self):
        """Синхронизировать список ордеров с биржей"""
        try:
            self._deduplicate_orders()
            exchange_orders = await self.ex.open_orders(self.symbol)
            exchange_order_ids = {str(o["orderId"]) for o in exchange_orders}

            # Проверяем ордера в памяти, которые должны быть на бирже, но их там нет
            open_orders_before_sync = [o for o in self.orders if o.status == "open"]
            current_time = time.time()
            # Не удаляем ордера, которые были созданы менее 3 секунд назад (API может еще не показать их)
            missing_on_exchange = [
                o for o in open_orders_before_sync if o.order_id not in exchange_order_ids and (current_time - getattr(o, "created_at", 0)) > 3.0
            ]
            if missing_on_exchange:
                log.warning(
                    f"[SYNC] Found {len(missing_on_exchange)} orders in memory marked as 'open' but not on exchange: {[o.order_id for o in missing_on_exchange]}"
                )
                # Запрашиваем статус на бирже: если FILLED — обрабатываем как исполнение (создаём замену), иначе просто удалим
                for order in missing_on_exchange:
                    try:
                        order_info = await self.ex.get_order(self.symbol, order.order_id)
                        if not order_info:
                            continue
                        status_raw = str(order_info.get("status", "")).upper()
                        if status_raw in ("FILLED", "CLOSED"):
                            order.status = "filled"
                            order.executed_qty = Decimal(str(order_info.get("executedQty", order.qty))) if order_info.get("executedQty") is not None else order.qty
                            exec_price = Decimal(str(order_info.get("price", order.price))) if order_info.get("price") else order.price
                            log.info(
                                "[SYNC] %s | Order %s (%s) is FILLED on exchange, processing as fill",
                                self._log_prefix(), order.order_id, order.side,
                            )
                            if order.side == "BUY":
                                await self.handle_buy_filled(order, exec_price)
                            elif order.side == "SELL":
                                await self.handle_sell_filled(order, exec_price)
                        else:
                            log.info("[SYNC] %s | Order %s status on exchange: %s (will be removed)", self._log_prefix(), order.order_id, status_raw)
                    except Exception as e:
                        log.warning("[SYNC] %s | Failed to get order %s status: %s (will be removed)", self._log_prefix(), order.order_id, e)

            # Удаляем только ордера, которых нет на бирже И которые были созданы более 3 секунд назад (не обработанные как FILLED выше остаются с status=open и будут удалены)
            self.orders = [
                o
                for o in self.orders
                if o.order_id in exchange_order_ids or o.status != "open" or (current_time - getattr(o, "created_at", 0)) <= 3.0
            ]

            our_order_ids = {o.order_id for o in self.orders}
            added_count = 0
            for ex_order in exchange_orders:
                order_id = str(ex_order["orderId"])
                if order_id not in our_order_ids:
                    order = Order(
                        order_id=order_id,
                        side=ex_order.get("side", ""),
                        price=Decimal(str(ex_order.get("price", "0"))),
                        qty=Decimal(str(ex_order.get("origQty", "0"))),
                        status="open",
                    )
                    # Для ордеров, загруженных с биржи, устанавливаем created_at = 0, чтобы они не удалялись при синхронизации
                    order.created_at = 0
                    self.orders.append(order)
                    added_count += 1
                    log.info(f"🟩 [SYNC] Added missing order from exchange: {order_id} ({order.side}) at {order.price:.8f}")

            if added_count > 0:
                log.info(f"[SYNC] Added {added_count} missing orders from exchange")
        except Exception as e:
            err_msg = str(e)
            if "Circuit breaker" in err_msg or "circuit breaker" in err_msg.lower():
                log.warning(f"Sync orders skipped (Circuit breaker): {e}")
            else:
                log.error(f"Error syncing orders: {e}")

    def _log_prefix(self):
        """Префикс для лога: user_id и символ (удобно при нескольких пользователях)."""
        return f"user_id={self.user_id} {self.symbol}"

    async def check_orders(self):
        """Проверить исполнение ордеров (используем подход сравнения: ордер был в памяти как open, но его нет в открытых на бирже)"""
        try:
            if self.state == BotState.STOPPED:
                return
            if self.state == BotState.PAUSED:
                log.info("[CHECK_ORDERS] %s | Bot is PAUSED, skipping check", self._log_prefix())
                return

            total_orders = len(self.orders)
            filled_orders_count = len([o for o in self.orders if o.status == "filled"])
            open_orders_count = len([o for o in self.orders if o.status == "open"])
            buy_orders_count = len([o for o in self.orders if o.side == "BUY" and o.status == "open"])
            sell_orders_count = len([o for o in self.orders if o.side == "SELL" and o.status == "open"])

            log.info(
                "[CHECK_ORDERS] %s | Starting order check: total=%s (open=%s, filled=%s, BUY=%s, SELL=%s)",
                self._log_prefix(), total_orders, open_orders_count, filled_orders_count, buy_orders_count, sell_orders_count
            )

            self.current_deposit = await self.ex.balance(config.QUOTE)
            self.base_asset = await self.ex.balance(config.BASE)

            # Получаем текущий список открытых ордеров на бирже
            exchange_orders = await self.ex.open_orders(self.symbol)
            exchange_order_ids = {str(o["orderId"]) for o in exchange_orders}
            exchange_buy_count = len([o for o in exchange_orders if o.get("side") == "BUY"])
            exchange_sell_count = len([o for o in exchange_orders if o.get("side") == "SELL"])

            log.info("[CHECK_ORDERS] %s | Found %s open orders on exchange (BUY=%s, SELL=%s)", self._log_prefix(), len(exchange_orders), exchange_buy_count, exchange_sell_count)

            # Проверяем ордера в памяти
            open_orders_in_memory = [o for o in self.orders if o.status == "open"]
            log.info("[CHECK_ORDERS] %s | Checking %s open orders in memory", self._log_prefix(), len(open_orders_in_memory))

            # Проверяем расхождение между памятью и биржей
            if len(open_orders_in_memory) != len(exchange_orders):
                log.warning(
                    "[CHECK_ORDERS] %s | Mismatch: %s in memory vs %s on exchange (diff=%s)",
                    self._log_prefix(), len(open_orders_in_memory), len(exchange_orders), len(open_orders_in_memory) - len(exchange_orders)
                )

            # Ищем ордера, которые есть в памяти как open, но их нет в открытых на бирже - это исполненные ордера
            filled_orders = []
            for order in open_orders_in_memory:
                if order.order_id not in exchange_order_ids:
                    # Ордер был открыт в памяти, но его нет в открытых на бирже - значит он исполнился
                    filled_orders.append(order)
                    fill_emoji = "🟥" if order.side == "SELL" else "🟩"
                    log.info("%s ✅ [CHECK_ORDERS] %s | Order %s (%s) FILLED: qty=%s, price=%s", fill_emoji, self._log_prefix(), order.order_id, order.side, order.qty, order.price)

            if filled_orders:
                log.info("🟩 [CHECK_ORDERS] %s | Found %s filled orders", self._log_prefix(), len(filled_orders))

            # Обрабатываем каждый исполненный ордер
            for order in filled_orders:
                order.status = "filled"
                order.executed_qty = order.qty

                # Обрабатываем исполнение в зависимости от типа
                try:
                    if order.side == "BUY":
                        await self.handle_buy_filled(order, order.price)
                    elif order.side == "SELL":
                        await self.handle_sell_filled(order, order.price)
                except Exception as e:
                    log.error("[CHECK_ORDERS] %s | Error handling %s fill for order %s: %s", self._log_prefix(), order.side, order.order_id, e, exc_info=True)

        except Exception as e:
            err_msg = str(e)
            if "Circuit breaker" in err_msg or "circuit breaker" in err_msg.lower():
                log.warning("[CHECK_ORDERS] %s | Skipped (Circuit breaker OPEN)", self._log_prefix())
            else:
                log.error("[CHECK_ORDERS] %s | Error checking orders: %s", self._log_prefix(), e, exc_info=True)

    async def main_loop(self):
        """Основной цикл бота"""
        log.info("[MAIN_LOOP] %s | Starting main loop, current state: %s", self._log_prefix(), self.state)
        while True:
            try:
                if self.state == BotState.INITIALIZING:
                    log.info("[MAIN_LOOP] %s | State: INITIALIZING - checking for existing orders", self._log_prefix())
                    # Инициализация - проверяем, есть ли уже открытые ордера
                    try:
                        # Сначала загружаем состояние (в потоке, чтобы не блокировать event loop)
                        await asyncio.to_thread(self.load_state)

                        price = await self.get_current_price()
                        self.current_deposit = await self.ex.balance(self.quote_asset_name)
                        self.base_asset = await self.ex.balance(self.base_asset_name)

                        log.info("[MAIN_LOOP] %s | Checking for existing orders on symbol: %s", self._log_prefix(), self.symbol)
                        log.info("[MAIN_LOOP] %s | Loaded state: symbol=%s, orders_in_memory=%s", self._log_prefix(), self.symbol, len(self.orders))

                        # Проверяем, есть ли уже открытые ордера на бирже
                        try:
                            exchange_orders = await self.ex.open_orders(self.symbol)
                        except Exception as e:
                            err_msg = str(e)
                            if "Circuit breaker" in err_msg or "circuit breaker" in err_msg.lower():
                                log.warning("[MAIN_LOOP] %s | Skipped getting orders (Circuit breaker OPEN)", self._log_prefix())
                            else:
                                log.error("[MAIN_LOOP] %s | Error getting orders from exchange: %s", self._log_prefix(), e)
                            exchange_orders = []

                        log.info("[MAIN_LOOP] %s | Found %s open orders on exchange for %s", self._log_prefix(), len(exchange_orders) if exchange_orders else 0, self.symbol)

                        if exchange_orders and len(exchange_orders) > 0:
                            # Есть открытые ордера на бирже - синхронизируемся с ними
                            log.info(f"Found {len(exchange_orders)} existing open orders on exchange, syncing...")
                            await self.sync_orders_from_exchange()

                            # Проверяем, есть ли загруженные ордера из состояния
                            if len(self.orders) == 0:
                                # Если ордеров нет в памяти, но есть на бирже, загружаем их из состояния или синхронизируем
                                await self.sync_orders_from_exchange()

                            # Загружаем состояние (параметры, статистику) — в потоке
                            await asyncio.to_thread(self.load_state)

                            # Обновляем initial_equity если он не был сохранен
                            if self.initial_equity == 0:
                                self.initial_equity = await self.get_total_equity(price)

                            self.state = BotState.TRADING
                            await asyncio.to_thread(self.save_state)  # чтобы при перезапуске сработало авто-восстановление
                            log.info(
                                "✅ [MAIN_LOOP] %s | Bot resumed with %s existing orders, state changed to TRADING", self._log_prefix(), len(self.orders)
                            )

                            # Уведомляем через Telegram о восстановлении
                            if self.telegram_notifier:
                                try:
                                    open_buy = len([o for o in self.orders if o.side == "BUY" and o.status == "open"])
                                    open_sell = len([o for o in self.orders if o.side == "SELL" and o.status == "open"])
                                    await self.telegram_notifier(
                                        f"✅ Бот восстановлен!\n\n"
                                        f"Символ: {self.symbol}\n"
                                        f"Открыто ордеров: BUY={open_buy}, SELL={open_sell}\n"
                                        f"Баланс: {self.quote_asset_name}={self.current_deposit:.2f}, {self.base_asset_name}={self.base_asset:.8f}\n"
                                        f"Параметры: шаг={self.grid_step_pct * 100:.2f}%, размер ордера={self.buy_order_value:.2f}"
                                    )
                                except Exception as e:
                                    log.warning(f"Failed to send notification: {e}")
                        else:
                            # Нет открытых ордеров - создаем новую сетку
                            log.warning("[MAIN_LOOP] %s | No existing orders found on exchange, creating new grid...", self._log_prefix())
                            log.warning("[MAIN_LOOP] %s | This will create a new grid even if orders exist for a different symbol!", self._log_prefix())
                            self.initial_equity = await self.get_total_equity(price)
                            await self.create_grid()
                            self.state = BotState.TRADING
                            await asyncio.to_thread(self.save_state)  # чтобы при перезапуске сработало авто-восстановление
                            log.info("✅ [MAIN_LOOP] %s | Bot initialized with new grid, state changed to TRADING", self._log_prefix())

                            # Уведомляем через Telegram о успешной инициализации
                            if self.telegram_notifier:
                                try:
                                    await self.telegram_notifier(
                                        f"✅ Бот инициализирован!\n\n"
                                        f"Символ: {self.symbol}\n"
                                        f"Баланс: {self.quote_asset_name}={self.current_deposit:.2f}, {self.base_asset_name}={self.base_asset:.8f}\n"
                                        f"Создано ордеров: {len(self.orders)}"
                                    )
                                except Exception as e:
                                    log.warning(f"Failed to send notification: {e}")

                    except Exception as e:
                        log.error(f"Failed to initialize bot for user {self.user_id}: {e}", exc_info=True)
                        # Оставляем бота в состоянии INITIALIZING для повторной попытки
                        # Уведомляем через Telegram об ошибке
                        if self.telegram_notifier:
                            try:
                                await self.telegram_notifier(f"❌ Ошибка инициализации бота:\n{str(e)}\n\nБот будет автоматически повторять попытки.")
                            except Exception:
                                pass
                        await asyncio.sleep(60)  # Ждем перед повторной попыткой

                elif self.state == BotState.TRADING:
                    log.info("[MAIN_LOOP] %s | State: TRADING, checking orders...", self._log_prefix())
                    # ВАЖНО: сначала проверяем исполнение, ПОТОМ синхронизируем!
                    # Иначе sync_orders_from_exchange удалит исполненные ордера до того, как check_orders их обработает
                    await self.check_orders()
                    await self.sync_orders_from_exchange()
                    # Проверяем критическую ситуацию: все BUY исполнены, но SELL не созданы и рыночная покупка не сработала
                    await self.check_critical_situation()
                    # Защита: при 3 открытых BUY и большой сетке — добавить до 5 BUY внизу (растянуть сетку, избежать rebalance sell на дне)
                    await self.check_protection_add_five_buy_when_three_left()

                elif self.state == BotState.PAUSED:
                    pass

                elif self.state == BotState.CRITICAL:
                    await self.check_orders()

                elif self.state == BotState.STOPPED:
                    log.info("[MAIN_LOOP] %s | State: STOPPED - exiting main loop", self._log_prefix())
                    break

                await asyncio.sleep(15)

            except Exception as e:
                log.exception("❌ [MAIN_LOOP] %s | Error in main loop: %s", self._log_prefix(), e, exc_info=True)
                await asyncio.sleep(60)
