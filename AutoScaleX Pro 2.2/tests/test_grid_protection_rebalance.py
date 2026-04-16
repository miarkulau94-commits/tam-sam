"""
Unit-тесты для модулей grid_protection и rebalance (отмена BUY, create_buy_orders_at_bottom, ребаланс).
"""

import os
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal
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

        def _align_to_tick(price, tick):
            if not tick or tick <= 0:
                return price
            return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        bot._align_to_tick = MagicMock(side_effect=_align_to_tick)
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

    @pytest.mark.asyncio
    async def test_create_buy_at_bottom_no_existing_buy_uses_current_price_branch(self):
        """Ветка «нет открытых BUY» — старт от current_price * (1 - step). Plain object (не MagicMock), чтобы orders не итерировался как mock."""
        class _Bot:
            pass

        bot = _Bot()
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.base_asset_name = "ETH"
        bot.grid_step_pct = Decimal("0.015")
        bot.buy_order_value = Decimal("50")
        bot.orders = []
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
        bot.ex.place_limit = AsyncMock(return_value={"orderId": "bottom_1"})

        def _req(_min):
            return Decimal("0")

        bot.get_required_notional = _req
        bot.get_max_buy_orders = lambda: 60

        def _align_to_tick(price, tick):
            if not tick or tick <= 0:
                return price
            return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        bot._align_to_tick = _align_to_tick
        n = await grid_protection.create_buy_orders_at_bottom(bot, Decimal("200"))
        assert n >= 1
        # place_limit вызывается до 5 раз — берём первый вызов (первый уровень внизу)
        first_price = bot.ex.place_limit.call_args_list[0][0][3]
        tick = Decimal("0.01")
        start = Decimal("200") * (Decimal("1") - Decimal("0.015"))
        assert first_price == _align_to_tick(start, tick)

    @pytest.mark.asyncio
    async def test_create_buy_at_bottom_limit_reached_debug_return(self, bot_mock):
        """len(open_buy) >= max_allowed_buy → возврат 0 без place_limit."""
        bot_mock.orders = [Order(str(i), "BUY", Decimal("100"), Decimal("0.1"), status="open") for i in range(65)]
        bot_mock.get_max_buy_orders = MagicMock(return_value=60)
        n = await grid_protection.create_buy_orders_at_bottom(bot_mock, Decimal("100"))
        assert n == 0
        bot_mock.ex.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_last_n_outer_exception_returns_zero(self):
        class BadBot:
            symbol = "ETH-USDT"

            def __init__(self):
                self.ex = MagicMock()

            @property
            def orders(self):
                raise RuntimeError("boom")

        n = await grid_protection.cancel_last_n_buy_orders(BadBot(), 1)
        assert n == 0

    @pytest.mark.asyncio
    async def test_cancel_last_n_single_order_failure_still_counts(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.orders = [Order("b1", "BUY", Decimal("50"), Decimal("0.1"), status="open")]
        bot.ex = MagicMock()
        bot.ex._request = AsyncMock(side_effect=RuntimeError("cancel failed"))
        n = await grid_protection.cancel_last_n_buy_orders(bot, 1)
        assert n == 0


class TestGridProtectionCreateBuyValidation:
    """Ветки цикла: низкая цена, ошибка place_limit, валидация qty, внешнее исключение."""

    @pytest.mark.asyncio
    async def test_price_too_low_stops_loop(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.grid_step_pct = Decimal("0.5")
        bot.buy_order_value = Decimal("10")
        bot.orders = [Order("b0", "BUY", Decimal("0.0001"), Decimal("1"), status="open")]
        bot.ex = MagicMock()
        bot.ex.symbol_info = AsyncMock(
            return_value={
                "stepSize": Decimal("0.0001"),
                "tickSize": Decimal("0.01"),
                "minQty": Decimal("0.0001"),
                "minNotional": Decimal("0"),
            }
        )
        bot.ex.available_balance = AsyncMock(return_value=Decimal("10000"))
        bot.ex.balance = AsyncMock(return_value=Decimal("10000"))
        bot.ex.place_limit = AsyncMock(return_value={"orderId": "x"})
        bot.get_required_notional = MagicMock(return_value=Decimal("0"))
        bot.get_max_buy_orders = MagicMock(return_value=60)
        bot._align_to_tick = MagicMock(return_value=Decimal("0"))

        n = await grid_protection.create_buy_orders_at_bottom(bot, Decimal("1"))
        assert n == 0
        bot.ex.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_limit_exception_logged_and_continue_attempt(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.grid_step_pct = Decimal("0.015")
        bot.buy_order_value = Decimal("50")
        bot.orders = [Order("b0", "BUY", Decimal("100"), Decimal("0.1"), status="open")]
        bot.ex = MagicMock()
        bot.ex.symbol_info = AsyncMock(
            return_value={
                "stepSize": Decimal("0.0001"),
                "tickSize": Decimal("0.01"),
                "minQty": Decimal("0.0001"),
                "minNotional": Decimal("0"),
            }
        )
        bot.ex.available_balance = AsyncMock(return_value=Decimal("5000"))
        bot.ex.balance = AsyncMock(return_value=Decimal("5000"))
        bot.ex.place_limit = AsyncMock(side_effect=RuntimeError("network"))
        bot.get_required_notional = MagicMock(return_value=Decimal("0"))
        bot.get_max_buy_orders = MagicMock(return_value=60)

        def align(price, tick):
            return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        bot._align_to_tick = MagicMock(side_effect=align)
        n = await grid_protection.create_buy_orders_at_bottom(bot, Decimal("100"))
        assert n == 0

    @pytest.mark.asyncio
    async def test_qty_below_min_logs_warning(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.grid_step_pct = Decimal("0.015")
        bot.buy_order_value = Decimal("1")
        bot.orders = [Order("b0", "BUY", Decimal("100"), Decimal("0.1"), status="open")]
        bot.ex = MagicMock()
        bot.ex.symbol_info = AsyncMock(
            return_value={
                "stepSize": Decimal("0.0001"),
                "tickSize": Decimal("0.01"),
                "minQty": Decimal("10"),
                "minNotional": Decimal("0"),
            }
        )
        bot.ex.available_balance = AsyncMock(return_value=Decimal("5000"))
        bot.ex.balance = AsyncMock(return_value=Decimal("5000"))
        bot.get_required_notional = MagicMock(return_value=Decimal("0"))
        bot.get_max_buy_orders = MagicMock(return_value=60)

        def align(price, tick):
            return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        bot._align_to_tick = MagicMock(side_effect=align)
        n = await grid_protection.create_buy_orders_at_bottom(bot, Decimal("100"))
        assert n == 0
        bot.ex.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_symbol_info_exception_returns_zero(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.grid_step_pct = Decimal("0.015")
        bot.buy_order_value = Decimal("50")
        bot.orders = []
        bot.ex = MagicMock()
        bot.ex.symbol_info = AsyncMock(side_effect=OSError("down"))
        n = await grid_protection.create_buy_orders_at_bottom(bot, Decimal("100"))
        assert n == 0

    @pytest.mark.asyncio
    async def test_loop_breaks_when_available_drops_below_order_value(self):
        """Стр. 101–104: на второй итерации available < buy_order_value — break без второго place."""
        class _B:
            pass

        b = _B()
        b.symbol = "ETH-USDT"
        b.quote_asset_name = "USDT"
        b.grid_step_pct = Decimal("0.015")
        b.buy_order_value = Decimal("50")
        b.orders = [Order(str(i), "BUY", Decimal("100"), Decimal("0.1"), status="open") for i in range(63)]
        b.ex = MagicMock()
        b.ex.symbol_info = AsyncMock(
            return_value={
                "stepSize": Decimal("0.0001"),
                "tickSize": Decimal("0.01"),
                "minQty": Decimal("0.0001"),
                "minNotional": Decimal("0"),
            }
        )
        b.ex.balance = AsyncMock(return_value=Decimal("10000"))
        # pre-check, затем первая итерация — ок, вторая — мало quote
        b.ex.available_balance = AsyncMock(side_effect=[Decimal("10000"), Decimal("10000"), Decimal("10")])
        b.ex.place_limit = AsyncMock(return_value={"orderId": "p1"})
        b.get_required_notional = lambda _m: Decimal("0")
        b.get_max_buy_orders = lambda: 60

        def _align(price, tick):
            return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

        b._align_to_tick = _align
        n = await grid_protection.create_buy_orders_at_bottom(b, Decimal("200"))
        assert n == 1
        assert b.ex.place_limit.call_count == 1


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


class TestRebalancingApply:
    """Тесты rebalance._rebalancing_apply_after_market_buy."""

    @pytest.mark.asyncio
    async def test_returns_false_without_order_id(self):
        bot = MagicMock()
        assert await rebalance._rebalancing_apply_after_market_buy(bot, {}, Decimal("1")) is False
        assert await rebalance._rebalancing_apply_after_market_buy(bot, {"x": 1}, Decimal("1")) is False

    @pytest.mark.asyncio
    async def test_success_calls_rebuild_sell_sync_and_bottom_buy_when_sell_count_high(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.base_asset_name = "ETH"
        bot.quote_asset_name = "USDT"
        bot.position_manager = MagicMock()
        bot.ex.get_order = AsyncMock(return_value={"price": "2000", "executedQty": "0.05"})
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.balance = AsyncMock(side_effect=[Decimal("0.05"), Decimal("1000")])
        bot.get_current_price = AsyncMock(return_value=Decimal("2000"))
        bot.rebuild_buy_grid_from_price = AsyncMock()
        bot.create_sell_grid_only = AsyncMock(return_value=3)
        bot.save_state = MagicMock()
        bot._cancelled_buy_for_rebalance_prep = True
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance.create_buy_orders_at_bottom", new_callable=AsyncMock
        ) as m_bottom:
            m_bottom.return_value = 1
            ok = await rebalance._rebalancing_apply_after_market_buy(bot, {"orderId": "m1"}, Decimal("1999"))
        assert ok is True
        assert bot._cancelled_buy_for_rebalance_prep is False
        bot.save_state.assert_called_once()
        m_bottom.assert_called_once()
        bot.position_manager.add_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_bottom_buy_when_sell_created_below_three(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.base_asset_name = "ETH"
        bot.quote_asset_name = "USDT"
        bot.position_manager = MagicMock()
        bot.ex.get_order = AsyncMock(return_value=None)
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.balance = AsyncMock(side_effect=[Decimal("0"), Decimal("1000")])
        bot.get_current_price = AsyncMock(return_value=Decimal("2000"))
        bot.rebuild_buy_grid_from_price = AsyncMock()
        bot.create_sell_grid_only = AsyncMock(return_value=2)
        bot.save_state = MagicMock()
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance.create_buy_orders_at_bottom", new_callable=AsyncMock
        ) as m_bottom:
            await rebalance._rebalancing_apply_after_market_buy(bot, {"orderId": "m1"}, Decimal("2000"))
        m_bottom.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_order_exception_does_not_break_flow(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.base_asset_name = "ETH"
        bot.quote_asset_name = "USDT"
        bot.position_manager = MagicMock()
        bot.ex.get_order = AsyncMock(side_effect=RuntimeError("api"))
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.balance = AsyncMock(side_effect=[Decimal("0"), Decimal("1000")])
        bot.get_current_price = AsyncMock(return_value=Decimal("2000"))
        bot.rebuild_buy_grid_from_price = AsyncMock()
        bot.create_sell_grid_only = AsyncMock(return_value=0)
        bot.save_state = MagicMock()
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance.create_buy_orders_at_bottom", new_callable=AsyncMock
        ):
            await rebalance._rebalancing_apply_after_market_buy(bot, {"orderId": "m1"}, Decimal("2000"))
        bot.position_manager.add_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_current_price_exception_uses_passed_price_for_rebuild(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.base_asset_name = "ETH"
        bot.quote_asset_name = "USDT"
        bot.position_manager = MagicMock()
        bot.ex.get_order = AsyncMock(return_value=None)
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.balance = AsyncMock(side_effect=[Decimal("0"), Decimal("1000")])
        bot.get_current_price = AsyncMock(side_effect=ConnectionError("ws down"))
        bot.rebuild_buy_grid_from_price = AsyncMock()
        bot.create_sell_grid_only = AsyncMock(return_value=0)
        bot.save_state = MagicMock()
        fallback = Decimal("1234.56")
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance.create_buy_orders_at_bottom", new_callable=AsyncMock
        ):
            await rebalance._rebalancing_apply_after_market_buy(bot, {"orderId": "m1"}, fallback)
        bot.rebuild_buy_grid_from_price.assert_called_once_with(fallback)

    @pytest.mark.asyncio
    async def test_rebuild_buy_raises_still_runs_sell_grid(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.base_asset_name = "ETH"
        bot.quote_asset_name = "USDT"
        bot.position_manager = MagicMock()
        bot.ex.get_order = AsyncMock(return_value=None)
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.balance = AsyncMock(side_effect=[Decimal("0"), Decimal("1000")])
        bot.get_current_price = AsyncMock(return_value=Decimal("2000"))
        bot.rebuild_buy_grid_from_price = AsyncMock(side_effect=ValueError("grid"))
        bot.create_sell_grid_only = AsyncMock(return_value=0)
        bot.save_state = MagicMock()
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance.create_buy_orders_at_bottom", new_callable=AsyncMock
        ):
            ok = await rebalance._rebalancing_apply_after_market_buy(bot, {"orderId": "m1"}, Decimal("2000"))
        assert ok is True
        bot.create_sell_grid_only.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_sell_grid_raises_treated_as_zero(self):
        bot = MagicMock()
        bot.symbol = "ETH-USDT"
        bot.base_asset_name = "ETH"
        bot.quote_asset_name = "USDT"
        bot.position_manager = MagicMock()
        bot.ex.get_order = AsyncMock(return_value=None)
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.balance = AsyncMock(side_effect=[Decimal("0"), Decimal("1000")])
        bot.get_current_price = AsyncMock(return_value=Decimal("2000"))
        bot.rebuild_buy_grid_from_price = AsyncMock()
        bot.create_sell_grid_only = AsyncMock(side_effect=RuntimeError("bingx"))
        bot.save_state = MagicMock()
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance.create_buy_orders_at_bottom", new_callable=AsyncMock
        ) as m_bottom:
            await rebalance._rebalancing_apply_after_market_buy(bot, {"orderId": "m1"}, Decimal("2000"))
        m_bottom.assert_not_called()


class TestCheckRebalancingBranches:
    """Тесты rebalance.check_rebalancing — market buy, недостаток баланса, ретраи."""

    def _bot_all_sell_gone(self):
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.base_asset_name = "ETH"
        bot.buy_order_value = Decimal("10")
        bot.orders = []
        bot.ex = MagicMock()
        bot.ex.open_orders = AsyncMock(return_value=[])
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("100"))
        bot.ex.balance = AsyncMock(return_value=Decimal("100"))
        bot.ex.place_market = AsyncMock(return_value={"orderId": "mk1"})
        return bot

    @pytest.mark.asyncio
    async def test_calls_apply_after_successful_market_buy(self):
        bot = self._bot_all_sell_gone()
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_called_once()
        bot.ex.place_market.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_apply_when_market_returns_without_order_id(self):
        bot = self._bot_all_sell_gone()
        bot.ex.place_market = AsyncMock(return_value={})
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_insufficient_for_full_uses_adjusted_quote_minus_reserve(self):
        """available < 5*buy+2, но > 1 — market buy на (available - 1)."""
        bot = self._bot_all_sell_gone()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("40"))
        bot.ex.place_market = AsyncMock(return_value={"orderId": "adj"})
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_called_once()
        assert bot.ex.place_market.call_args[1]["quote_order_qty"] == Decimal("39")

    @pytest.mark.asyncio
    async def test_insufficient_error_on_first_buy_retries_with_adjusted_amount(self):
        bot = self._bot_all_sell_gone()
        bot.ex.place_market = AsyncMock(
            side_effect=[
                Exception("insufficient balance for order"),
                {"orderId": "retry1"},
            ]
        )
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        assert bot.ex.place_market.call_count == 2
        assert bot.ex.place_market.call_args_list[1][1]["quote_order_qty"] == Decimal("99")
        m_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_permission_denied_inner_raise_caught_by_outer_handler(self):
        """Внутренний raise Permission denied перехватывается внешним try в check_rebalancing (лог, без проброса)."""
        bot = self._bot_all_sell_gone()
        bot.ex.place_market = AsyncMock(side_effect=RuntimeError("Permission denied"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_orders_error_treats_exchange_as_no_sells(self):
        bot = self._bot_all_sell_gone()
        bot.ex.open_orders = AsyncMock(side_effect=OSError("timeout"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_insufficient_retry_returns_empty_no_apply(self):
        bot = self._bot_all_sell_gone()
        bot.ex.place_market = AsyncMock(
            side_effect=[
                Exception("insufficient balance"),
                {},
            ]
        )
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        assert bot.ex.place_market.call_count == 2
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_insufficient_retry_second_call_raises(self):
        bot = self._bot_all_sell_gone()
        bot.ex.place_market = AsyncMock(
            side_effect=[
                Exception("insufficient balance"),
                RuntimeError("secondary failure"),
            ]
        )
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_market_error_not_insufficient(self):
        bot = self._bot_all_sell_gone()
        bot.ex.place_market = AsyncMock(side_effect=RuntimeError("Network timeout"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_insufficient_but_quote_le_one_skips_retry(self):
        """Первая ветка (full buy), insufficient, available ≤ 1 — без второго place_market."""
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.symbol = "ETH-USDT"
        bot.quote_asset_name = "USDT"
        bot.buy_order_value = Decimal("-0.4")
        bot.orders = []
        bot.ex = MagicMock()
        bot.ex.open_orders = AsyncMock(return_value=[])
        bot.ex.invalidate_balance_cache = AsyncMock()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("0.5"))
        bot.ex.balance = AsyncMock(return_value=Decimal("0.5"))
        bot.ex.place_market = AsyncMock(side_effect=Exception("insufficient balance"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        bot.ex.place_market.assert_called_once()
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_available_branch_market_returns_empty(self):
        bot = self._bot_all_sell_gone()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("40"))
        bot.ex.place_market = AsyncMock(return_value={})
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()
        assert bot.ex.place_market.call_args[1]["quote_order_qty"] == Decimal("39")

    @pytest.mark.asyncio
    async def test_low_available_branch_insufficient_error(self):
        bot = self._bot_all_sell_gone()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("40"))
        bot.ex.place_market = AsyncMock(side_effect=Exception("Insufficient balance"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_available_branch_other_error(self):
        bot = self._bot_all_sell_gone()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("40"))
        bot.ex.place_market = AsyncMock(side_effect=RuntimeError("bad request"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        m_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_available_not_enough_for_adjusted_path(self):
        bot = self._bot_all_sell_gone()
        bot.ex.available_balance = AsyncMock(return_value=Decimal("0.5"))
        with patch("rebalance.asyncio.sleep", new_callable=AsyncMock), patch(
            "rebalance._rebalancing_apply_after_market_buy", new_callable=AsyncMock
        ) as m_apply:
            await rebalance.check_rebalancing(bot, Decimal("2000"))
        bot.ex.place_market.assert_not_called()
        m_apply.assert_not_called()


class TestCheckRebalancingAfterAllBuyFilled:
    """Тесты rebalance.check_rebalancing_after_all_buy_filled."""

    @pytest.mark.asyncio
    async def test_creates_critical_sell_when_vwap_positive(self):
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.symbol = "ETH-USDT"
        bot.orders = []
        bot.grid_step_pct = Decimal("0.015")
        bot.ex.open_orders = AsyncMock(return_value=[])
        bot.calculate_vwap = AsyncMock(return_value=Decimal("12.34"))
        bot.create_critical_sell_grid = AsyncMock(return_value={"created_count": 2})
        await rebalance.check_rebalancing_after_all_buy_filled(bot, Decimal("1"))
        bot.create_critical_sell_grid.assert_called_once_with(vwap_source="auto_after_all_buy")

    @pytest.mark.asyncio
    async def test_does_not_create_grid_when_vwap_zero(self):
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.orders = []
        bot.grid_step_pct = Decimal("0.015")
        bot.ex.open_orders = AsyncMock(return_value=[])
        bot.calculate_vwap = AsyncMock(return_value=Decimal("0"))
        bot.create_critical_sell_grid = AsyncMock()
        await rebalance.check_rebalancing_after_all_buy_filled(bot, Decimal("1"))
        bot.create_critical_sell_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_warning_when_created_count_zero(self):
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.orders = []
        bot.grid_step_pct = Decimal("0.015")
        bot.ex.open_orders = AsyncMock(return_value=[])
        bot.calculate_vwap = AsyncMock(return_value=Decimal("10"))
        bot.create_critical_sell_grid = AsyncMock(return_value={"created_count": 0})
        await rebalance.check_rebalancing_after_all_buy_filled(bot, Decimal("1"))
        bot.create_critical_sell_grid.assert_called_once_with(vwap_source="auto_after_all_buy")

    @pytest.mark.asyncio
    async def test_outer_exception_when_open_orders_fails(self):
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.orders = []
        bot.ex.open_orders = AsyncMock(side_effect=OSError("unavailable"))
        await rebalance.check_rebalancing_after_all_buy_filled(bot, Decimal("1"))
        bot.calculate_vwap.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_auto_vwap_when_exchange_sell_count_at_threshold(self):
        """ТЗ п.9.1: при open_SELL ≥ 60 (1.5%) авто-VWAP не вызывается; счёт по exchange_orders."""
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.symbol = "ETH-USDT"
        bot.orders = []
        bot.grid_step_pct = Decimal("0.015")
        bot.ex.open_orders = AsyncMock(
            return_value=[{"side": "SELL", "orderId": str(i)} for i in range(60)]
        )
        bot.calculate_vwap = AsyncMock(return_value=Decimal("10"))
        bot.create_critical_sell_grid = AsyncMock()
        await rebalance.check_rebalancing_after_all_buy_filled(bot, Decimal("1"))
        bot.create_critical_sell_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_auto_vwap_when_exchange_sell_count_at_threshold_075_tz_p13(self):
        """ТЗ п.9.1 / п.13: при шаге 0.75% и open_SELL ≥ 120 авто-VWAP не вызывается."""
        from trading_bot import BotState

        bot = MagicMock()
        bot.state = BotState.TRADING
        bot.symbol = "ETH-USDT"
        bot.orders = []
        bot.grid_step_pct = Decimal("0.0075")
        bot.ex.open_orders = AsyncMock(
            return_value=[{"side": "SELL", "orderId": str(i)} for i in range(120)]
        )
        bot.calculate_vwap = AsyncMock(return_value=Decimal("10"))
        bot.create_critical_sell_grid = AsyncMock()
        await rebalance.check_rebalancing_after_all_buy_filled(bot, Decimal("1"))
        bot.create_critical_sell_grid.assert_not_called()
