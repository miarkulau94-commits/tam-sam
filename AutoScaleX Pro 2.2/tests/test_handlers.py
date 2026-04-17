"""Точечные тесты handlers (ранний выход, без полного trading_bot)."""
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
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


@pytest.mark.asyncio
async def test_handle_buy_filled_micro_shortage_caps_qty_and_places_sell():
    """
    Free чуть меньше hedge (комиссия BUY + step): разрыв > 0.1% sell_qty, но <= 3*FEE_RATE*sell_qty —
    срабатывает Micro shortage, place_limit с урезанным qty (регрессия SKIPPED на AVAX 11:17 в логе).
    """
    from handlers import handle_buy_filled
    from trading_bot import BotState

    price = Decimal("9.57")
    order_qty = Decimal("2.089864")
    order = Order("buy-avax", "BUY", price, order_qty, status="open")

    bot = MagicMock()
    bot.state = BotState.TRADING
    bot.user_id = 508265586
    bot.symbol = "AVAX-USDT"
    bot.base_asset_name = "AVAX"
    bot.quote_asset_name = "USDT"
    bot.orders = []
    bot.profit_bank = Decimal("0")
    bot.total_executed_buys = 0
    bot._cancelled_buy_for_rebalance_prep = False
    bot.grid_step_pct = Decimal("0.015")

    bot.position_manager.add_position = MagicMock()
    bot.statistics.save_trade = MagicMock()
    bot.get_total_equity = AsyncMock(return_value=Decimal("1000"))
    bot.find_next_free_sell_price_up = MagicMock(return_value=Decimal("9.71"))
    bot.get_required_notional = MagicMock(return_value=Decimal("0"))
    bot.sync_orders_from_exchange = AsyncMock()
    bot.check_rebalancing_after_all_buy_filled = AsyncMock()
    bot.maybe_process_tail_grid = AsyncMock()
    bot.create_buy_orders_at_bottom = AsyncMock(return_value=0)
    bot.check_critical_level = AsyncMock()
    bot.check_pyramiding = AsyncMock()
    bot.save_state = MagicMock()

    bot.ex.balance = AsyncMock(
        side_effect=[
            Decimal("14.48514"),
            Decimal("523.5"),
            Decimal("14.48514"),
            Decimal("14.48514"),
        ]
    )
    bot.ex.available_balance = AsyncMock(side_effect=[Decimal("2.08514"), Decimal("2.08514")])
    bot.ex.invalidate_balance_cache = AsyncMock()
    bot.ex.symbol_info = AsyncMock(
        return_value={
            "stepSize": Decimal("0.001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.01"),
            "minNotional": Decimal("5"),
        }
    )
    bot.ex.place_limit = AsyncMock(return_value={"orderId": "sell-micro-1"})

    async def _sleep(_):
        return None

    async def _to_thread(fn, *a, **kw):
        return fn(*a, **kw) if a else fn()

    with patch("handlers.asyncio.sleep", side_effect=_sleep), patch("handlers.asyncio.to_thread", side_effect=_to_thread):
        await handle_buy_filled(bot, order, price)

    bot.ex.place_limit.assert_awaited()
    call = bot.ex.place_limit.await_args
    assert call[0][1] == "SELL"
    placed_qty = call[0][2]
    placed_price = call[0][3]
    assert placed_price == Decimal("9.71")
    assert placed_qty == Decimal("2.085")
    assert placed_qty < order_qty * (Decimal("1") - config.FEE_RATE)
    assert any(o.order_id == "sell-micro-1" and o.side == "SELL" for o in bot.orders)
