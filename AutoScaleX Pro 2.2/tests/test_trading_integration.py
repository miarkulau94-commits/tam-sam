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

from trading_bot import Order, TradingBot


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
            with patch.object(bot, "create_buy_orders_at_bottom", create_at_bottom), patch.object(
                bot, "get_current_price", get_price
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 5
            create_at_bottom.assert_called_once()
            call_price = create_at_bottom.call_args[0][0]
            assert call_price == Decimal("100")

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
            with patch.object(bot, "create_buy_orders_at_bottom", create_at_bottom), patch.object(
                bot, "get_current_price", get_price
            ):
                n = await bot.check_protection_add_five_buy_when_three_left()
            assert n == 0
            create_at_bottom.assert_called_once()
