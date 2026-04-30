"""Очередь и flush отложенного парного SELL после SKIPPED (free base < hedge)."""
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import Order
from trading_bot import BotState, TradingBot


@pytest.fixture
def bot_pending():
    mock_ex = MagicMock()
    mock_ex.circuit_breaker = MagicMock()
    mock_ex.circuit_breaker.reset = MagicMock()
    with patch("trading_bot.BingXSpot", return_value=mock_ex), patch("trading_bot.BingXSpotAsync", return_value=mock_ex):
        b = TradingBot(999888, "k", "s", symbol="TEST-USDT")
    b.state = BotState.TRADING
    b.grid_step_pct = Decimal("0.015")
    b.orders = []
    return b


@pytest.mark.asyncio
async def test_try_flush_pending_hedge_places_sell_and_clears(bot_pending):
    bot = bot_pending
    bot.queue_pending_hedge_after_buy_skipped(Decimal("100"), Decimal("0.5"))
    bot.ex.symbol_info = AsyncMock(
        return_value={
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.1"),
            "minQty": Decimal("0.01"),
            "minNotional": Decimal("0"),
        }
    )
    bot.ex.invalidate_balance_cache = AsyncMock()
    bot.ex.available_balance = AsyncMock(return_value=Decimal("0.6"))
    bot.ex.place_limit = AsyncMock(return_value={"orderId": "hedge-flush-1"})
    bot.find_next_free_sell_price_up = MagicMock(return_value=Decimal("101.5"))

    await bot.try_flush_pending_hedge()

    assert bot._pending_hedge_buy_fill_price is None
    assert bot._pending_hedge_target_qty is None
    bot.ex.place_limit.assert_awaited_once()
    assert any(o.order_id == "hedge-flush-1" and o.side == "SELL" for o in bot.orders)


@pytest.mark.asyncio
async def test_try_flush_pending_hedge_no_op_when_not_trading(bot_pending):
    bot = bot_pending
    bot.queue_pending_hedge_after_buy_skipped(Decimal("1"), Decimal("1"))
    bot.state = BotState.PAUSED
    bot.ex.place_limit = AsyncMock()

    await bot.try_flush_pending_hedge()

    bot.ex.place_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_flush_skips_when_free_below_min_qty(bot_pending):
    bot = bot_pending
    bot.queue_pending_hedge_after_buy_skipped(Decimal("100"), Decimal("0.5"))
    bot.ex.symbol_info = AsyncMock(
        return_value={
            "stepSize": Decimal("0.01"),
            "tickSize": Decimal("0.1"),
            "minQty": Decimal("0.01"),
            "minNotional": Decimal("0"),
        }
    )
    bot.ex.invalidate_balance_cache = AsyncMock()
    bot.ex.available_balance = AsyncMock(return_value=Decimal("0.005"))
    bot.ex.place_limit = AsyncMock()
    bot.find_next_free_sell_price_up = MagicMock(return_value=Decimal("101"))

    await bot.try_flush_pending_hedge()

    bot.ex.place_limit.assert_not_awaited()
    assert bot._pending_hedge_target_qty == Decimal("0.5")


@pytest.mark.asyncio
async def test_try_flush_skips_when_below_min_volume(bot_pending):
    bot = bot_pending
    bot.queue_pending_hedge_after_buy_skipped(Decimal("100"), Decimal("0.5"))
    bot.ex.symbol_info = AsyncMock(
        return_value={
            "stepSize": Decimal("0.001"),
            "tickSize": Decimal("0.1"),
            "minQty": Decimal("0.001"),
            "minVolume": Decimal("0.027"),
            "minNotional": Decimal("0"),
        }
    )
    bot.ex.invalidate_balance_cache = AsyncMock()
    bot.ex.available_balance = AsyncMock(return_value=Decimal("0.001392"))
    bot.ex.place_limit = AsyncMock()
    bot.find_next_free_sell_price_up = MagicMock(return_value=Decimal("101"))

    await bot.try_flush_pending_hedge()

    bot.ex.place_limit.assert_not_awaited()
    assert bot._pending_hedge_target_qty == Decimal("0.5")
