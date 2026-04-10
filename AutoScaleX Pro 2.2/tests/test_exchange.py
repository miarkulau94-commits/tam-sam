"""
Unit tests for exchange — CircuitBreaker, rate limit retry.
"""

import os
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exchange as exchange_mod
from exchange import CircuitBreaker, CircuitState, get_api_metrics_last_minute, _get_global_rps_limiter


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3, timeout=1, success_threshold=2)
        assert cb.state == CircuitState.CLOSED

    def test_successful_call_passes(self):
        cb = CircuitBreaker(failure_threshold=3, timeout=1, success_threshold=2)
        result = cb.call(lambda: 42)
        assert result == 42

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, timeout=60, success_threshold=2)

        def fail():
            raise RuntimeError("API error")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        assert cb.state == CircuitState.OPEN

    def test_blocks_when_open(self):
        cb = CircuitBreaker(failure_threshold=2, timeout=60, success_threshold=2)

        def fail():
            raise RuntimeError("fail")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        assert cb.state == CircuitState.OPEN

        with pytest.raises(RuntimeError, match="Circuit breaker is OPEN"):
            cb.call(lambda: 1)

    def test_api_key_error_not_counted(self):
        cb = CircuitBreaker(failure_threshold=2, timeout=60, success_threshold=2)

        def api_key_fail():
            raise RuntimeError("Incorrect apiKey")

        for _ in range(5):
            with pytest.raises(RuntimeError):
                cb.call(api_key_fail)
        assert cb.state == CircuitState.CLOSED

    def test_rate_limit_error_not_counted_toward_open(self):
        """429 / rate limit не открывают circuit breaker."""
        cb = CircuitBreaker(failure_threshold=3, timeout=60, success_threshold=2)

        def rate_limit_fail():
            raise RuntimeError("API BingX: превышен лимит запросов (rate limit) после 4 попыток.")

        for _ in range(10):
            with pytest.raises(RuntimeError):
                cb.call(rate_limit_fail)
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_reset_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, timeout=60, success_threshold=2)

        def fail():
            raise RuntimeError("fail")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.call(lambda: 1) == 1


class TestRateLimiter:
    def test_init_tokens(self):
        from exchange import RateLimiter
        rl = RateLimiter(rate_limit=5, interval=60)
        try:
            assert rl.tokens == 5
            for _ in range(5):
                rl.wait()
            assert rl.tokens == 0
        finally:
            rl.stop()

    def test_stop(self):
        from exchange import RateLimiter
        rl = RateLimiter(rate_limit=10, interval=60)
        rl.stop()
        # stop should not raise


