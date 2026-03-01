"""
Unit tests for buy_position — PositionManager, FIFO, restore_from_trades
"""

import os
import sys
from decimal import Decimal

# Ensure parent dir is in path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buy_position import BuyPosition, PositionManager


class TestBuyPosition:
    def test_repr(self):
        pos = BuyPosition(Decimal("100"), Decimal("1"))
        assert "100" in repr(pos)
        assert "1" in repr(pos)


class TestPositionManager:
    def test_add_position(self):
        pm = PositionManager()
        pm.add_position(Decimal("100"), Decimal("1"))
        assert pm.get_total_qty() == Decimal("1")
        assert pm.get_average_price() == Decimal("100")

    def test_add_multiple_positions(self):
        pm = PositionManager()
        pm.add_position(Decimal("100"), Decimal("2"))
        pm.add_position(Decimal("110"), Decimal("1"))
        assert pm.get_total_qty() == Decimal("3")
        assert pm.get_average_price().quantize(Decimal("0.01")) == Decimal("103.33")

    def test_calculate_profit_for_sell_empty(self):
        pm = PositionManager()
        profit = pm.calculate_profit_for_sell(Decimal("1"), Decimal("120"), Decimal("0.001"))
        assert profit == Decimal("0")

    def test_calculate_profit_for_sell_zero_qty(self):
        pm = PositionManager()
        pm.add_position(Decimal("100"), Decimal("1"))
        profit = pm.calculate_profit_for_sell(Decimal("0"), Decimal("120"), Decimal("0.001"))
        assert profit == Decimal("0")

    def test_calculate_profit_for_sell_fifo(self):
        pm = PositionManager()
        pm.add_position(Decimal("100"), Decimal("1"))
        pm.add_position(Decimal("110"), Decimal("1"))
        profit = pm.calculate_profit_for_sell(Decimal("1"), Decimal("120"), Decimal("0.001"))
        assert profit > 0
        assert pm.get_total_qty() == Decimal("1")

    def test_calculate_profit_for_sell_partial_position(self):
        pm = PositionManager()
        pm.add_position(Decimal("100"), Decimal("2"))
        profit = pm.calculate_profit_for_sell(Decimal("1"), Decimal("110"), Decimal("0"))
        assert profit == Decimal("10")
        assert pm.get_total_qty() == Decimal("1")

    def test_restore_from_trades_empty(self):
        pm = PositionManager()
        n = pm.restore_from_trades([], Decimal("0.001"))
        assert n == 0
        assert pm.get_total_qty() == Decimal("0")

    def test_restore_from_trades_buy_only(self):
        pm = PositionManager()
        trades = [
            {"type": "BUY", "price": "100", "qty": "1", "timestamp": "2025-01-01T00:00:00"},
            {"type": "BUY", "price": "110", "qty": "0.5", "timestamp": "2025-01-01T01:00:00"},
        ]
        n = pm.restore_from_trades(trades, Decimal("0.001"))
        assert n == 2
        assert pm.get_total_qty() == Decimal("1.5")

    def test_restore_from_trades_buy_and_sell(self):
        pm = PositionManager()
        trades = [
            {"type": "BUY", "price": "100", "qty": "1", "timestamp": "2025-01-01T00:00:00", "symbol": "ETH-USDT"},
            {"type": "SELL", "price": "120", "qty": "0.5", "timestamp": "2025-01-01T01:00:00", "symbol": "ETH-USDT"},
        ]
        n = pm.restore_from_trades(trades, Decimal("0.001"), symbol="ETH-USDT")
        assert n == 1
        assert pm.get_total_qty() == Decimal("0.5")

    def test_restore_from_trades_filter_symbol(self):
        pm = PositionManager()
        trades = [
            {"type": "BUY", "price": "100", "qty": "1", "timestamp": "2025-01-01T00:00:00", "symbol": "ETH-USDT"},
            {"type": "BUY", "price": "50", "qty": "1", "timestamp": "2025-01-01T01:00:00", "symbol": "BTC-USDT"},
        ]
        n = pm.restore_from_trades(trades, Decimal("0.001"), symbol="ETH-USDT")
        assert n == 1
        assert pm.get_total_qty() == Decimal("1")
