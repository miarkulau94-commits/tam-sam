"""Unit tests for persistence - StatePersistence"""
import os
import sys
import tempfile
from unittest.mock import mock_open, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SECRET = "a" * 40  # длина для ENCRYPTION_SECRET в production


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

    @patch("persistence.config.ENCRYPTION_SECRET", _SECRET)
    def test_encrypt_decrypt_api_keys_roundtrip(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            ek, es = p.encrypt_api_keys("uid42", "my_api_key", "my_secret_key")
            assert ek != "my_api_key" and es != "my_secret_key"
            ak, sk = p.decrypt_api_keys("uid42", ek, es)
            assert ak == "my_api_key"
            assert sk == "my_secret_key"

    @patch("persistence.config.ENCRYPTION_SECRET", _SECRET)
    def test_save_load_api_keys_roundtrip(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            p.save_api_keys("uid99", "k1", "s1")
            loaded = p.load_api_keys("uid99")
            assert loaded is not None
            assert loaded == ("k1", "s1")
            assert p.has_api_keys("uid99") is True

    @patch("persistence.config.ENCRYPTION_SECRET", _SECRET)
    def test_load_api_keys_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            assert p.load_api_keys("unknown_uid") is None
            assert p.has_api_keys("unknown_uid") is False

    @patch("persistence.config.ENCRYPTION_SECRET", _SECRET)
    def test_decrypt_fails_for_wrong_uid(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence
            from cryptography.fernet import InvalidToken

            p = StatePersistence(state_dir=base, user_data_dir=base)
            ek, es = p.encrypt_api_keys("uidA", "k", "s")
            with pytest.raises(InvalidToken):
                p.decrypt_api_keys("uidB", ek, es)

    def test_encrypt_raises_when_encryption_secret_empty(self):
        with tempfile.TemporaryDirectory() as base:
            with patch("persistence.config.ENCRYPTION_SECRET", ""):
                from persistence import StatePersistence

                p = StatePersistence(state_dir=base, user_data_dir=base)
                with pytest.raises(ValueError, match="ENCRYPTION_SECRET"):
                    p.encrypt_api_keys("u1", "a", "b")

    def test_load_state_invalid_json_returns_none(self):
        """Битый JSON в user_{id}.json — load_state возвращает None, бот не падает."""
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            path = p._get_user_file(777)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            assert p.load_state(777) is None

    def test_load_state_truncated_file_returns_none(self):
        """Обрезанный JSON — JSONDecodeError, None."""
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            path = p._get_user_file(888)
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"uid": "x"')
            assert p.load_state(888) is None

    def test_load_orders_returns_empty_when_state_corrupt(self):
        """load_orders опирается на load_state; при битом файле — []."""
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            path = p._get_user_file(999)
            with open(path, "w", encoding="utf-8") as f:
                f.write("undefined")
            assert p.load_orders(999) == []

    @patch("persistence.config.ENCRYPTION_SECRET", _SECRET)
    def test_load_user_trades_invalid_json_returns_empty_shell(self):
        """Файл UID с невалидным JSON — дефолтная оболочка без падения."""
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            uid = "12345"
            path = p._get_uid_file(uid)
            with open(path, "w", encoding="utf-8") as f:
                f.write("[truncated")
            data = p.load_user_trades(uid)
            assert data["uid"] == uid
            assert data["trades"] == []
            assert data.get("total_trades", 0) == 0

    def test_list_user_ids_oserror_returns_empty(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            with patch("persistence.os.listdir", side_effect=OSError("denied")):
                assert p.list_user_ids_with_state() == []

    def test_save_state_open_oserror_swallowed(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            with patch("builtins.open", mock_open()) as m:
                m.side_effect = OSError("disk full")
                p.save_state(1, {"x": 1})

    def test_delete_state_oserror_swallowed(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            p.save_state(2, {"a": 1})
            with patch("persistence.os.remove", side_effect=OSError("busy")):
                p.delete_state(2)

    def test_has_api_keys_false_on_load_error(self):
        with tempfile.TemporaryDirectory() as base:
            from persistence import StatePersistence

            p = StatePersistence(state_dir=base, user_data_dir=base)
            with patch.object(p, "load_user_trades", side_effect=ValueError("bad")):
                assert p.has_api_keys("1") is False