class TestBingXSpotSign:
    """BingXSpot._sign produces valid signature."""

    def test_sign_adds_signature(self):
        from exchange import BingXSpot

        ex = BingXSpot("key1", "secret1")
        payload = ex._sign({"symbol": "BTC-USDT", "timestamp": "1234567890"})
        assert "signature" in payload
        assert isinstance(payload["signature"], str)
        assert len(payload["signature"]) == 64  # sha256 hex

    def test_sign_sorts_params(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        out = ex._sign({"a": "1", "b": "2"})
        assert "signature" in out
        assert out["a"] == "1"
        assert out["b"] == "2"


class TestBingXSpotAPI:
    """BingXSpot API methods with mocked _request."""

    def test_symbol_info_returns_structure_when_request_ok(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        raw = {
            "symbols": [
                {
                    "symbol": "BTC-USDT",
                    "status": 1,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                    ],
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                }
            ]
        }
        ex._request = lambda m, e, p=None: raw if e == "/openApi/spot/v1/common/symbols" else None
        info = ex.symbol_info("BTC-USDT")
        assert info["stepSize"] == Decimal("0.00001")
        assert info["minQty"] == Decimal("0.00001")
        assert info["tickSize"] == Decimal("0.01")
        assert info["status"] == "TRADING"
        assert info["baseAsset"] == "BTC"
        assert info["quoteAsset"] == "USDT"

    def test_symbol_info_returns_defaults_when_request_none(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, e, p=None: None
        info = ex.symbol_info("BTC-USDT")
        assert info["stepSize"] == Decimal("0.000001")
        assert info["minQty"] == Decimal("0.000001")
        assert info["tickSize"] == Decimal("0.01")
        assert info["status"] == "UNKNOWN"

    def test_symbol_info_raises_when_symbol_not_in_response(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, e, p=None: {"symbols": [{"symbol": "ETH-USDT"}]}
        with pytest.raises(RuntimeError, match="BTC-USDT not found"):
            ex.symbol_info("BTC-USDT")

    def test_price_returns_decimal_from_ticker(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, e, p=None: [{"lastPrice": "50000.5"}] if "ticker" in e else None
        price = ex.price("BTC-USDT")
        assert price == Decimal("50000.5")

    def test_balance_returns_decimal_from_balance_response(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, e, p=None: {"balances": [{"asset": "USDT", "free": "100", "locked": "10"}]} if "balance" in e else None
        bal = ex.balance("USDT")
        assert bal == Decimal("110")

    def test_open_orders_returns_list(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, e, p=None: {"orders": [{"orderId": "1", "side": "BUY"}]} if "openOrders" in e else None
        orders = ex.open_orders("BTC-USDT")
        assert orders == [{"orderId": "1", "side": "BUY"}]

    def test_open_orders_returns_empty_when_request_none(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, e, p=None: None
        orders = ex.open_orders("BTC-USDT")
        assert orders == []


class TestBingXSpotRateLimitRetry:
    """Rate limit: ретраи с длинной паузой (18, 36 сек), без немедленного raise."""

    def test_rate_limited_retries_then_succeeds(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        # Эмуляция ответов BingX: два раза rate limited, третий — успех
        def json_429():
            return {"code": 429, "msg": "rate limited"}
        def json_ok():
            return {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}}

        r1, r2 = Mock(), Mock()
        r1.raise_for_status = r2.raise_for_status = lambda: None
        r1.json = json_429
        r2.json = json_429
        r3 = Mock()
        r3.raise_for_status = lambda: None
        r3.json = json_ok

        with patch.object(ex.sess, "get", side_effect=[r1, r2, r3]):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")

        assert result == Decimal("100")
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 18
        assert mock_sleep.call_args_list[1][0][0] == 36

    def test_rate_limited_exhausted_raises_with_message(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r = Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"code": 429, "msg": "rate limited"}
        with patch.object(ex.sess, "get", side_effect=[r, r, r, r]):
            with patch("exchange.time.sleep"):
                with pytest.raises(RuntimeError, match="rate limit|превышен лимит"):
                    ex.balance("USDT")

    def test_frequency_limit_bingx_100410_retries_then_succeeds(self):
        """BingX code 100410 'frequency limit' — те же ретраи 18/36 сек, что и для 'rate limited'."""
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        msg_100410 = "code:100410:The endpoint trigger frequency limit rule is currently in the disabled period and will be unblocked after 1772633973621"

        def json_freq_limit():
            return {"code": 100410, "msg": msg_100410}
        def json_ok():
            return {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "200", "locked": "0"}]}}

        r1, r2 = Mock(), Mock()
        r1.raise_for_status = r2.raise_for_status = lambda: None
        r1.json = json_freq_limit
        r2.json = json_freq_limit
        r3 = Mock()
        r3.raise_for_status = lambda: None
        r3.json = json_ok

        with patch.object(ex.sess, "get", side_effect=[r1, r2, r3]):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")

        assert result == Decimal("200")
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 18
        assert mock_sleep.call_args_list[1][0][0] == 36

    def test_rate_limit_headers_low_remain_triggers_sleep(self):
        """При успешном ответе с X-RateLimit Remain < 5 и Expire > 0 вызывается sleep(Expire+0.5)."""
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r = Mock()
        r.raise_for_status = lambda: None
        r.headers = {"X-RateLimit-Requests-Remain": "3", "X-RateLimit-Requests-Expire": "10"}
        r.json = lambda: {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}}

        with patch.object(ex.sess, "get", return_value=r):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")

        assert result == Decimal("100")
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 10.5

    def test_rate_limit_headers_remain_high_no_sleep(self):
        """При Remain >= 5 sleep не вызывается."""
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r = Mock()
        r.raise_for_status = lambda: None
        r.headers = {"X-RateLimit-Requests-Remain": "10", "X-RateLimit-Requests-Expire": "60"}
        r.json = lambda: {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "50", "locked": "0"}]}}

        with patch.object(ex.sess, "get", return_value=r):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")

        assert result == Decimal("50")
        mock_sleep.assert_not_called()

    def test_rate_limit_headers_expire_capped_at_60s(self):
        """При большом Expire (или в мс) пауза ограничена 60 с, чтобы не блокировать бота."""
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r = Mock()
        r.raise_for_status = lambda: None
        r.headers = {"X-RateLimit-Requests-Remain": "2", "X-RateLimit-Requests-Expire": "300000"}
        r.json = lambda: {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}}

        with patch.object(ex.sess, "get", return_value=r):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")

        assert result == Decimal("100")
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 60.0

    def test_rate_limit_fourth_attempt_uses_75s_delay(self):
        """При 429: перед 4-й попыткой пауза 75 с (3-й sleep)."""
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r_fail = Mock()
        r_fail.raise_for_status = lambda: None
        r_fail.json = lambda: {"code": 429, "msg": "rate limited"}
        r_ok = Mock()
        r_ok.raise_for_status = lambda: None
        r_ok.json = lambda: {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}}

        with patch.object(ex.sess, "get", side_effect=[r_fail, r_fail, r_fail, r_ok]):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")
        assert result == Decimal("100")
        assert mock_sleep.call_count == 3
        assert mock_sleep.call_args_list[0][0][0] == 18
        assert mock_sleep.call_args_list[1][0][0] == 36
        assert mock_sleep.call_args_list[2][0][0] == 75

    def test_5xx_retries_then_succeeds(self):
        """При HTTP 502/503 — ретраи с паузой 5*attempt, затем успех."""
        import requests
        from decimal import Decimal
        from exchange import BingXSpot

        def raise_502():
            err = requests.exceptions.HTTPError()
            err.response = Mock(status_code=502)
            raise err

        ex = BingXSpot("k", "s")
        r_fail = Mock()
        r_fail.raise_for_status = raise_502
        r_fail.json = lambda: None
        r_ok = Mock()
        r_ok.raise_for_status = lambda: None
        r_ok.json = lambda: {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}}

        with patch.object(ex.sess, "get", side_effect=[r_fail, r_ok]):
            with patch("exchange.time.sleep") as mock_sleep:
                result = ex.balance("USDT")
        assert result == Decimal("100")
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 5


