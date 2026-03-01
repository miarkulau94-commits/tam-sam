"""
Unit tests for statistics — generate_report
"""

import os
import sys
import tempfile
from decimal import Decimal

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
