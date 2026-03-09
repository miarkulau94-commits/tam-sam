"""
Integration tests for trading_bot — Order, _deduplicate_orders, get_max_buy_orders, get_required_notional,
handle_buy_filled, handle_sell_filled, sync_orders_from_exchange,
get_min_open_orders_for_protection, check_protection_add_five_buy_when_three_left
"""

import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from trading_bot import BotState, Order, TradingBot


class TestOrder:
    """Тесты Order — сериализация и десериализация"""

    def test_to_dict_buy(self):
        o = Order("123", "BUY", Decimal("100"), Decimal("0.5"))
        d = o.to_dict()
        assert d["order_id"] == "123"
        assert d["side"] == "BUY"
        assert d["price"] == "100"
        assert d["qty"] == "0.5"
        assert d["status"] == "open"

    def test_to_dict_sell(self):
        o = Order("456", "SELL", Decimal("105"), Decimal("0.5"))
        d = o.to_dict()
        assert d["side"] == "SELL"
        assert "amount_usdt" in d

    def test_from_dict_roundtrip_buy(self):
        o = Order("oid1", "BUY", Decimal("100"), Decimal("1"))
        d = o.to_dict()
        o2 = Order.from_dict(d)
        assert o2.order_id == o.order_id
        assert o2.side == o.side
        assert o2.price == o.price
        assert o2.qty == o.qty
        assert o2.status == o.status

    def test_from_dict_roundtrip_sell(self):
        o = Order("oid2", "SELL", Decimal("110"), Decimal("0.5"))
        d = o.to_dict()
        o2 = Order.from_dict(d)
        assert o2.side == "SELL"
        assert o2.amount_usdt == o.price * o.qty


