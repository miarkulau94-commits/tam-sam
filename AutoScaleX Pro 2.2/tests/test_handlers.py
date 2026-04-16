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
