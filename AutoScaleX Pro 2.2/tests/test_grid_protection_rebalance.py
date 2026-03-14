"""
Unit-тесты для модулей grid_protection и rebalance (отмена BUY, защита по порогам, ребаланс).
"""

import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import Order

import grid_protection
import rebalance


class TestGridProtectionCancel:
    """Тесты grid_protection.cancel_last_n_buy_orders."""

    @pytest.fixture
    def bot_with_orders(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.orders = [
            Order("b1", "BUY", Decimal("99"), Decimal("0.1"), status="open"),
            Order("b2", "BUY", Decimal("98"), Decimal("0.1"), status="open"),
            Order("b3", "BUY", Decimal("97"), Decimal("0.1"), status="open"),
        ]
        bot.ex = MagicMock()
        bot.ex._request = AsyncMock(return_value={})
        return bot

    @pytest.mark.asyncio
    async def test_cancel_last_n_returns_zero_when_no_open_buy(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.orders = [Order("s1", "SELL", Decimal("101"), Decimal("0.1"), status="open")]
        bot.ex = MagicMock()
        n = await grid_protection.cancel_last_n_buy_orders(bot, 5)
        assert n == 0
        bot.ex._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_last_n_cancels_lowest_prices_and_removes_from_orders(self, bot_with_orders):
        bot = bot_with_orders
        n = await grid_protection.cancel_last_n_buy_orders(bot, 2)
        assert n == 2
        assert bot.ex._request.call_count == 2
        open_buy = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]
        assert len(open_buy) == 1
        assert open_buy[0].price == Decimal("99")


class TestGridProtectionCreateBuyAtBottom:
    """Тесты grid_protection.create_buy_orders_at_bottom (границы: нет баланса, лимит достигнут)."""

    @pytest.fixture
    def bot_mock(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.base_asset_name = "ETH"
        bot.grid_step_pct = Decimal("0.015")
        bot.buy_order_value = Decimal("50")
        bot.orders = [
            Order("b1", "BUY", Decimal("100"), Decimal("0.1"), status="open"),
        ]
        bot.ex = MagicMock()
        bot.ex.symbol_info = AsyncMock(
            return_value={
                "stepSize": Decimal("0.0001"),
                "tickSize": Decimal("0.01"),
                "minQty": Decimal("0.0001"),
                "minNotional": Decimal("0"),
            }
        )
        bot.ex.available_balance = AsyncMock(return_value=Decimal("1000"))
        bot.ex.balance = AsyncMock(return_value=Decimal("1000"))
        bot.ex.place_limit = AsyncMock(return_value={"orderId": "new_1"})
        bot.get_required_notional = MagicMock(return_value=Decimal("0"))
        bot.get_max_buy_orders = MagicMock(return_value=60)
        return bot

    @pytest.mark.asyncio
    async def test_create_buy_at_bottom_returns_zero_when_insufficient_balance(self, bot_mock):
        bot_mock.ex.available_balance = AsyncMock(return_value=Decimal("1"))
        n = await grid_protection.create_buy_orders_at_bottom(bot_mock, Decimal("100"))
        assert n == 0
        bot_mock.ex.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_buy_at_bottom_places_first_order_at_multiplicative_step(self, bot_mock):
        """Первый добавленный BUY внизу на ~1.5% ниже минимального существующего BUY (мультипликативный шаг)."""
        bot_mock.ex.place_limit = AsyncMock(return_value={"orderId": "new_1"})
        n = await grid_protection.create_buy_orders_at_bottom(bot_mock, Decimal("100"))
        assert n >= 1
        first_call = bot_mock.ex.place_limit.call_args_list[0]
        placed_price = first_call[0][3]
        # Минимальный BUY в bot_mock = 100, шаг 1.5% → первый новый уровень 100 * 0.985 = 98.5
        assert placed_price == Decimal("98.5")


class TestRebalanceCheck:
    """Тесты rebalance.check_rebalancing (остановленный бот не ребалансирует)."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.mark.asyncio
    async def test_check_rebalancing_does_nothing_when_stopped(self, temp_dirs):
        from trading_bot import BotState, TradingBot

        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_rebal_stop")
        os.makedirs(trades_dir, exist_ok=True)
        mock_ex = MagicMock()
        mock_ex.open_orders = AsyncMock(return_value=[])
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_ex),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(1, "k", "s", symbol="ETH-USDT")
            bot.state = BotState.STOPPED
            bot.orders = []
            await rebalance.check_rebalancing(bot, Decimal("100"))
        mock_ex.place_market.assert_not_called()