class TestTradingBotWithMockedExchange:
    """Интеграционные тесты TradingBot с замоканным exchange"""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.balance.return_value = Decimal("1000")
        ex.available_balance.return_value = Decimal("1000")
        ex.open_orders.return_value = []
        ex.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        ex.circuit_breaker = MagicMock()
        ex.circuit_breaker.state = MagicMock()
        return ex

    def test_get_max_buy_orders_075(self, temp_dirs, mock_exchange):
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.0075")
            assert bot.get_max_buy_orders() in (125, 130)

    def test_get_max_buy_orders_15(self, temp_dirs, mock_exchange):
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            assert bot.get_max_buy_orders() in (60, 65)

    def test_get_required_notional_positive(self, temp_dirs, mock_exchange):
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            assert bot.get_required_notional(Decimal("20")) == Decimal("20")
            assert bot.get_required_notional(Decimal("0")) == Decimal("0")

    def test_deduplicate_orders(self, temp_dirs, mock_exchange):
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            o1 = Order("id1", "BUY", Decimal("100"), Decimal("0.5"))
            o2 = Order("id1", "BUY", Decimal("100"), Decimal("0.5"))
            o3 = Order("id2", "SELL", Decimal("101"), Decimal("0.5"))
            bot.orders = [o1, o2, o3]
            bot._deduplicate_orders()
            assert len(bot.orders) == 2
            assert bot.orders[0].order_id == "id1"
            assert bot.orders[1].order_id == "id2"

    def test_load_state_resets_grid_step_when_saved_065(self, temp_dirs, mock_exchange):
        """При загрузке state с grid_step_pct '0.65' (ошибочный шаг) бот сбрасывает на config.GRID_STEP_PCT."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_grid065")
        os.makedirs(trades_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "user_12345.json")
        with open(state_file, "w", encoding="utf-8") as f:
            import json
            json.dump({
                "uid": "12345",
                "symbol": "ETH-USDT",
                "grid_step_pct": "0.65",
                "orders": [],
                "buy_order_value": "50",
            }, f)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
        assert bot.grid_step_pct == config.GRID_STEP_PCT

    def test_load_state_resets_grid_step_when_saved_00065(self, temp_dirs, mock_exchange):
        """При загрузке state с grid_step_pct '0.0065' (0.65%) бот сбрасывает на config.GRID_STEP_PCT."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_grid00065")
        os.makedirs(trades_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "user_12345.json")
        with open(state_file, "w", encoding="utf-8") as f:
            import json
            json.dump({
                "uid": "12345",
                "symbol": "ETH-USDT",
                "grid_step_pct": "0.0065",
                "orders": [],
                "buy_order_value": "50",
            }, f)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
        assert bot.grid_step_pct == config.GRID_STEP_PCT

    def test_load_state_profit_bank_from_user_data_overrides_state(self, temp_dirs, mock_exchange):
        """При загрузке state profit_bank подменяется из user_data/<uid>.json (источник истины)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_profit_bank")
        os.makedirs(trades_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "user_12345.json")
        with open(state_file, "w", encoding="utf-8") as f:
            import json
            json.dump({
                "uid": "35812365",
                "symbol": "KSM-USDT",
                "grid_step_pct": "0.0075",
                "profit_bank": "18.62247060161",
                "orders": [],
                "buy_order_value": "50",
            }, f)
        uid_file = os.path.join(user_data_dir, "35812365.json")
        with open(uid_file, "w", encoding="utf-8") as f:
            import json
            json.dump({
                "uid": "35812365",
                "trades": [],
                "settings": {"profit_bank": "-1.84514217971"},
                "total_trades": 0,
            }, f)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="KSM-USDT")
        assert bot.profit_bank == Decimal("-1.84514217971")

    def test_save_state_persists_cancelled_buy_for_rebalance_prep(self, temp_dirs, mock_exchange):
        """save_state записывает флаг cancelled_buy_for_rebalance_prep в state."""
        import json
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_rebalance_flag")
        os.makedirs(trades_dir, exist_ok=True)
        saved_state = []

        def capture_save(user_id, state):
            saved_state.append(state)

        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.persistence.save_state = capture_save
            bot._cancelled_buy_for_rebalance_prep = True
            bot.save_state()
        assert len(saved_state) == 1
        assert saved_state[0].get("cancelled_buy_for_rebalance_prep") is True

    def test_load_state_restores_cancelled_buy_for_rebalance_prep_true(self, temp_dirs, mock_exchange):
        """При загрузке state с cancelled_buy_for_rebalance_prep: true флаг восстанавливается."""
        import json
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_rebalance_load_true")
        os.makedirs(trades_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "user_12345.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "uid": "12345",
                "symbol": "ETH-USDT",
                "grid_step_pct": "0.0075",
                "orders": [],
                "buy_order_value": "50",
                "cancelled_buy_for_rebalance_prep": True,
            }, f)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
        assert bot._cancelled_buy_for_rebalance_prep is True

    def test_load_state_restores_cancelled_buy_for_rebalance_prep_false_when_missing(self, temp_dirs, mock_exchange):
        """При загрузке state без ключа cancelled_buy_for_rebalance_prep флаг остаётся False."""
        import json
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_rebalance_load_false")
        os.makedirs(trades_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "user_12345.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "uid": "12345",
                "symbol": "ETH-USDT",
                "grid_step_pct": "0.0075",
                "orders": [],
                "buy_order_value": "50",
            }, f)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
        assert bot._cancelled_buy_for_rebalance_prep is False


class TestTradingCycleIntegration:
    """Интеграционные тесты полного торгового цикла (handle_buy_filled, handle_sell_filled, sync)."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.balance.return_value = Decimal("1000")
        ex.available_balance.return_value = Decimal("1000")
        ex.open_orders.return_value = []
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
        ex.place_limit = MagicMock(return_value={"orderId": "new_sell_123"})
        return ex

    @pytest.mark.asyncio
    async def test_handle_buy_filled_adds_position(self, temp_dirs, mock_exchange):
        """После handle_buy_filled позиция добавляется в position_manager."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_cycle")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.side_effect = lambda a: Decimal("0.5") if "ETH" in a else Decimal("900")
        mock_exchange.available_balance.return_value = Decimal("900")
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            initial_buys = bot.total_executed_buys
            order = Order("buy1", "BUY", Decimal("100"), Decimal("0.005"), status="open")
            order.amount_usdt = Decimal("50")
            await bot.handle_buy_filled(order, Decimal("100"))
            assert order.status == "filled"
            assert len(bot.position_manager.positions) == 1
            assert bot.position_manager.positions[0].price == Decimal("100")
            assert bot.total_executed_buys == initial_buys + 1

    @pytest.mark.asyncio
    async def test_handle_buy_filled_uses_available_balance_for_sell_qty(self, temp_dirs, mock_exchange):
        """При создании SELL после BUY используется available_balance(base) с биржи для объёма."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_avail")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.side_effect = lambda a: Decimal("0.5") if "ETH" in a else Decimal("900")
        mock_exchange.available_balance.return_value = Decimal("0.01")  # free ETH для SELL
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0005"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        mock_exchange.place_limit.return_value = {"orderId": "sell_after_buy_1"}
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            order = Order("buy1", "BUY", Decimal("100"), Decimal("0.005"), status="open")
            order.amount_usdt = Decimal("50")
            await bot.handle_buy_filled(order, Decimal("100"))
        mock_exchange.available_balance.assert_called()
        calls = [c[0][0] for c in mock_exchange.available_balance.call_args_list]
        assert "ETH" in calls, "available_balance должен вызываться для базового актива (ETH)"

    @pytest.mark.asyncio
    async def test_sell_after_buy_price_rounds_to_nearest_tick(self, temp_dirs, mock_exchange):
        """После BUY цена SELL округляется до ближайшего тика: 1.44 * 1.015 = 1.4616 → 1.462 (tick 0.001), не 1.461 (floor)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_sell_tick")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.side_effect = lambda a: Decimal("0.01") if "DOT" in a else Decimal("500")
        mock_exchange.available_balance.return_value = Decimal("0.01")
        mock_exchange.place_limit.return_value = {"orderId": "sell_after_buy_tick"}
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.001"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.015")  # 1.5%
            order = Order("buy1", "BUY", Decimal("1.44"), Decimal("7"), status="open")
            order.amount_usdt = Decimal("10")
            await bot.handle_buy_filled(order, Decimal("1.44"))
        mock_exchange.place_limit.assert_called_once()
        call_args = mock_exchange.place_limit.call_args[0]
        assert call_args[1] == "SELL"
        placed_price = call_args[3]
        # 1.44 * 1.015 = 1.4616 → nearest tick 0.001 = 1.462 (не floor 1.461)
        assert placed_price == Decimal("1.462"), "SELL price must round to nearest tick (1.462), not floor (1.461)"

    @pytest.mark.asyncio
    async def test_handle_sell_filled_adds_profit(self, temp_dirs, mock_exchange):
        """После handle_sell_filled profit_bank увеличивается."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_cycle2")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.side_effect = lambda a: Decimal("0") if "ETH" in a else Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit.return_value = {"orderId": "new_buy_456"}
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.position_manager.add_position(Decimal("100"), Decimal("0.005"))
            initial_profit = bot.profit_bank
            initial_sells = bot.total_executed_sells
            order = Order("sell1", "SELL", Decimal("101"), Decimal("0.005"), status="open")
            await bot.handle_sell_filled(order, Decimal("101"))
            assert order.status == "filled"
            assert bot.profit_bank > initial_profit
            assert bot.total_executed_sells == initial_sells + 1

    @pytest.mark.asyncio
    async def test_sync_orders_adds_missing(self, temp_dirs, mock_exchange):
        """sync_orders_from_exchange добавляет ордера с биржи, которых нет в памяти."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_cycle3")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.open_orders.return_value = [
            {"orderId": "ex1", "side": "BUY", "price": "100", "origQty": "0.5"},
            {"orderId": "ex2", "side": "SELL", "price": "101", "origQty": "0.5"},
        ]
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.orders.clear()  # Имитируем пустую память — проверяем, что sync добавит ордера
            await bot.sync_orders_from_exchange()
            assert len(bot.orders) == 2
            order_ids = {o.order_id for o in bot.orders}
            assert "ex1" in order_ids
            assert "ex2" in order_ids

    @pytest.mark.asyncio
    async def test_sync_processes_filled_missing_order_and_creates_replacement(self, temp_dirs, mock_exchange):
        """Если в sync ордер есть в памяти но нет на бирже — запрашиваем статус; при FILLED обрабатываем как fill и создаём замену (SELL после BUY)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_sync_fill")
        os.makedirs(trades_dir, exist_ok=True)
        missing_buy = Order("missing_buy_1", "BUY", Decimal("1.51"), Decimal("6.62"), status="open")
        missing_buy.created_at = 0
        bot_orders = [missing_buy]
        for i in range(23):
            o = Order(f"buy_{i}", "BUY", Decimal("1.5") - Decimal(i) * Decimal("0.01"), Decimal("6.6"), status="open")
            o.created_at = 0
            bot_orders.append(o)
        for i in range(4):
            o = Order(f"sell_{i}", "SELL", Decimal("1.55") + Decimal(i) * Decimal("0.01"), Decimal("6.5"), status="open")
            o.created_at = 0
            bot_orders.append(o)
        exchange_open = [
            {"orderId": o.order_id, "side": o.side, "price": str(o.price), "origQty": str(o.qty)}
            for o in bot_orders if o.order_id != "missing_buy_1"
        ]
        mock_exchange.open_orders.return_value = exchange_open
        get_order_calls = []
        def get_order_sync(symbol, order_id):
            get_order_calls.append((symbol, order_id))
            if order_id == "missing_buy_1":
                return {"status": "FILLED", "price": "1.51", "executedQty": "6.62"}
            return None
        mock_exchange.get_order = get_order_sync
        mock_exchange.balance.side_effect = lambda a: Decimal("50") if "DOT" in a or "ETH" in a else Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_sell_1"})
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.orders = bot_orders
            bot.position_manager.add_position(Decimal("1.51"), Decimal("6.62"))
            await bot.sync_orders_from_exchange()
            assert missing_buy.status == "filled"
            assert get_order_calls == [("DOT-USDT", "missing_buy_1")]
            open_buy = len([o for o in bot.orders if o.side == "BUY" and o.status == "open"])
            open_sell = len([o for o in bot.orders if o.side == "SELL" and o.status == "open"])
            assert open_buy == 23
            assert open_sell == 5
            mock_exchange.place_limit.assert_called()
            sell_calls = [c for c in mock_exchange.place_limit.call_args_list if c[0][1] == "SELL"]
            assert len(sell_calls) >= 1

    @pytest.mark.asyncio
    async def test_create_buy_after_sell_at_max_returns_false_no_place_limit(self, temp_dirs, mock_exchange):
        """При 61 BUY и 4 открытых SELL (лимит после SELL = 61) create_buy_after_sell возвращает False и не вызывает place_limit."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_max")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "would_be_new"})
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")  # 1.5% -> max 60 BUY, после SELL разрешено 61–64
            # 61 BUY + 4 открытых SELL (1 SELL уже исполнился) — лимит 61 достигнут
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i) * Decimal("0.5"), Decimal("0.1"), status="open")
                for i in range(61)
            ] + [
                Order(f"sell_{j}", "SELL", Decimal("105") + Decimal(j), Decimal("0.1"), status="open")
                for j in range(4)
            ]
            assert bot.get_max_buy_orders() == 60
            result = await bot.create_buy_after_sell(Decimal("101"))
            assert result is False
            mock_exchange.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_buy_after_sell_allows_61_when_60_buy_4_sell(self, temp_dirs, mock_exchange):
        """После 1-го SELL: 60 BUY и 4 open SELL — разрешён 1 новый BUY (лимит 61), place_limit вызывается."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_61")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy_61"})
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="KSM-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.buy_order_value = Decimal("20")
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("4") - Decimal(i) * Decimal("0.01"), Decimal("5"), status="open")
                for i in range(60)
            ] + [
                Order(f"sell_{j}", "SELL", Decimal("4.5") + Decimal(j) * Decimal("0.02"), Decimal("5"), status="open")
                for j in range(4)
            ]
            result = await bot.create_buy_after_sell(Decimal("4.64"))
            assert result is True
            mock_exchange.place_limit.assert_called_once()
            assert len([o for o in bot.orders if o.side == "BUY" and o.status == "open"]) == 61

    @pytest.mark.asyncio
    async def test_create_buy_after_sell_price_rounds_to_nearest_tick_not_floor(self, temp_dirs, mock_exchange):
        """При шаге 1.5% и sell_price=1.46 цена BUY округляется до ближайшего тика (1.44), а не вниз (1.43)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_nearest_tick")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy_tick"})
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.015")  # 1.5%
            bot.buy_order_value = Decimal("10")
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("1.3") - Decimal(i) * Decimal("0.01"), Decimal("7"), status="open")
                for i in range(60)
            ] + [
                Order(f"sell_{j}", "SELL", Decimal("1.5") + Decimal(j) * Decimal("0.02"), Decimal("7"), status="open")
                for j in range(4)
            ]
            # 1.46 * (1 - 0.015) = 1.4381 -> nearest tick 0.01 = 1.44 (не 1.43)
            result = await bot.create_buy_after_sell(Decimal("1.46"))
            assert result is True
            mock_exchange.place_limit.assert_called_once()
            placed_price = mock_exchange.place_limit.call_args[0][3]
            assert placed_price == Decimal("1.44"), "BUY price must round to nearest tick (1.44), not floor (1.43)"

    @pytest.mark.asyncio
    async def test_create_buy_after_sell_allows_62_63_64_by_open_sell_count(self, temp_dirs, mock_exchange):
        """Лимит после SELL: 62 при 3 open SELL, 63 при 2, 64 при 1 — новый BUY выставляется."""
        state_dir, user_data_dir = temp_dirs
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy"})
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", os.path.join(tempfile.gettempdir(), "trades_62_64"), create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.buy_order_value = Decimal("10")
            for open_sell, open_buy in [(3, 61), (2, 62), (1, 63)]:
                mock_exchange.place_limit.reset_mock()
                bot.orders = [
                    Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                    for i in range(open_buy)
                ] + [
                    Order(f"sell_{j}", "SELL", Decimal("105") + Decimal(j), Decimal("0.1"), status="open")
                    for j in range(open_sell)
                ]
                result = await bot.create_buy_after_sell(Decimal("101"))
                assert result is True, f"open_sell={open_sell}, open_buy={open_buy}"
                mock_exchange.place_limit.assert_called_once()
                assert len([o for o in bot.orders if o.side == "BUY" and o.status == "open"]) == open_buy + 1

    @pytest.mark.asyncio
    async def test_create_buy_after_sell_fallback_step_when_primary_price_occupied(self, temp_dirs, mock_exchange):
        """При шаге 1.5%: если на основной цене (sell - 1.5%) уже есть BUY, выставляем BUY по fallback (sell - 1%)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_fallback")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy_fallback"})
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        # Синхронный мок: BingXSpotAsync вызывает методы через asyncio.to_thread();
        # AsyncMock в потоке возвращает корутину, которую никто не await'ит → RuntimeWarning.
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.015")  # 1.5%
            # sell_price=1.57 -> primary BUY price = 1.57*0.985 = 1.54645. Ставим BUY на 1.54 (в пределах tick 0.01)
            # fallback = 1.57*0.99 = 1.5543 -> по tick 1.55, на 1.55 BUY нет — должен выставиться
            bot.orders = [
                Order("buy_occupied", "BUY", Decimal("1.54"), Decimal("6.5"), status="open"),
            ]
            sell_price = Decimal("1.57")
            result = await bot.create_buy_after_sell(sell_price)
            assert result is True
            mock_exchange.place_limit.assert_called_once()
            call_kwargs = mock_exchange.place_limit.call_args
            assert call_kwargs[0][1] == "BUY"  # side
            # Цена должна быть fallback, округлённая по tick: 1.5543 -> 1.55
            placed_price = call_kwargs[0][3]
            assert placed_price == Decimal("1.55")
            assert len([o for o in bot.orders if o.side == "BUY" and o.status == "open"]) == 2

    @pytest.mark.asyncio
    async def test_create_buy_after_sell_fallback_step_075_when_primary_occupied(self, temp_dirs, mock_exchange):
        """При шаге 0.75%: если на основной цене (sell - 0.75%) уже есть BUY, выставляем BUY по fallback (sell - 0.5%)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_fallback075")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy_fb075"})
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.0075")  # 0.75%
            # sell_price=2.0 -> primary = 2.0*0.9925 = 1.985, fallback = 2.0*0.995 = 1.99
            # Занят уровень 1.98 (в пределах tick от 1.985), на 1.99 BUY нет
            bot.orders = [
                Order("buy_occupied", "BUY", Decimal("1.98"), Decimal("5"), status="open"),
            ]
            sell_price = Decimal("2.0")
            result = await bot.create_buy_after_sell(sell_price)
            assert result is True
            mock_exchange.place_limit.assert_called_once()
            placed_price = mock_exchange.place_limit.call_args[0][3]
            assert placed_price == Decimal("1.99")
            assert len([o for o in bot.orders if o.side == "BUY" and o.status == "open"]) == 2


class TestPyramidingFallback:
    """Тесты запасного шага в пирамидинге: при занятой основной цене — 1% (при шаге 1.5%) или 0.5% (при 0.75%)."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.balance.return_value = Decimal("1000")
        ex.available_balance.return_value = Decimal("1000")
        ex.open_orders.return_value = []
        ex.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        ex.circuit_breaker = MagicMock()
        ex.circuit_breaker.state = MagicMock()
        ex.invalidate_balance_cache = MagicMock(return_value=None)
        ex.place_limit = MagicMock(return_value={"orderId": "pyramid_buy_1"})
        return ex

    @pytest.mark.asyncio
    async def test_pyramiding_uses_fallback_15_when_main_price_occupied(self, temp_dirs, mock_exchange):
        """Пирамидинг при шаге 1.5%: при [1.50, 1.47] добавляется следующий уровень 1.44 (lowest - 1.5%)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_pyr_fb15")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.015")  # 1.5%
            bot.buy_order_value = Decimal("50")
            bot.profit_bank = Decimal("50")
            # lowest=1.47 -> следующий уровень 1.47*0.985=1.44
            bot.orders = [
                Order("a", "BUY", Decimal("1.50"), Decimal("10"), status="open"),
                Order("b", "BUY", Decimal("1.47"), Decimal("10"), status="open"),
            ]
            await bot.check_pyramiding()
        mock_exchange.place_limit.assert_called_once()
        placed_price = mock_exchange.place_limit.call_args[0][3]
        assert placed_price == Decimal("1.44"), "Следующий уровень сетки 1.47 - 1.5% = 1.44"

    @pytest.mark.asyncio
    async def test_pyramiding_uses_fallback_075_when_main_price_occupied(self, temp_dirs, mock_exchange):
        """Пирамидинг при шаге 0.75%: при [2.00, 1.98] добавляется следующий уровень 1.96 (lowest - 0.75%)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_pyr_fb075")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.0075")  # 0.75%
            bot.buy_order_value = Decimal("50")
            bot.profit_bank = Decimal("50")
            # lowest=1.98 -> следующий уровень 1.98*0.9925=1.96
            bot.orders = [
                Order("a", "BUY", Decimal("2.00"), Decimal("10"), status="open"),
                Order("b", "BUY", Decimal("1.98"), Decimal("10"), status="open"),
            ]
            await bot.check_pyramiding()
        mock_exchange.place_limit.assert_called_once()
        placed_price = mock_exchange.place_limit.call_args[0][3]
        assert placed_price == Decimal("1.96"), "Следующий уровень сетки 1.98 - 0.75% = 1.96"

    @pytest.mark.asyncio
    async def test_pyramiding_skips_when_both_main_and_fallback_occupied(self, temp_dirs, mock_exchange):
        """Пирамидинг: при занятых основной и запасной для одного уровня — ставим следующий уровень (lowest=1.47 -> 1.44)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_pyr_skip")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="DOT-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.buy_order_value = Decimal("50")
            bot.profit_bank = Decimal("50")
            # [1.50, 1.47, 1.48] -> lowest=1.47, следующий уровень 1.44 (свободен) — один ордер по 1.44
            bot.orders = [
                Order("lowest", "BUY", Decimal("1.50"), Decimal("10"), status="open"),
                Order("main_occupied", "BUY", Decimal("1.47"), Decimal("10"), status="open"),
                Order("fallback_occupied", "BUY", Decimal("1.48"), Decimal("10"), status="open"),
            ]
            await bot.check_pyramiding()
        mock_exchange.place_limit.assert_called_once()
        placed_price = mock_exchange.place_limit.call_args[0][3]
        assert placed_price == Decimal("1.44"), "Следующий уровень ниже 1.47 — 1.44"


class TestTradingCycleIntegrationContinued:
    """Продолжение TestTradingCycleIntegration: тесты, следующие за TestPyramidingFallback в файле."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.balance.return_value = Decimal("1000")
        ex.available_balance.return_value = Decimal("1000")
        ex.open_orders.return_value = []
        ex.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        ex.circuit_breaker = MagicMock()
        ex.circuit_breaker.state = MagicMock()
        ex.invalidate_balance_cache = MagicMock(return_value=None)
        ex.place_limit = MagicMock(return_value={"orderId": "new_sell_123"})
        return ex

    @pytest.mark.asyncio
    async def test_handle_sell_filled_at_max_profit_still_added_no_new_buy(self, temp_dirs, mock_exchange):
        """При 61 BUY и 4 открытых SELL после handle_sell_filled прибыль копится, новый BUY не создаётся (лимит 61)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_max2")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.side_effect = lambda a: Decimal("0") if "ETH" in a else Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy_456"})
        mock_exchange.invalidate_balance_cache = MagicMock(return_value=None)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")  # 1.5% -> max 60 BUY, после SELL разрешено 61–64
            bot.position_manager.add_position(Decimal("100"), Decimal("0.005"))
            # 61 BUY + 5 SELL (один заполним) — после заполнения 1 SELL останется 61 BUY и 4 SELL, лимит 61
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i) * Decimal("0.5"), Decimal("0.1"), status="open")
                for i in range(61)
            ] + [
                Order(f"sell_{j}", "SELL", Decimal("105") + Decimal(j), Decimal("0.1"), status="open")
                for j in range(5)
            ]
            initial_profit = bot.profit_bank
            initial_sells = bot.total_executed_sells
            order = bot.orders[-1]  # последний SELL заполняем
            await bot.handle_sell_filled(order, Decimal("101"))
            assert order.status == "filled"
            assert bot.profit_bank > initial_profit
            assert bot.total_executed_sells == initial_sells + 1
            # Новый BUY не создаётся — лимит 61 достигнут (61 BUY, 4 open SELL)
            mock_exchange.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_buy_orders_at_bottom_at_max_returns_zero(self, temp_dirs, mock_exchange):
        """При лимите BUY после SELL (61 BUY и 4 open SELL) create_buy_orders_at_bottom возвращает 0."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_max3")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "would_be_new"})
        mock_exchange.get_current_price = MagicMock(return_value=Decimal("100"))
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            # 61 BUY + 4 SELL -> max_allowed_buy = 61, лимит достигнут
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i) * Decimal("0.5"), Decimal("0.1"), status="open")
                for i in range(61)
            ] + [
                Order(f"sell_{j}", "SELL", Decimal("105") + Decimal(j), Decimal("0.1"), status="open")
                for j in range(4)
            ]
            created = await bot.create_buy_orders_at_bottom(Decimal("100"))
            assert created == 0
            mock_exchange.place_limit.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_buy_orders_at_bottom_allows_one_when_60_buy_4_sell(self, temp_dirs, mock_exchange):
        """Кнопка «Добавить Buy»: при 60 BUY и 4 open SELL разрешён 1 новый BUY (лимит 61)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_add1")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.balance.return_value = Decimal("1000")
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_buy_add1"})
        mock_exchange.get_current_price = MagicMock(return_value=Decimal("4.60"))
        mock_exchange.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="KSM-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.buy_order_value = Decimal("20")
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("4") - Decimal(i) * Decimal("0.01"), Decimal("5"), status="open")
                for i in range(60)
            ] + [
                Order(f"sell_{j}", "SELL", Decimal("4.5") + Decimal(j) * Decimal("0.02"), Decimal("5"), status="open")
                for j in range(4)
            ]
            created = await bot.create_buy_orders_at_bottom(Decimal("4.60"))
            assert created == 1
            mock_exchange.place_limit.assert_called_once()
            assert len([o for o in bot.orders if o.side == "BUY" and o.status == "open"]) == 61

    def test_get_min_open_orders_for_protection_15(self, temp_dirs, mock_exchange):
        """Порог для защиты «3 BUY → 5 внизу»: при шаге 1.5% = 62."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_thr")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            assert bot.get_min_open_orders_for_protection() == 62

    def test_get_min_open_orders_for_protection_075(self, temp_dirs, mock_exchange):
        """Порог для защиты «3 BUY → 5 внизу»: при шаге 0.75% = 127."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_thr2")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.0075")
            assert bot.get_min_open_orders_for_protection() == 127

    @pytest.mark.asyncio
    async def test_protection_add_five_when_three_left_above_threshold_calls_create_at_bottom(self, temp_dirs, mock_exchange):
        """При 3 открытых BUY и total_open > порога (62 для 1.5%) вызывается create_buy_orders_at_bottom и возвращается число созданных."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_prot")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_1"})
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            # 3 BUY + 60 «других» открытых = 63 > 62
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(60):
                bot.orders.append(Order(f"oth_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=5)
            get_price = AsyncMock(return_value=Decimal("100"))
            with (
                patch("grid_protection.create_buy_orders_at_bottom", create_at_bottom),
                patch.object(bot, "get_current_price", get_price),
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 5
            create_at_bottom.assert_called_once()
            call_price = create_at_bottom.call_args[0][1]  # (bot, current_price) -> index 1 is price
            assert call_price == Decimal("100")

    @pytest.mark.asyncio
    async def test_check_orders_triggers_rebalancing_when_0_sell_on_exchange_and_memory(self, temp_dirs, mock_exchange):
        """При 0 SELL на бирже и в памяти в TRADING и хотя бы одном исполнении check_orders вызывает check_rebalancing (57 BUY / 0 SELL)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_0sell")
        os.makedirs(trades_dir, exist_ok=True)
        # На бирже 57 BUY, 0 SELL
        mock_exchange.open_orders.return_value = [
            {"orderId": f"buy_{i}", "side": "BUY", "price": str(4.5 - i * 0.01), "origQty": "10", "executedQty": "0"}
            for i in range(57)
        ]
        mock_exchange.balance.return_value = Decimal("1000")
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.config.QUOTE", "USDT"),
            patch("trading_bot.config.BASE", "KSM"),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(8367409606, "key", "secret", symbol="KSM-USDT")
            bot.state = BotState.TRADING
            # 56 open BUY + 1 filled (чтобы total_filled > 0 и ребаланс сработал)
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("4.5") - Decimal(i) * Decimal("0.01"), Decimal("10"), status="open")
                for i in range(56)
            ]
            bot.orders.append(
                Order("buy_filled", "BUY", Decimal("4.0"), Decimal("10"), status="filled")
            )
            get_price = AsyncMock(return_value=Decimal("4.93"))
            check_rebal = AsyncMock()
            with patch.object(bot, "get_current_price", get_price), patch.object(bot, "check_rebalancing", check_rebal):
                await bot.check_orders()
            check_rebal.assert_called_once()
            assert check_rebal.call_args[0][0] == Decimal("4.93")

    @pytest.mark.asyncio
    async def test_check_orders_skips_rebalancing_when_0_sell_and_no_fills_buy_only_grid(self, temp_dirs, mock_exchange):
        """При 0 SELL и 0 исполнений (свежая сетка «только BUY») ребаланс не вызывается."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_0sell_0fill")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.open_orders.return_value = [
            {"orderId": f"buy_{i}", "side": "BUY", "price": str(5.0 - i * 0.01), "origQty": "10", "executedQty": "0"}
            for i in range(60)
        ]
        mock_exchange.balance.return_value = Decimal("500")
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.config.QUOTE", "USDT"),
            patch("trading_bot.config.BASE", "KSM"),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(8367409606, "key", "secret", symbol="KSM-USDT")
            bot.state = BotState.TRADING
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("5.0") - Decimal(i) * Decimal("0.01"), Decimal("10"), status="open")
                for i in range(60)
            ]
            check_rebal = AsyncMock()
            with patch.object(bot, "check_rebalancing", check_rebal):
                await bot.check_orders()
            check_rebal.assert_not_called()

    @pytest.mark.asyncio
    async def test_protection_add_five_when_three_left_below_threshold_does_nothing(self, temp_dirs, mock_exchange):
        """При 3 открытых BUY но total_open <= порога защита не вызывается (0 созданных)."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_prot2")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            # 3 BUY + 5 SELL = 8 открытых <= 62
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(5):
                bot.orders.append(Order(f"sell_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=5)
            with patch.object(bot, "create_buy_orders_at_bottom", create_at_bottom):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 0
            create_at_bottom.assert_not_called()

    @pytest.mark.asyncio
    async def test_protection_add_five_when_more_than_three_buy_does_nothing(self, temp_dirs, mock_exchange):
        """При 4+ открытых BUY защита не срабатывает."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_prot3")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(4)
            ]
            for i in range(60):
                bot.orders.append(Order(f"oth_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=5)
            with patch.object(bot, "create_buy_orders_at_bottom", create_at_bottom):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 0
            create_at_bottom.assert_not_called()

    @pytest.mark.asyncio
    async def test_protection_add_five_when_three_left_create_at_bottom_returns_zero(self, temp_dirs, mock_exchange):
        """При 3 BUY и total > порога защита вызывает create_buy_orders_at_bottom; если тот вернул 0 (мало баланса), защита возвращает 0."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_test_prot4")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(60):
                bot.orders.append(Order(f"s_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=0)
            get_price = AsyncMock(return_value=Decimal("100"))
            with (
                patch("grid_protection.create_buy_orders_at_bottom", create_at_bottom),
                patch.object(bot, "get_current_price", get_price),
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 0
            create_at_bottom.assert_called_once()


class TestProtectionBoundaries:
    """Граничные значения порогов защиты: 62 (1.5%), 127 (0.75%)."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.balance.return_value = Decimal("1000")
        ex.available_balance.return_value = Decimal("1000")
        ex.open_orders.return_value = []
        ex.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        return ex

    @pytest.mark.asyncio
    async def test_protection_above_threshold_63_runs(self, temp_dirs, mock_exchange):
        """При шаге 1.5% и total_open == 63 (> 62) защита срабатывает и вызывается create_buy_orders_at_bottom."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_bound_63")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "x"})
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            # 3 BUY + 60 других = 63 (> 62)
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(60):
                bot.orders.append(Order(f"o_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=2)
            get_price = AsyncMock(return_value=Decimal("100"))
            with (
                patch("grid_protection.create_buy_orders_at_bottom", create_at_bottom),
                patch.object(bot, "get_current_price", get_price),
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 2
            create_at_bottom.assert_called_once()

    @pytest.mark.asyncio
    async def test_protection_below_threshold_61_skips(self, temp_dirs, mock_exchange):
        """При шаге 1.5% и total_open == 61 (< 62) защита не срабатывает."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_bound_61")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.015")
            # 3 BUY + 58 других = 61
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(58):
                bot.orders.append(Order(f"o_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=5)
            get_price = AsyncMock(return_value=Decimal("100"))
            with (
                patch("grid_protection.create_buy_orders_at_bottom", create_at_bottom),
                patch.object(bot, "get_current_price", get_price),
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 0
            create_at_bottom.assert_not_called()

    @pytest.mark.asyncio
    async def test_protection_above_threshold_128_runs(self, temp_dirs, mock_exchange):
        """При шаге 0.75% и total_open == 128 (> 127) защита срабатывает."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_bound_128")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.available_balance.return_value = Decimal("1000")
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "y"})
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.0075")
            # 3 BUY + 125 других = 128 (> 127)
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(125):
                bot.orders.append(Order(f"o_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=3)
            get_price = AsyncMock(return_value=Decimal("100"))
            with (
                patch("grid_protection.create_buy_orders_at_bottom", create_at_bottom),
                patch.object(bot, "get_current_price", get_price),
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 3
            create_at_bottom.assert_called_once()

    @pytest.mark.asyncio
    async def test_protection_below_threshold_126_skips(self, temp_dirs, mock_exchange):
        """При шаге 0.75% и total_open == 126 (< 127) защита не срабатывает."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_bound_126")
        os.makedirs(trades_dir, exist_ok=True)
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.grid_step_pct = Decimal("0.0075")
            # 3 BUY + 123 других = 126
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(3)
            ]
            for i in range(123):
                bot.orders.append(Order(f"o_{i}", "SELL", Decimal("101") + Decimal(i), Decimal("0.1"), status="open"))
            create_at_bottom = AsyncMock(return_value=5)
            get_price = AsyncMock(return_value=Decimal("100"))
            with (
                patch("grid_protection.create_buy_orders_at_bottom", create_at_bottom),
                patch.object(bot, "get_current_price", get_price),
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 0
            create_at_bottom.assert_not_called()


class TestFullCycleIntegration:
    """Интеграционные тесты полного цикла: SELL fill → отмена 5 BUY → флаг → save/load → ребаланс."""

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as user_data_dir:
                yield state_dir, user_data_dir

    @pytest.fixture
    def mock_exchange(self):
        ex = MagicMock()
        ex.balance.return_value = Decimal("1000")
        ex.available_balance.return_value = Decimal("1000")
        ex.open_orders.return_value = []
        ex.symbol_info.return_value = {
            "stepSize": Decimal("0.0001"),
            "tickSize": Decimal("0.01"),
            "minQty": Decimal("0.0001"),
            "minNotional": Decimal("0"),
            "status": "TRADING",
        }
        ex._request = AsyncMock(return_value={})
        ex.invalidate_balance_cache = AsyncMock()
        return ex

    @pytest.mark.asyncio
    async def test_sell_fill_sets_flag_then_load_restores_it(self, temp_dirs, mock_exchange):
        """Полный цикл: 1 SELL исполнен → отмена 5 BUY, флаг сохранён → после load_state флаг восстанавливается."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_full_cycle")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.place_limit = MagicMock(return_value={"orderId": "new_sell"})
        mock_exchange.open_orders.return_value = [
            {"orderId": "buy_0", "side": "BUY", "price": "100", "origQty": "0.1", "executedQty": "0", "status": "NEW"},
            {"orderId": "sell_1", "side": "SELL", "price": "101", "origQty": "0.1", "executedQty": "0", "status": "NEW"},
        ]
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot.state = BotState.TRADING
            bot.grid_step_pct = Decimal("0.015")
            bot.buy_order_value = Decimal("50")
            bot.orders = [
                Order(f"buy_{i}", "BUY", Decimal("100") - Decimal(i), Decimal("0.1"), status="open")
                for i in range(6)
            ]
            sell_to_fill = Order("sell_0", "SELL", Decimal("101"), Decimal("0.1"), status="open")
            bot.orders.append(sell_to_fill)
            bot.orders.append(Order("sell_1", "SELL", Decimal("102"), Decimal("0.1"), status="open"))
            bot.position_manager.add_position(Decimal("100"), Decimal("0.1"))

            from handlers import handle_sell_filled

            await handle_sell_filled(bot, sell_to_fill, Decimal("101"))

            assert getattr(bot, "_cancelled_buy_for_rebalance_prep", False) is True

            bot2 = TradingBot(12345, "key", "secret", symbol="ETH-USDT")
            bot2.load_state()
            assert getattr(bot2, "_cancelled_buy_for_rebalance_prep", False) is True

    @pytest.mark.asyncio
    async def test_rebalancing_flow_market_buy_and_apply_called(self, temp_dirs, mock_exchange):
        """При 0 SELL check_rebalancing выполняет market buy и apply (моки). bot.ex подменён на мок напрямую."""
        state_dir, user_data_dir = temp_dirs
        trades_dir = os.path.join(tempfile.gettempdir(), "trades_rebal_flow")
        os.makedirs(trades_dir, exist_ok=True)
        mock_exchange.open_orders = AsyncMock(return_value=[])
        mock_exchange.available_balance = AsyncMock(return_value=Decimal("500"))
        mock_exchange.balance = AsyncMock(return_value=Decimal("500"))
        mock_exchange.invalidate_balance_cache = AsyncMock()
        mock_exchange.place_market = AsyncMock(return_value={"orderId": "mb_1"})
        mock_exchange.get_order = AsyncMock(
            return_value={"orderId": "mb_1", "price": "100", "executedQty": "10", "status": "FILLED"}
        )
        with (
            patch("trading_bot.config.STATE_DIR", state_dir),
            patch("trading_bot.config.USER_DATA_DIR", user_data_dir),
            patch("trading_bot.config.TRADES_DIR", trades_dir, create=True),
            patch("trading_bot.BingXSpot", return_value=mock_exchange),
            patch("persistence.config.STATE_DIR", state_dir),
            patch("persistence.config.USER_DATA_DIR", user_data_dir),
            patch("asyncio.sleep", AsyncMock()),
        ):
            bot = TradingBot(999, "k", "s", symbol="ETH-USDT")
            bot.ex = mock_exchange
            bot.state = BotState.TRADING
            bot.orders = []
            bot.rebuild_buy_grid_from_price = AsyncMock()
            bot.create_sell_grid_only = AsyncMock(return_value=3)
            with patch("rebalance.create_buy_orders_at_bottom", AsyncMock(return_value=2)):
                await bot.check_rebalancing(Decimal("100"))
            mock_exchange.place_market.assert_called_once()
            bot.rebuild_buy_grid_from_price.assert_called_once()
            bot.create_sell_grid_only.assert_called_once()
