"""Тесты хвоста сетки: пороги, ATR, парсер kline, шаг хвоста, ранний выход try_activate."""
import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import tail_grid
from order_manager import Order
from trading_bot import BotState, TradingBot


def test_open_sell_threshold_interpolation():
    assert tail_grid.open_sell_threshold_for_grid_step(Decimal("0.0075")) == 120
    assert tail_grid.open_sell_threshold_for_grid_step(Decimal("0.015")) == 60
    mid = tail_grid.open_sell_threshold_for_grid_step(Decimal("0.01125"))
    assert 60 <= mid <= 120


def test_should_block_and_cancel_symmetric_at_boundary():
    g = Decimal("0.015")
    t = tail_grid.open_sell_threshold_for_grid_step(g)
    assert tail_grid.should_block_auto_vwap(t, g) is True
    assert tail_grid.should_allow_tail_cancel(t, g) is True
    assert tail_grid.should_block_auto_vwap(t - 1, g) is False
    assert tail_grid.should_allow_tail_cancel(t + 1, g) is False


def test_atr_wilder_simple():
    candles = [
        {"open": Decimal("100"), "high": Decimal("110"), "low": Decimal("90"), "close": Decimal("100"), "_t": 1},
        {"open": Decimal("100"), "high": Decimal("105"), "low": Decimal("95"), "close": Decimal("102"), "_t": 2},
    ]
    for i in range(20):
        candles.append(
            {
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "_t": 3 + i,
            }
        )
    atr = tail_grid.compute_atr_wilder(candles, 14)
    assert atr is not None
    assert atr > 0


def test_order_tail_roundtrip():
    o = Order("1", "BUY", Decimal("1"), Decimal("2"), is_tail=True, base_ladder_index=3)
    d = o.to_dict()
    assert d.get("is_tail") is True
    assert d.get("base_ladder_index") == 3
    o2 = Order.from_dict(d)
    assert o2.is_tail is True
    assert o2.base_ladder_index == 3


# --- normalize_klines_payload ---


def test_normalize_klines_none_and_empty():
    assert tail_grid.normalize_klines_payload(None) == []
    assert tail_grid.normalize_klines_payload({}) == []


def test_normalize_klines_dict_data_list_of_dicts():
    raw = {
        "data": [
            {"open": "1", "high": "2", "low": "0.5", "close": "1.5", "time": 1000},
            {"o": "1", "h": "2", "l": "0.5", "c": "1.5"},
        ]
    }
    out = tail_grid.normalize_klines_payload(raw)
    assert len(out) == 2
    assert out[0]["close"] == Decimal("1.5")
    assert out[0].get("_t") == 1000


def test_normalize_klines_dict_klines_key():
    raw = {"klines": [{"open": "10", "high": "11", "low": "9", "close": "10"}]}
    out = tail_grid.normalize_klines_payload(raw)
    assert len(out) == 1
    assert out[0]["low"] == Decimal("9")


def test_normalize_klines_top_level_list_of_arrays():
    raw = [
        [1_700_000_000_000, "100", "110", "90", "105"],
        [1_700_000_360_000, "105", "106", "104", "105"],
    ]
    out = tail_grid.normalize_klines_payload(raw)
    assert len(out) == 2
    assert out[0]["open"] == Decimal("100")
    assert out[0]["_t"] == 1_700_000_000_000


def test_normalize_klines_skips_bad_row():
    raw = {"data": [{"open": "1", "high": "2", "low": "0", "close": "1"}, {}, {"oops": True}]}
    out = tail_grid.normalize_klines_payload(raw)
    assert len(out) == 1


# --- order_candles_chronologically ---


def test_order_candles_sort_by_time():
    a = {"close": Decimal("1"), "_t": 300}
    b = {"close": Decimal("2"), "_t": 100}
    c = {"close": Decimal("3"), "_t": 200}
    ordered = tail_grid.order_candles_chronologically([a, b, c])
    assert [x["close"] for x in ordered] == [Decimal("2"), Decimal("3"), Decimal("1")]


