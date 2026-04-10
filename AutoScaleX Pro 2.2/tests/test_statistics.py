"""
Unit tests for statistics — generate_report
"""

import builtins
import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statistics import Statistics


class TestStatistics:
    def test_generate_report_empty(self):
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "trades.csv")
            json_file = os.path.join(d, "statistics.json")
            s = Statistics(csv_file=csv_file, json_file=json_file, uid=None, persistence=None)
            report = s.generate_report(
                current_equity=Decimal("1000"),
                profit_bank=Decimal("0"),
                initial_equity=Decimal("1000"),
            )
            assert "Всего сделок: 0" in report
            assert "BUY: 0" in report
            assert "SELL: 0" in report
            assert "1000.00" in report

    def test_generate_report_with_trades(self):
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "trades.csv")
            s = Statistics(csv_file=csv_file, uid=None, persistence=None)
            s.trades = [
                {"type": "BUY", "profit": "0"},
                {"type": "SELL", "profit": "5"},
                {"type": "BUY", "profit": "0"},
                {"type": "SELL", "profit": "3"},
            ]
            report = s.generate_report(
                current_equity=Decimal("1108"),
                profit_bank=Decimal("8"),
                initial_equity=Decimal("1000"),
            )
            assert "Всего сделок: 4" in report
            assert "BUY: 2" in report
            assert "SELL: 2" in report
            assert "Прибыльных сделок: 2" in report
            assert "108.00" in report

    def test_generate_report_zero_initial_equity(self):
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "trades.csv")
            s = Statistics(csv_file=csv_file, uid=None, persistence=None)
            report = s.generate_report(
                current_equity=Decimal("500"),
                profit_bank=Decimal("0"),
                initial_equity=Decimal("0"),
            )
            assert "Общая прибыль: 0.00" in report
            assert "ROI: 0.00" in report

    def test_save_trade_adds_to_list(self):
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "t2.csv")
            json_file = os.path.join(d, "s2.json")
            s = Statistics(csv_file=csv_file, json_file=json_file, uid=None, persistence=None)
            s.save_trade({"type": "BUY", "price": "100", "qty": "0.5", "symbol": "ETH-USDT"})
            assert len(s.trades) == 1
            assert s.trades[0]["type"] == "BUY"

    def test_ensure_csv_header_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "new.csv")
            s = Statistics(csv_file=csv_file, uid=None, persistence=None)
            assert os.path.exists(csv_file)

    def test_load_trades_from_file_swallows_storage_error(self):
        mock_p = MagicMock()
        mock_p.load_user_trades.side_effect = OSError("no access")
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "t.csv")
            Statistics(csv_file=csv_file, json_file=os.path.join(d, "j.json"), uid="u1", persistence=mock_p)
        mock_p.load_user_trades.assert_called_once_with("u1")

    def test_load_from_json_invalid_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            json_file = os.path.join(d, "bad.json")
            with open(json_file, "w", encoding="utf-8") as f:
                f.write("{ not json")
            csv_file = os.path.join(d, "t.csv")
            s = Statistics(csv_file=csv_file, json_file=json_file, uid=None, persistence=None)
            assert s.trades == []

    def test_generate_report_invalid_types_returns_error_message(self):
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "t.csv")
            s = Statistics(csv_file=csv_file, uid=None, persistence=None)
            report = s.generate_report(Decimal("1000"), Decimal("0"), "not-a-decimal")
            assert report == "Ошибка генерации отчета"

    def test_clear_all_with_persistence(self):
        mock_p = MagicMock()
        mock_p.load_user_trades.return_value = {"trades": [{"type": "BUY"}], "settings": {"k": "v"}}
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "c.csv")
            json_file = os.path.join(d, "st.json")
            s = Statistics(csv_file=csv_file, json_file=json_file, uid="uid1", persistence=mock_p)
            s.clear_all()
            assert s.trades == []
            mock_p.save_user_trades.assert_called_once_with("uid1", [], {"k": "v"})

    def test_save_trade_csv_append_oserror_still_keeps_trade_and_json(self):
        _real_open = builtins.open

        def open_stub(path, mode="r", *args, **kwargs):
            p = str(path)
            if mode == "a" and p.endswith("tr.csv"):
                raise OSError("disk full")
            return _real_open(path, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "tr.csv")
            json_file = os.path.join(d, "st.json")
            s = Statistics(csv_file=csv_file, json_file=json_file, uid=None, persistence=None)
            with patch("builtins.open", open_stub):
                s.save_trade({"type": "BUY", "symbol": "X", "price": Decimal("1"), "qty": Decimal("1")})
            assert len(s.trades) == 1
            assert os.path.exists(json_file)

    def test_save_trade_persistence_error_still_keeps_trade_in_memory(self):
        mock_p = MagicMock()
        mock_p.load_user_trades.return_value = {"trades": [], "settings": {}}
        mock_p.add_trade.side_effect = ValueError("bad uid")
        with tempfile.TemporaryDirectory() as d:
            csv_file = os.path.join(d, "p.csv")
            json_file = os.path.join(d, "pj.json")
            s = Statistics(csv_file=csv_file, json_file=json_file, uid="u", persistence=mock_p)
            s.save_trade({"type": "SELL", "symbol": "X", "price": "10", "qty": "1"})
        assert len(s.trades) == 1
        mock_p.add_trade.assert_called_once()
