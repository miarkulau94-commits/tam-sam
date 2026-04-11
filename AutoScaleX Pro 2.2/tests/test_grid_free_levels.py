"""Поиск следующего свободного уровня сетки (BUY вниз / SELL вверх)."""
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import Order
from trading_bot import TradingBot


@pytest.fixture
def bot_minimal():
    """Минимальный бот: только orders и grid_step_pct."""
    mock_ex = MagicMock()
    mock_ex.circuit_breaker = MagicMock()
    mock_ex.circuit_breaker.reset = MagicMock()
    with patch("trading_bot.BingXSpot", return_value=mock_ex), patch("trading_bot.BingXSpotAsync", return_value=mock_ex):
        b = TradingBot(999001, "k", "s", symbol="TEST-USDT")
    b.grid_step_pct = Decimal("0.015")
    b.orders = []
    return b


@pytest.fixture
def bot_0075():
    mock_ex = MagicMock()
    mock_ex.circuit_breaker = MagicMock()
    mock_ex.circuit_breaker.reset = MagicMock()
    with patch("trading_bot.BingXSpot", return_value=mock_ex), patch("trading_bot.BingXSpotAsync", return_value=mock_ex):
        b = TradingBot(999002, "k", "s", symbol="TEST-USDT")
    b.grid_step_pct = Decimal("0.0075")
    b.orders = []
    return b


def _first_buy(bot: TradingBot, anchor: Decimal, tick: Decimal) -> Decimal:
    g = bot.grid_step_pct
    assert g is not None
    return bot._align_to_tick(anchor * (Decimal("1") - g), tick)


def _first_sell(bot: TradingBot, anchor: Decimal, tick: Decimal) -> Decimal:
    g = bot.grid_step_pct
    assert g is not None
    return bot._align_to_tick(anchor * (Decimal("1") + g), tick)


def test_find_next_free_buy_skips_occupied(bot_minimal):
    tick = Decimal("0.01")
    anchor = Decimal("100")
    bot_minimal.orders.append(Order(order_id="1", side="BUY", price=Decimal("98.50"), qty=Decimal("1"), amount_usdt=Decimal("10")))
    first = _first_buy(bot_minimal, anchor, tick)
    got = bot_minimal.find_next_free_buy_price_down(anchor, tick)
    assert got is not None
    assert got < first
    assert not bot_minimal._open_buy_at_tick(got, tick)


def test_find_next_free_sell_skips_occupied(bot_minimal):
    tick = Decimal("0.01")
    anchor = Decimal("100")
    first = _first_sell(bot_minimal, anchor, tick)
    bot_minimal.orders.append(Order(order_id="1", side="SELL", price=first, qty=Decimal("1")))
    got = bot_minimal.find_next_free_sell_price_up(anchor, tick)
    assert got is not None
    assert got > first


def test_find_next_free_buy_empty_orders_returns_first_grid(bot_minimal):
    tick = Decimal("0.01")
    anchor = Decimal("100")
    expected = _first_buy(bot_minimal, anchor, tick)
    got = bot_minimal.find_next_free_buy_price_down(anchor, tick)
    assert got == expected


def test_find_next_free_sell_empty_orders_returns_first_grid(bot_minimal):
    tick = Decimal("0.01")
    anchor = Decimal("100")
    expected = _first_sell(bot_minimal, anchor, tick)
    got = bot_minimal.find_next_free_sell_price_up(anchor, tick)
    assert got == expected


def test_find_next_free_ignores_non_open_orders(bot_minimal):
    tick = Decimal("0.01")
    anchor = Decimal("100")
    first = _first_buy(bot_minimal, anchor, tick)
    bot_minimal.orders.append(
        Order(order_id="1", side="BUY", price=first, qty=Decimal("1"), status="filled", amount_usdt=Decimal("10"))
    )
    got = bot_minimal.find_next_free_buy_price_down(anchor, tick)
    assert got == first


def test_find_next_free_grid_step_invalid_returns_none(bot_minimal):
    tick = Decimal("0.01")
    bot_minimal.grid_step_pct = Decimal("0")
    assert bot_minimal.find_next_free_buy_price_down(Decimal("100"), tick) is None
    assert bot_minimal.find_next_free_sell_price_up(Decimal("100"), tick) is None
    bot_minimal.grid_step_pct = None  # type: ignore[assignment]
    assert bot_minimal.find_next_free_buy_price_down(Decimal("100"), tick) is None