def test_order_candles_newest_first_without_time_gets_reversed():
    oldest = {"open": Decimal("1"), "high": Decimal("2"), "low": Decimal("0.5"), "close": Decimal("1")}
    newest = {"open": Decimal("2"), "high": Decimal("3"), "low": Decimal("1"), "close": Decimal("2")}
    ordered = tail_grid.order_candles_chronologically([newest, oldest])
    assert ordered[0]["close"] == Decimal("1")
    assert ordered[-1]["close"] == Decimal("2")


def test_step_tail_price_wilder_matches_tz():
    """ТЗ: round_to_tick(ATR × k) в единицах цены."""
    tick = Decimal("0.01")
    atr = Decimal("2")
    k = Decimal("0.5")
    st = tail_grid.step_tail_price_wilder(atr, k, tick, Decimal("30"))
    assert st == Decimal("1.00")


# --- try_activate_tail_grid: ранний выход (без запроса klines) ---


@pytest.fixture
def temp_dirs():
    with tempfile.TemporaryDirectory() as state_dir:
        with tempfile.TemporaryDirectory() as user_data_dir:
            yield state_dir, user_data_dir


@pytest.fixture
def tail_bot(temp_dirs, monkeypatch):
    """Кулдаун п.4.6 в юнит-тестах отключён (0 с), чтобы не ломать сценарии с повторной активацией."""
    monkeypatch.setattr(config, "TAIL_ANTIFLAP_COOLDOWN_SEC", 0)
    state_dir, user_data_dir = temp_dirs
    trades_dir = os.path.join(tempfile.gettempdir(), "trades_tail_unit")
    os.makedirs(trades_dir, exist_ok=True)
    mock_sync = MagicMock()
    mock_sync.circuit_breaker = MagicMock()
    with (
        patch("trading_bot.config.STATE_DIR", state_dir),
        patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
        patch("trading_bot.BingXSpot", return_value=mock_sync),
        patch("persistence.config.STATE_DIR", state_dir),
        patch("persistence.config.USER_DATA_DIR", user_data_dir),
    ):
        bot = TradingBot(42, "key", "secret", symbol="ETH-USDT")
    bot.ex = MagicMock()
    bot.ex.open_orders = AsyncMock(return_value=[])
    bot.ex.available_balance = AsyncMock(return_value=Decimal("10000"))
    bot.ex.get_spot_klines_v2 = AsyncMock(return_value=[])
    bot.ex.symbol_info = AsyncMock(
        return_value={
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
        }
    )
    bot.ex.balance = AsyncMock(return_value=Decimal("10000"))
    bot.ex.place_limit = AsyncMock(return_value={"orderId": "tail-1"})
    bot.grid_step_pct = Decimal("0.015")
    bot.buy_order_value = Decimal("50")
    bot.state = BotState.TRADING
    bot.save_state = MagicMock()
    return bot


@pytest.mark.asyncio
async def test_try_activate_skips_when_already_activated(tail_bot):
    tail_bot._tail_activation_done = True
    tail_bot._base_ladder_count = 2
    tail_bot._base_ladder_filled_indices = {1, 2}
    await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.ex.get_spot_klines_v2.assert_not_called()


@pytest.mark.asyncio
async def test_try_activate_skips_when_base_incomplete(tail_bot):
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 3
    tail_bot._base_ladder_filled_indices = {1, 2}
    await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.ex.get_spot_klines_v2.assert_not_called()


@pytest.mark.asyncio
async def test_try_activate_skips_when_antiflap_cooldown_active(tail_bot, monkeypatch):
    """ТЗ п.4.6: пока не прошёл кулдаун после последнего kline/отмены — kline не запрашиваем."""
    monkeypatch.setattr(config, "TAIL_ANTIFLAP_COOLDOWN_SEC", 120)
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 1
    tail_bot._base_ladder_filled_indices = {1}
    tail_bot._last_base_buy_fill_price = Decimal("2000")
    tail_bot.orders = []
    tail_bot._tail_antiflap_last_ts = 1000.0
    with patch("trading_bot.time.time", return_value=1050.0):
        await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.ex.get_spot_klines_v2.assert_not_called()


