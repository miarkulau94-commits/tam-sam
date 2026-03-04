"""
Unit tests for exchange — CircuitBreaker, rate limit retry.
"""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange import CircuitBreaker, CircuitState


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
        with patch.object(ex.sess, "get", side_effect=[r, r, r]):
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