def test_buy_uses_shallow_fallback_after_max_grid_steps(bot_minimal):
    """После GRID_FREE_MAX_STEPS занятых уровней по сетке — первый свободный мелкий % от якоря."""
    tick = Decimal("0.01")
    anchor = Decimal("100")
    g = bot_minimal.grid_step_pct
    assert g is not None
    p = bot_minimal._align_to_tick(anchor * (Decimal("1") - g), tick)
    orders = []
    with patch("config.GRID_FREE_MAX_STEPS", 2):
        for _ in range(2):
            orders.append(Order(order_id=str(len(orders)), side="BUY", price=p, qty=Decimal("1")))
            nxt = bot_minimal._align_to_tick(p * (Decimal("1") - g), tick)
            assert nxt < p
            p = nxt
        bot_minimal.orders = orders
        got = bot_minimal.find_next_free_buy_price_down(anchor, tick)
    # Первый fallback для 1.5%: 1.4% ниже якоря
    shallow = bot_minimal._align_to_tick(anchor * (Decimal("1") - Decimal("0.014")), tick)
    assert got == shallow
    assert not bot_minimal._open_buy_at_tick(got, tick)


def test_sell_uses_shallow_fallback_after_max_grid_steps(bot_minimal):
    tick = Decimal("0.01")
    anchor = Decimal("100")
    g = bot_minimal.grid_step_pct
    assert g is not None
    p = bot_minimal._align_to_tick(anchor * (Decimal("1") + g), tick)
    orders = []
    with patch("config.GRID_FREE_MAX_STEPS", 2):
        for _ in range(2):
            orders.append(Order(order_id=str(len(orders)), side="SELL", price=p, qty=Decimal("1")))
            nxt = bot_minimal._align_to_tick(p * (Decimal("1") + g), tick)
            assert nxt > p
            p = nxt
        bot_minimal.orders = orders
        got = bot_minimal.find_next_free_sell_price_up(anchor, tick)
    shallow = bot_minimal._align_to_tick(anchor * (Decimal("1") + Decimal("0.014")), tick)
    assert got == shallow


def test_fallback_pcts_0075_branch(bot_0075):
    """Сетка 0.75% — список fallback из GRID_FALLBACK_BUY_BELOW_ANCHOR_PCT_0075."""
    tick = Decimal("0.01")
    anchor = Decimal("200")
    g = bot_0075.grid_step_pct
    assert g is not None
    p = bot_0075._align_to_tick(anchor * (Decimal("1") - g), tick)
    orders = []
    with patch("config.GRID_FREE_MAX_STEPS", 1):
        orders.append(Order(order_id="1", side="BUY", price=p, qty=Decimal("1")))
        bot_0075.orders = orders
        got = bot_0075.find_next_free_buy_price_down(anchor, tick)
    first_fb = Decimal("0.006")
    expected = bot_0075._align_to_tick(anchor * (Decimal("1") - first_fb), tick)
    assert got == expected


def test_generic_grid_filters_fallbacks_below_step():
    """Произвольный шаг сетки: в fallback попадают только доли < grid_step_pct."""
    mock_ex = MagicMock()
    mock_ex.circuit_breaker = MagicMock()
    mock_ex.circuit_breaker.reset = MagicMock()
    with patch("trading_bot.BingXSpot", return_value=mock_ex), patch("trading_bot.BingXSpotAsync", return_value=mock_ex):
        b = TradingBot(999003, "k", "s", symbol="TEST-USDT")
    b.grid_step_pct = Decimal("0.01")  # 1.0% — отсекаем из generic всё >= 0.01
    b.orders = []
    pcts = b._fallback_pcts_shorter_than_grid()
    assert all(x < Decimal("0.01") for x in pcts)
    assert Decimal("0.014") not in pcts


def test_buy_returns_none_when_every_candidate_occupied(bot_minimal):
    """Если на каждом кандидате уже есть BUY — None (сетка + fallbacks исчерпаны)."""
    tick = Decimal("0.01")
    with patch.object(bot_minimal, "_open_buy_at_tick", return_value=True):
        assert bot_minimal.find_next_free_buy_price_down(Decimal("100"), tick) is None


def test_sell_returns_none_when_every_candidate_occupied(bot_minimal):
    tick = Decimal("0.01")
    with patch.object(bot_minimal, "_open_sell_at_tick", return_value=True):
        assert bot_minimal.find_next_free_sell_price_up(Decimal("100"), tick) is None