@pytest.mark.asyncio
async def test_try_activate_calls_klines_when_open_sell_at_threshold_015(tail_bot):
    """При open_SELL >= порога (как после съеденной базы ~65 SELL) хвост всё равно активируется — п.4, не п.9."""
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 1
    tail_bot._base_ladder_filled_indices = {1}
    tail_bot._last_base_buy_fill_price = Decimal("2000")
    tail_bot.orders = []
    tail_bot.grid_step_pct = Decimal("0.015")
    thr = tail_grid.open_sell_threshold_for_grid_step(tail_bot.grid_step_pct)
    tail_bot.ex.open_orders = AsyncMock(
        return_value=[{"orderId": str(i), "side": "SELL"} for i in range(thr)]
    )
    candles = []
    t0 = 1_700_000_000_000
    for i in range(20):
        candles.append([t0 + i * 3600_000, "100", "101", "99", "100"])
    tail_bot.ex.get_spot_klines_v2 = AsyncMock(return_value=candles)
    with patch("trading_bot.config.TAIL_MAX_ORDERS", 1):
        await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.ex.get_spot_klines_v2.assert_called_once()
    tail_bot.ex.place_limit.assert_called()


@pytest.mark.asyncio
async def test_try_activate_calls_klines_when_open_sell_at_threshold_075(tail_bot):
    """То же для шага 0.75% и 120 открытых SELL."""
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 1
    tail_bot._base_ladder_filled_indices = {1}
    tail_bot._last_base_buy_fill_price = Decimal("2000")
    tail_bot.orders = []
    tail_bot.grid_step_pct = Decimal("0.0075")
    thr = tail_grid.open_sell_threshold_for_grid_step(tail_bot.grid_step_pct)
    assert thr == 120
    tail_bot.ex.open_orders = AsyncMock(
        return_value=[{"orderId": str(i), "side": "SELL"} for i in range(thr)]
    )
    candles = []
    t0 = 1_700_000_000_000
    for i in range(20):
        candles.append([t0 + i * 3600_000, "100", "101", "99", "100"])
    tail_bot.ex.get_spot_klines_v2 = AsyncMock(return_value=candles)
    with patch("trading_bot.config.TAIL_MAX_ORDERS", 1):
        await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.ex.get_spot_klines_v2.assert_called_once()
    tail_bot.ex.place_limit.assert_called()


@pytest.mark.asyncio
async def test_try_activate_calls_klines_when_ready(tail_bot):
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 1
    tail_bot._base_ladder_filled_indices = {1}
    tail_bot._last_base_buy_fill_price = Decimal("2000")
    tail_bot.orders = []
    # минимальный набор свечей для ATR(14)
    candles = []
    t0 = 1_700_000_000_000
    for i in range(20):
        candles.append([t0 + i * 3600_000, "100", "101", "99", "100"])
    tail_bot.ex.get_spot_klines_v2 = AsyncMock(return_value=candles)
    await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.ex.get_spot_klines_v2.assert_called_once()
    tail_bot.ex.place_limit.assert_called()


@pytest.mark.asyncio
async def test_telegram_notifier_called_on_tail_activation_tz_p13(tail_bot):
    """ТЗ п.7 / п.13: после успешного включения хвоста — уведомление в Telegram."""
    tail_bot.telegram_notifier = AsyncMock()
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 1
    tail_bot._base_ladder_filled_indices = {1}
    tail_bot._last_base_buy_fill_price = Decimal("2000")
    tail_bot.orders = []
    candles = []
    t0 = 1_700_000_000_000
    for i in range(20):
        candles.append([t0 + i * 3600_000, "100", "101", "99", "100"])
    tail_bot.ex.get_spot_klines_v2 = AsyncMock(return_value=candles)
    with patch("trading_bot.config.TAIL_MAX_ORDERS", 1):
        await tail_bot.try_activate_tail_grid(Decimal("2000"))
    tail_bot.telegram_notifier.assert_awaited_once()
    msg = tail_bot.telegram_notifier.await_args[0][0]
    assert "Хвост" in msg or "хвост" in msg.lower()
    assert "ETH-USDT" in msg or tail_bot.symbol in msg


