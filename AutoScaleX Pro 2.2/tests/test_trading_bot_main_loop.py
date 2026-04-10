"""
Точечные тесты main_loop: STOPPED, один проход TRADING, INITIALIZING без ордеров на бирже.
"""
import asyncio
import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_bot import BotState, TradingBot


@pytest.fixture
def temp_dirs():
    with tempfile.TemporaryDirectory() as state_dir:
        with tempfile.TemporaryDirectory() as user_data_dir:
            yield state_dir, user_data_dir


@pytest.fixture
def mock_exchange():
    ex = MagicMock()
    ex.balance.return_value = Decimal("1000")
    ex.available_balance.return_value = Decimal("1000")
    ex.open_orders.return_value = []
    ex.price.return_value = Decimal("2000")
    ex.symbol_info.return_value = {
        "stepSize": Decimal("0.0001"),
        "tickSize": Decimal("0.01"),
        "minQty": Decimal("0.0001"),
        "minNotional": Decimal("0"),
        "status": "TRADING",
    }
    ex.circuit_breaker = MagicMock()
    ex.circuit_breaker.state = MagicMock()
    ex.invalidate_balance_cache = MagicMock()
    ex.place_limit = MagicMock(return_value={"orderId": "new1"})
    return ex


@pytest.mark.asyncio
async def test_main_loop_stopped_exits_without_sleep(temp_dirs, mock_exchange):
    """STOPPED: один заход в цикл, break до asyncio.sleep(15)."""
    state_dir, user_data_dir = temp_dirs
    trades_dir = os.path.join(tempfile.gettempdir(), "trades_main_loop_stopped")
    os.makedirs(trades_dir, exist_ok=True)
    sleep_mock = AsyncMock()

    with (
        patch("trading_bot.config.STATE_DIR", state_dir),
        patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
        patch("trading_bot.BingXSpot", return_value=mock_exchange),
        patch("persistence.config.STATE_DIR", state_dir),
        patch("persistence.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.asyncio.sleep", sleep_mock),
    ):
        bot = TradingBot(99901, "k", "s", symbol="ETH-USDT")
        bot.state = BotState.STOPPED
        await bot.main_loop()

    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_loop_trading_iteration_check_orders_then_sync_order(temp_dirs, mock_exchange):
    """TRADING: один цикл — check_orders, затем sync_orders_from_exchange; выход по CancelledError на sleep(15)."""
    state_dir, user_data_dir = temp_dirs
    trades_dir = os.path.join(tempfile.gettempdir(), "trades_main_loop_trading")
    os.makedirs(trades_dir, exist_ok=True)

    async def sleep_side_effect(delay):
        if delay == 15:
            raise asyncio.CancelledError()
        return None

    with (
        patch("trading_bot.config.STATE_DIR", state_dir),
        patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
        patch("trading_bot.BingXSpot", return_value=mock_exchange),
        patch("persistence.config.STATE_DIR", state_dir),
        patch("persistence.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.asyncio.sleep", side_effect=sleep_side_effect),
    ):
        bot = TradingBot(99902, "k", "s", symbol="ETH-USDT")
        bot.state = BotState.TRADING
        bot.check_orders = AsyncMock()
        bot.sync_orders_from_exchange = AsyncMock()
        bot.check_critical_situation = AsyncMock()
        bot.check_protection_add_five_buy_when_three_left = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await bot.main_loop()

    bot.check_orders.assert_awaited_once()
    bot.sync_orders_from_exchange.assert_awaited_once()
    bot.check_critical_situation.assert_awaited_once()
    bot.check_protection_add_five_buy_when_three_left.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_loop_initializing_no_exchange_orders_calls_create_grid_once(temp_dirs, mock_exchange):
    """INITIALIZING: open_orders пуст — create_grid, переход в TRADING; выход на первом sleep(15)."""
    state_dir, user_data_dir = temp_dirs
    trades_dir = os.path.join(tempfile.gettempdir(), "trades_main_loop_init")
    os.makedirs(trades_dir, exist_ok=True)
    mock_exchange.open_orders.return_value = []

    async def sleep_side_effect(delay):
        if delay == 15:
            raise asyncio.CancelledError()
        if delay == 60:
            return None
        return None

    with (
        patch("trading_bot.config.STATE_DIR", state_dir),
        patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
        patch("trading_bot.BingXSpot", return_value=mock_exchange),
        patch("persistence.config.STATE_DIR", state_dir),
        patch("persistence.config.USER_DATA_DIR", user_data_dir),
        patch("trading_bot.asyncio.sleep", side_effect=sleep_side_effect),
    ):
        bot = TradingBot(99903, "k", "s", symbol="ETH-USDT")
        bot.state = BotState.INITIALIZING
        bot.create_grid = AsyncMock()
        bot.get_total_equity = AsyncMock(return_value=Decimal("10000"))

        with pytest.raises(asyncio.CancelledError):
            await bot.main_loop()

    bot.create_grid.assert_awaited_once()
    assert bot.state == BotState.TRADING