class TestGlobalRateLimiterAndMetrics:
    """Глобальный лимитер 40 RPS и метрики запросов/ошибок."""

    def test_global_rps_limiter_exists(self):
        limiter = _get_global_rps_limiter()
        assert limiter is not None
        assert hasattr(limiter, "wait")

    def test_get_api_metrics_last_minute_returns_dict(self):
        m = get_api_metrics_last_minute()
        assert isinstance(m, dict)
        assert "requests" in m
        assert "errors" in m
        assert m["requests"] >= 0
        assert m["errors"] >= 0


class TestCircuitBreakerHalfOpen:
    """HALF_OPEN после таймаута OPEN и возврат в CLOSED."""

    def test_closes_after_success_in_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, timeout=10, success_threshold=1)

        def fail():
            raise RuntimeError("fail")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        assert cb.state == CircuitState.OPEN

        # Сдвигаем «последний сбой» в прошлое, чтобы при фиксированном time перейти из OPEN в HALF_OPEN
        cb.last_failure_time = 0.0
        with patch.object(exchange_mod.time, "time", return_value=100.0):
            assert cb.call(lambda: 42) == 42
        assert cb.state == CircuitState.CLOSED


class TestBingXSpotHttpVerbsAndHelpers:
    """DELETE/POST в _request, place_market, cancel_order, кэш, is_symbol_trading."""

    def test_request_delete_uses_sess_delete(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r = Mock()
        r.raise_for_status = lambda: None
        r.headers = {}
        r.json = lambda: {"code": 0, "data": {"deleted": True}}
        with patch.object(ex.sess, "delete", return_value=r):
            with patch("exchange.time.sleep"):
                out = ex._request("DELETE", "/openApi/test", {"id": "1"})
        assert out == {"deleted": True}

    def test_request_post_uses_sess_post(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        r = Mock()
        r.raise_for_status = lambda: None
        r.headers = {}
        r.json = lambda: {"code": 0, "data": {"orderId": "post1"}}
        with patch.object(ex.sess, "post", return_value=r):
            with patch("exchange.time.sleep"):
                out = ex._request("POST", "/openApi/spot/v1/trade/order", {"symbol": "BTC-USDT"})
        assert out["orderId"] == "post1"

    def test_place_market_bad_side(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        with pytest.raises(ValueError, match="Bad side"):
            ex.place_market("BTC-USDT", "HOLD", Decimal("1"))

    def test_place_market_returns_none_when_no_qty(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        assert ex.place_market("BTC-USDT", "SELL", Decimal("0"), None) is None

    def test_place_market_sell_with_quantity(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        captured = {}

        def capture_post(m, ep, p=None):
            captured["payload"] = p
            return {"orderId": "m1"}

        ex._request = capture_post
        r = ex.place_market("BTC-USDT", "SELL", Decimal("0.1"), None)
        assert r["orderId"] == "m1"
        assert "quantity" in captured["payload"]

    def test_cancel_order_order_gone_returns_none(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")

        def boom(*a, **k):
            raise RuntimeError("Order does not exist on exchange")

        ex._request = boom
        assert ex.cancel_order("BTC-USDT", "999") is None

    def test_price_second_call_uses_cache(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        n = {"c": 0}

        def req(m, ep, p=None):
            n["c"] += 1
            return [{"lastPrice": "50.5"}]

        ex._request = req
        assert ex.price("ETH-USDT") == Decimal("50.5")
        assert ex.price("ETH-USDT") == Decimal("50.5")
        assert n["c"] == 1

    def test_balance_zero_when_request_returns_none(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda *a, **k: None
        assert ex.balance("USDT") == Decimal("0")

    def test_price_raises_when_api_none_and_no_cache(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda *a, **k: None
        with pytest.raises(RuntimeError, match="кэшированной цены|API"):
            ex.price("BTC-USDT")

    def test_invalidate_balance_cache_clears_all_when_asset_none(self):
        from decimal import Decimal
        import time as time_std

        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._cache = {("balance", "USDT"): (Decimal("1"), time_std.time())}
        ex.invalidate_balance_cache(None)
        assert ("balance", "USDT") not in ex._cache

    def test_is_symbol_trading_false_for_break_status(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        raw = {
            "symbols": [
                {
                    "symbol": "ZBT-USDT",
                    "status": 0,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                    ],
                    "baseAsset": "ZBT",
                    "quoteAsset": "USDT",
                }
            ]
        }
        ex._request = lambda m, e, p=None: raw if "symbols" in e else None
        assert ex.is_symbol_trading("ZBT-USDT") is False

    def test_close_stops_rate_limiter(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.close()
        ex.rate_limiter._stop_event.is_set()


class TestBingXSpotAsyncWrapper:
    """BingXSpotAsync делегирует в sync-клиент."""

    @pytest.mark.asyncio
    async def test_place_market_async(self):
        from decimal import Decimal
        from exchange import BingXSpot, BingXSpotAsync

        ex = BingXSpot("k", "s")
        ex._request = lambda *a, **k: {"orderId": "async1"}
        async_ex = BingXSpotAsync(ex)
        r = await async_ex.place_market("ETH-USDT", "BUY", Decimal("0"), quote_order_qty=Decimal("25"))
        assert r["orderId"] == "async1"

    @pytest.mark.asyncio
    async def test_get_order_async(self):
        from exchange import BingXSpot, BingXSpotAsync

        ex = BingXSpot("k", "s")
        ex._request = lambda *a, **k: {"orderId": "q1", "status": "FILLED"}
        async_ex = BingXSpotAsync(ex)
        r = await async_ex.get_order("ETH-USDT", "q1")
        assert r["status"] == "FILLED"


class TestBingXSpotCancelAll:
    """cancel_all: пропуск битых ордеров, отмена валидных, ошибки cancel_order."""

    def test_cancel_all_skips_bad_side_or_type(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.open_orders = lambda symbol: [
            {"side": "BUY", "type": "LIMIT", "orderId": "good"},
            {"side": "WEIRD", "type": "LIMIT", "orderId": "skip1"},
            {"side": "BUY", "type": "STOP", "orderId": "skip2"},
        ]
        ex.cancel_order = MagicMock(return_value={})
        with patch("exchange.time.sleep"):
            ex.cancel_all("ETH-USDT")
        ex.cancel_order.assert_called_once_with("ETH-USDT", "good")

    def test_cancel_all_processes_buy_and_sell(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.open_orders = lambda symbol: [
            {"side": "BUY", "type": "LIMIT", "orderId": "a"},
            {"side": "SELL", "type": "LIMIT", "orderId": "b"},
        ]
        ex.cancel_order = MagicMock(return_value={})
        with patch("exchange.time.sleep"):
            ex.cancel_all("ETH-USDT")
        assert ex.cancel_order.call_count == 2

    def test_cancel_all_swallows_order_not_exist(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.open_orders = lambda symbol: [{"side": "BUY", "type": "LIMIT", "orderId": "x"}]

        def cancel(sym, oid):
            raise RuntimeError("Order does not exist")

        ex.cancel_order = cancel
        with patch("exchange.time.sleep"):
            ex.cancel_all("ETH-USDT")

    def test_cancel_all_warns_on_other_cancel_failure(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.open_orders = lambda symbol: [{"side": "BUY", "type": "LIMIT", "orderId": "x"}]
        ex.cancel_order = MagicMock(side_effect=RuntimeError("server error"))
        with patch("exchange.time.sleep"):
            ex.cancel_all("ETH-USDT")

    def test_cancel_order_non_exist_message_raises_none(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("order not exist"))
        assert ex.cancel_order("BTC-USDT", "1") is None

    def test_cancel_order_other_error_reraises(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))
        with pytest.raises(RuntimeError, match="network"):
            ex.cancel_order("BTC-USDT", "1")


class TestBingXSpotValidateAndPlaceLimit:
    """validate_order и place_limit."""

    def _trading_info(self):
        from decimal import Decimal

        return {
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.1"),
            "minNotional": Decimal("5"),
            "status": "TRADING",
        }

    def test_validate_order_rejects_non_positive_price(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: True
        ex.symbol_info = lambda s: self._trading_info()
        r = ex.validate_order("ETH-USDT", "BUY", Decimal("1"), Decimal("0"))
        assert r["valid"] is False
        assert any("Цена" in e for e in r["errors"])

    def test_validate_order_rejects_qty_below_min(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: True
        ex.symbol_info = lambda s: self._trading_info()
        r = ex.validate_order("ETH-USDT", "BUY", Decimal("0.05"), Decimal("2000"))
        assert r["valid"] is False
        assert any("минимального" in e for e in r["errors"])

    def test_validate_order_rejects_low_notional(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: True
        info = dict(self._trading_info())
        info["minNotional"] = Decimal("1000")
        ex.symbol_info = lambda s: info
        r = ex.validate_order("ETH-USDT", "BUY", Decimal("0.1"), Decimal("10"))
        assert r["valid"] is False
        assert any("Номинал" in e for e in r["errors"])

    def test_validate_order_buy_low_quote_adds_warning(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: True
        ex.symbol_info = lambda s: self._trading_info()
        ex.balance = lambda asset: Decimal("1")
        r = ex.validate_order("ETH-USDT", "BUY", Decimal("10"), Decimal("200"))
        assert r["valid"] is True
        assert any("Недостаточно баланса" in w for w in r["warnings"])

    def test_validate_order_sell_low_base_adds_warning(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: True
        ex.symbol_info = lambda s: self._trading_info()
        ex.balance = lambda asset: Decimal("0.01") if asset == "ETH" else Decimal("10000")
        r = ex.validate_order("ETH-USDT", "SELL", Decimal("5"), Decimal("200"))
        assert r["valid"] is True
        assert any("базовой валюты" in w for w in r["warnings"])

    def test_validate_order_symbol_not_trading(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: False
        ex.symbol_info = lambda s: {"status": "BREAK"}
        r = ex.validate_order("X-USDT", "BUY", Decimal("1"), Decimal("10"))
        assert r["valid"] is False
        assert "недоступен" in r["errors"][0]

    def test_place_limit_raises_when_validation_fails(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.is_symbol_trading = lambda s: False
        ex.symbol_info = lambda s: {"status": "BREAK"}
        with pytest.raises(ValueError, match="валидацию"):
            ex.place_limit("ETH-USDT", "BUY", Decimal("1"), Decimal("100"))

    def test_place_limit_validate_false_posts_order(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.symbol_info = lambda s: {
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.01"),
            "minNotional": Decimal("0"),
        }
        ex._request = MagicMock(return_value={"orderId": "lim1"})
        with patch("exchange.time.sleep"):
            res = ex.place_limit("ETH-USDT", "BUY", Decimal("1"), Decimal("2000"), validate=False)
        assert res["orderId"] == "lim1"
        ex._request.assert_called_once()

    def test_place_limit_validate_false_zero_price_returns_none(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.symbol_info = lambda s: {
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.01"),
            "minNotional": Decimal("0"),
        }
        assert ex.place_limit("ETH-USDT", "BUY", Decimal("1"), Decimal("0"), validate=False) is None

    def test_place_limit_bad_side_raises(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.symbol_info = lambda s: {
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.01"),
            "minNotional": Decimal("0"),
        }
        with pytest.raises(ValueError, match="Bad side"):
            ex.place_limit("ETH-USDT", "HOLD", Decimal("1"), Decimal("100"), validate=False)


class TestBingXSpotReferralsAndSymbols:
    """get_referrals_from_api, get_referral_commissions, get_all_symbols, get_popular_symbols, get_order_limits."""

    def test_get_referrals_from_api_returns_on_first_endpoint(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")

        def req(m, ep, p=None):
            if "invitee/list" in ep and "commission" not in ep:
                return {"invitees": [{"uid": "1"}], "total": 1, "page": 1, "pageSize": 50}
            return None

        ex._request = req
        out = ex.get_referrals_from_api(page=1, page_size=50)
        assert out is not None
        assert len(out["invitees"]) == 1
        assert out["total"] == 1

    def test_get_referral_commissions_returns_structure(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")

        def req(m, ep, p=None):
            if "commission" in ep:
                return {
                    "commissions": [{"a": 1}],
                    "totalCommission": "10",
                    "total": 1,
                    "page": 1,
                    "pageSize": 50,
                }
            return None

        ex._request = req
        out = ex.get_referral_commissions()
        assert out is not None
        assert out["totalCommission"] == "10"

    def test_get_all_symbols_filters_trading(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex._request = lambda m, ep, p=None: {
            "symbols": [
                {"symbol": "AAA-USDT", "baseAsset": "AAA", "quoteAsset": "USDT", "status": 1},
                {"symbol": "BBB-USDT", "baseAsset": "BBB", "quoteAsset": "USDT", "status": 0},
            ]
        }
        syms = ex.get_all_symbols()
        assert len(syms) == 1
        assert syms[0]["symbol"] == "AAA-USDT"

    def test_get_popular_symbols_orders_and_limits(self):
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.get_all_symbols = lambda: [
            {"symbol": "DOGE-USDT", "baseAsset": "DOGE", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "ZZZ-USDT", "baseAsset": "ZZZ", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "BTC-USDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
        ]
        pop = ex.get_popular_symbols(quote_asset="USDT", limit=10)
        assert len(pop) <= 10
        symbols = [p["symbol"] for p in pop]
        assert "BTC-USDT" in symbols

    def test_get_order_limits_returns_symbol_info_fields(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.symbol_info = lambda s: {
            "minQty": Decimal("0.1"),
            "minNotional": Decimal("5"),
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.01"),
            "status": "TRADING",
        }
        lim = ex.get_order_limits("ETH-USDT")
        assert lim["minQty"] == Decimal("0.1")
        assert lim["status"] == "TRADING"

    def test_get_order_limits_on_error_returns_defaults(self):
        from decimal import Decimal
        from exchange import BingXSpot

        ex = BingXSpot("k", "s")
        ex.symbol_info = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
        lim = ex.get_order_limits("ETH-USDT")
        assert lim["status"] == "UNKNOWN"
        assert lim["stepSize"] == Decimal("0.000001")