def _klines_for_atr():
    t0 = 1_700_000_000_000
    return [[t0 + i * 3600_000, "100", "101", "99", "100"] for i in range(20)]


@pytest.mark.asyncio
async def test_tail_place_cancel_and_place_again(tail_bot):
    """Полный цикл: выставить хвост → отменить (условие по SELL) → снова выставить хвост."""
    tail_bot._tail_activation_done = False
    tail_bot._base_ladder_count = 1
    tail_bot._base_ladder_filled_indices = {1}
    tail_bot._last_base_buy_fill_price = Decimal("2000")
    tail_bot.orders = []
    tail_bot.ex.cancel_order = AsyncMock()
    candles = _klines_for_atr()
    tail_bot.ex.get_spot_klines_v2 = AsyncMock(return_value=candles)

    order_ids = iter(["tail-wave-1", "tail-wave-2"])

    def _place(*_a, **_k):
        return {"orderId": next(order_ids)}

    tail_bot.ex.place_limit = AsyncMock(side_effect=_place)

    with patch("trading_bot.config.TAIL_MAX_ORDERS", 1):
        # 1) первая волна хвоста
        await tail_bot.try_activate_tail_grid(Decimal("2000"))

    assert tail_bot._tail_activation_done is True
    assert tail_bot.tail_active is True
    assert tail_bot.tail_order_ids == ["tail-wave-1"]
    assert tail_bot.tail_anchor_price is not None
    assert tail_bot.step_tail is not None  # цена: ATR×k (или fallback)
    assert tail_bot.tail_activated_at is not None
    tail_open = [o for o in tail_bot.orders if o.side == "BUY" and o.status == "open" and o.is_tail]
    assert len(tail_open) == 1
    assert tail_open[0].order_id == "tail-wave-1"
    assert tail_bot.ex.get_spot_klines_v2.call_count == 1
    assert tail_bot.ex.place_limit.call_count == 1

    # 2) отмена хвоста (open SELL = 0 ≤ порога)
    await tail_bot.cancel_tail_buy_orders_if_allowed()

    assert tail_bot._tail_activation_done is False
    assert tail_bot.tail_active is False
    assert tail_bot.tail_order_ids == []
    assert tail_bot.step_tail is None
    assert tail_bot.tail_anchor_price is None
    assert tail_bot.tail_activated_at is None
    assert not any(o.is_tail for o in tail_bot.orders)
    tail_bot.ex.cancel_order.assert_called_once_with(tail_bot.symbol, "tail-wave-1")

    tail_bot.ex.get_spot_klines_v2.reset_mock()
    tail_bot.ex.place_limit.reset_mock()
    tail_bot.ex.cancel_order.reset_mock()
    tail_bot.ex.get_spot_klines_v2.return_value = candles

    with patch("trading_bot.config.TAIL_MAX_ORDERS", 1):
        # 3) вторая волна хвоста после отмены
        await tail_bot.try_activate_tail_grid(Decimal("2000"))

    assert tail_bot._tail_activation_done is True
    tail_open2 = [o for o in tail_bot.orders if o.side == "BUY" and o.status == "open" and o.is_tail]
    assert len(tail_open2) == 1
    assert tail_open2[0].order_id == "tail-wave-2"
    assert tail_bot.ex.get_spot_klines_v2.call_count == 1
    assert tail_bot.ex.place_limit.call_count == 1
    assert tail_bot.tail_order_ids == ["tail-wave-2"]
    assert tail_bot.tail_active is True


