"""Unit tests for persistence - StatePersistence"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestStatePersistence:
    def test_init_creates_dirs(self):
        with tempfile.TemporaryDirectory() as base:
            state_dir = os.path.join(base, "state")
            user_dir = os.path.join(base, "user_data")
            from persistence import StatePersistence
            p = StatePersistence(state_dir=state_dir, user_data_dir=user_dir)
            assert os.path.isdir(state_dir)
            assert os.path.isdir(user_dir)

    def test_save_load_state(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            p = StatePersistence(state_dir=base, user_data_dir=base)
            p.save_state(1, {"uid": "test123"})
            loaded = p.load_state(1)
            assert loaded and loaded["uid"] == "test123"

    def test_load_state_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            p = StatePersistence(state_dir=base, user_data_dir=base)
            assert p.load_state(99999) is None

    def test_save_load_orders(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            p = StatePersistence(state_dir=base, user_data_dir=base)
            orders = [{"order_id": "o1", "side": "BUY", "price": "100", "qty": "0.5"}]
            p.save_orders(2, orders)
            assert p.load_orders(2) == orders

    def test_delete_state(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            p = StatePersistence(state_dir=base, user_data_dir=base)
            p.save_state(4, {"uid": "x"})
            p.delete_state(4)
            assert p.load_state(4) is None

    def test_get_uid_file_raises_for_no_digits(self):
        import pytest
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            p = StatePersistence(state_dir=base, user_data_dir=base)
            with pytest.raises(ValueError):
                p._get_uid_file("abc")

    def test_save_load_user_trades(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            p = StatePersistence(state_dir=base, user_data_dir=base)
            trades = [{"type": "BUY", "price": "100", "qty": "1"}]
            p.save_user_trades("100", trades)
            data = p.load_user_trades("100")
            assert data["trades"] == trades
