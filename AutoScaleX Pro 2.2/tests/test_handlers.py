"""Точечные тесты handlers (ранний выход, без полного trading_bot)."""
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import Order


@pytest.mark.asyncio
async def test_handle_buy_filled_stopped_does_not_touch_exchange():
    from handlers import handle_buy_filled
    from trading_bot import BotState

    bot = MagicMock()
    bot.state = BotState.STOPPED
    bot.user_id = 1
    bot.symbol = "TEST-USDT"
    order = Order("o1", "BUY", Decimal("10"), Decimal("0.5"), status="open")
    await handle_buy_filled(bot, order, Decimal("10"))
    bot.ex.balance.assert_not_called()


@pytest.mark.asyncio
async def test_handle_sell_filled_stopped_does_not_touch_exchange():
    from handlers import handle_sell_filled
    from trading_bot import BotState

    bot = MagicMock()
    bot.state = BotState.STOPPED
    bot.user_id = 2
    bot.symbol = "TEST-USDT"
    order = Order("s1", "SELL", Decimal("11"), Decimal("0.5"), status="open")
    await handle_sell_filled(bot, order, Decimal("11"))
    bot.ex.balance.assert_not_called()


@pytest.mark.asyncio
async def test_handle_sell_filled_one_sell_runs_rebalance_prep_once():
    """При 1 открытом SELL и флаге False — вызывается cancel_last_n_buy_orders."""
    from handlers import handle_sell_filled
    from trading_bot import BotState

    sell_o = Order("s1", "SELL", Decimal("2"), Decimal("10"), status="open")
    buy_o = Order("b1", "BUY", Decimal("1"), Decimal("10"), status="open")
    bot = MagicMock()
    bot.state = BotState.TRADING
    bot.user_id = 1
    bot.symbol = "DOT-USDT"
    bot.quote_asset_name = "USDT"
    bot.orders = [sell_o, buy_o]
    bot._cancelled_buy_for_rebalance_prep = False
    bot.profit_bank = Decimal("0")
    bot.total_executed_sells = 0
    bot.position_manager.calculate_profit_for_sell = MagicMock(return_value=Decimal("1"))
    bot.ex.balance = AsyncMock(return_value=Decimal("100"))
    bot.ex.invalidate_balance_cache = AsyncMock()
    bot.ex.available_balance = AsyncMock(return_value=Decimal("50"))
    bot.get_total_equity = AsyncMock(return_value=Decimal("1000"))
    bot.statistics.save_trade = MagicMock()
    bot.create_buy_after_sell = AsyncMock(return_value=True)
    bot.sync_orders_from_exchange = AsyncMock()
    bot.cancel_last_n_buy_orders = AsyncMock(return_value=5)
    bot.check_rebalancing = AsyncMock()
    bot.check_pyramiding = AsyncMock()
    bot.save_state = MagicMock()
    order = Order("filled", "SELL", Decimal("2"), Decimal("10"), status="open")

    async def _to_thread(fn, *a, **kw):
        return fn(*a, **kw) if a else fn()

    with patch("handlers.asyncio.sleep", new_callable=AsyncMock), patch("handlers.asyncio.to_thread", side_effect=_to_thread):
        await handle_sell_filled(bot, order, Decimal("2"))

    bot.cancel_last_n_buy_orders.assert_awaited_once_with(5)
    assert bot._cancelled_buy_for_rebalance_prep is True


@pytest.mark.asyncio
async def test_handle_sell_filled_one_sell_skips_rebalance_prep_when_already_prepared():
    """При 1 SELL, но _cancelled_buy_for_rebalance_prep True — отмену BUY не повторяем."""
    from handlers import handle_sell_filled
    from trading_bot import BotState

    sell_o = Order("s1", "SELL", Decimal("2"), Decimal("10"), status="open")
    buy_o = Order("b1", "BUY", Decimal("1"), Decimal("10"), status="open")
    bot = MagicMock()
    bot.state = BotState.TRADING
    bot.user_id = 1
    bot.symbol = "DOT-USDT"
    bot.quote_asset_name = "USDT"
    bot.orders = [sell_o, buy_o]
    bot._cancelled_buy_for_rebalance_prep = True
    bot.profit_bank = Decimal("0")
    bot.total_executed_sells = 0
    bot.position_manager.calculate_profit_for_sell = MagicMock(return_value=Decimal("1"))
    bot.ex.balance = AsyncMock(return_value=Decimal("100"))
    bot.ex.invalidate_balance_cache = AsyncMock()
    bot.get_total_equity = AsyncMock(return_value=Decimal("1000"))
    bot.statistics.save_trade = MagicMock()
    bot.create_buy_after_sell = AsyncMock(return_value=True)
    bot.sync_orders_from_exchange = AsyncMock()
    bot.cancel_last_n_buy_orders = AsyncMock(return_value=5)
    bot.check_rebalancing = AsyncMock()
    bot.check_pyramiding = AsyncMock()
    bot.save_state = MagicMock()
    order = Order("filled", "SELL", Decimal("2"), Decimal("10"), status="open")

    async def _to_thread(fn, *a, **kw):
        return fn(*a, **kw) if a else fn()

    with patch("handlers.asyncio.sleep", new_callable=AsyncMock), patch("handlers.asyncio.to_thread", side_effect=_to_thread):
        await handle_sell_filled(bot, order, Decimal("2"))

    bot.cancel_last_n_buy_orders.assert_not_called()