def test_tail_fields_persisted_in_save_state():
    """В state попадают tail_active, tail_order_ids, step_tail, tail_anchor_price, tail_activated_at."""
    with tempfile.TemporaryDirectory() as state_dir:
        with tempfile.TemporaryDirectory() as user_data_dir:
            trades_dir = os.path.join(tempfile.gettempdir(), "trades_tail_save_test")
            os.makedirs(trades_dir, exist_ok=True)
            mock_sync = MagicMock()
            mock_sync.circuit_breaker = MagicMock()
            with (
                patch("trading_bot.config.STATE_DIR", state_dir),
                patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
                patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
                patch("trading_bot.BingXSpot", return_value=mock_sync),
                patch("persistence.config.STATE_DIR", state_dir),
                patch("persistence.config.USER_DATA_DIR", user_data_dir),
            ):
                bot = TradingBot(99901, "key", "secret", symbol="ETH-USDT")
            bot.ex = MagicMock()
            bot.grid_step_pct = Decimal("0.015")
            bot.orders = [
                Order("tid-1", "BUY", Decimal("1900"), Decimal("0.02"), is_tail=True),
            ]
            bot.tail_active = True
            bot.tail_order_ids = ["tid-1"]
            bot.step_tail = Decimal("15.00")
            bot.tail_anchor_price = Decimal("2000")
            bot.tail_activated_at = 1_700_000_000.0
            bot._tail_activation_done = True

            captured = []

            def _capture(_uid, state):
                captured.append(dict(state))

            orig_save = bot.persistence.save_state
            bot.persistence.save_state = _capture
            try:
                bot.save_state()
            finally:
                bot.persistence.save_state = orig_save

            assert len(captured) == 1
            saved = captured[0]
            assert saved.get("tail_active") is True
            assert saved.get("tail_order_ids") == ["tid-1"]
            assert saved.get("step_tail") == "15.00"
            assert saved.get("tail_anchor_price") == "2000"
            assert saved.get("tail_activated_at") == 1_700_000_000.0


def test_open_sell_threshold_none_uses_high_tier():
    assert tail_grid.open_sell_threshold_for_grid_step(None) == config.TAIL_OPEN_SELL_THRESHOLD_0_75_PCT


def test_normalize_klines_dict_without_list_returns_empty():
    assert tail_grid.normalize_klines_payload({"code": 0}) == []


def test_normalize_klines_non_list_non_dict():
    assert tail_grid.normalize_klines_payload("not-a-list") == []


def test_normalize_klines_invalid_time_skips_t_int():
    raw = {"data": [{"open": "1", "high": "2", "low": "0.5", "close": "1", "time": "not-int"}]}
    out = tail_grid.normalize_klines_payload(raw)
    assert len(out) == 1
    assert "_t" not in out[0]


def test_order_candles_empty_list():
    assert tail_grid.order_candles_chronologically([]) == []


def test_compute_atr_returns_none_when_tr_series_shorter_than_period():
    """14 свечей → len(TR)=13 < period 14 → None (ветка len(trs) < period)."""
    candles = []
    t0 = 1_000
    for i in range(14):
        candles.append(
            {
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "_t": t0 + i,
            }
        )
    assert tail_grid.compute_atr_wilder(candles, 14) is None


def test_step_tail_when_raw_nonpositive_uses_fallback():
    tick = Decimal("0.01")
    st = tail_grid.step_tail_price_wilder(Decimal("0"), Decimal("1"), tick, Decimal("2.5"))
    assert st == Decimal("2.50")


def test_step_tail_when_tick_zero_returns_raw():
    st = tail_grid.step_tail_price_wilder(Decimal("1"), Decimal("1"), Decimal("0"), Decimal("3"))
    assert st == Decimal("1")


def test_normalize_klines_short_array_row_skipped():
    """Ветка else: строка list/tuple с len < 5 — continue (стр. 67)."""
    raw = {"data": [[1, 2, 3], [1_700_000_000_000, "10", "11", "9", "10"]]}
    out = tail_grid.normalize_klines_payload(raw)
    assert len(out) == 1


def test_step_tail_negative_atr_times_k_uses_fallback_when_raw_nonpositive():
    """atr > 0, k < 0 → raw отрицательный; ветка raw <= 0 подставляет fallback (стр. 121–122)."""
    tick = Decimal("0.01")
    st = tail_grid.step_tail_price_wilder(Decimal("10"), Decimal("-0.5"), tick, Decimal("2.5"))
    assert st == Decimal("2.50")
