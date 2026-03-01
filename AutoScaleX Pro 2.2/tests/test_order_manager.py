"""
Unit tests for order_manager — Order, deduplicate_orders
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import Order, deduplicate_orders


class TestOrder:
    """Тесты Order"""

    def test_init_buy(self):
        o = Order("oid1", "BUY", Decimal("100"), Decimal("0.5"))
        assert o.order_id == "oid1"
        assert o.side == "BUY"
        assert o.price == Decimal("100")
        assert o.qty == Decimal("0.5")
        assert o.status == "open"

    def test_init_sell(self):
        o = Order("oid2", "SELL", Decimal("105"), Decimal("0.3"))
        assert o.side == "SELL"
        assert o.amount_usdt == Decimal("105") * Decimal("0.3")

    def test_to_dict(self):
        o = Order("oid3", "BUY", Decimal("50"), Decimal("1"))
        d = o.to_dict()
        assert d["order_id"] == "oid3"
        assert d["side"] == "BUY"
        assert d["price"] == "50"
        assert d["qty"] == "1"
        assert "executed_qty" in d

    def test_from_dict_minimal(self):
        d = {"order_id": "f1", "side": "BUY", "price": "100", "qty": "0.5"}
        o = Order.from_dict(d)
        assert o.order_id == "f1"
        assert o.price == Decimal("100")
        assert o.qty == Decimal("0.5")

    def test_from_dict_missing_price_raises(self):
        import pytest

        with pytest.raises(KeyError, match="price"):
            Order.from_dict({"order_id": "x", "qty": "1"})

    def test_from_dict_missing_qty_raises(self):
        import pytest

        with pytest.raises(KeyError, match="qty"):
            Order.from_dict({"order_id": "x", "price": "100"})

    def test_from_dict_roundtrip(self):
        o = Order("r1", "SELL", Decimal("200"), Decimal("0.25"), status="filled")
        d = o.to_dict()
        o2 = Order.from_dict(d)
        assert o2.order_id == o.order_id
        assert o2.side == o.side
        assert o2.price == o.price
        assert o2.status == o.status


class TestDeduplicateOrders:
    """Тесты deduplicate_orders"""

    def test_no_duplicates_unchanged(self):
        orders = [
            Order("a", "BUY", Decimal("100"), Decimal("0.5")),
            Order("b", "SELL", Decimal("101"), Decimal("0.5")),
        ]
        deduplicate_orders(orders, 1, "ETH-USDT")
        assert len(orders) == 2

    def test_removes_duplicate(self):
        orders = [
            Order("dup", "BUY", Decimal("100"), Decimal("0.5")),
            Order("dup", "BUY", Decimal("100"), Decimal("0.5")),
            Order("c", "SELL", Decimal("101"), Decimal("0.5")),
        ]
        deduplicate_orders(orders, 1, "ETH-USDT")
        assert len(orders) == 2
        ids = [o.order_id for o in orders]
        assert "dup" in ids
        assert ids.count("dup") == 1
