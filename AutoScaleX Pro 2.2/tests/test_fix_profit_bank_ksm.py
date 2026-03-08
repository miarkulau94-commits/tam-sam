"""
Unit tests for scripts/fix_profit_bank_ksm — обнуление ошибочного profit и пересчёт profit_bank.
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.fix_profit_bank_ksm import fix_profit_bank_in_data


def test_fix_profit_bank_no_target_trade():
    """Если нет SELL с ошибочным profit ~20.22, данные не меняются."""
    data = {
        "trades": [
            {"type": "BUY", "symbol": "KSM-USDT", "profit": "0", "profit_bank": "0"},
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "0.5", "profit_bank": "0.5"},
        ],
        "settings": {"profit_bank": "0.5"},
    }
    original_trades = [dict(t) for t in data["trades"]]
    applied = fix_profit_bank_in_data(data)
    assert applied is False
    assert data["trades"] == original_trades
    assert data["settings"]["profit_bank"] == "0.5"


def test_fix_profit_bank_zeros_erroneous_and_recalculates():
    """Обнуляет сделку с profit ~20.22 и пересчитывает profit_bank по порядку."""
    data = {
        "trades": [
            {"type": "BUY", "symbol": "KSM-USDT", "profit": "0", "profit_bank": "0"},
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "0.247", "profit_bank": "0.247"},
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "20.22234888", "profit_bank": "20.47"},
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "-0.5", "profit_bank": "19.97"},
        ],
        "settings": {"profit_bank": "19.97"},
    }
    applied = fix_profit_bank_in_data(data)
    assert applied is True
    # Ошибочная сделка (вторая SELL с 20.22) обнулена
    sells = [t for t in data["trades"] if t.get("type") == "SELL"]
    assert sells[0]["profit"] == "0.247"
    assert sells[1]["profit"] == "0"
    assert sells[2]["profit"] == "-0.5"
    # profit_bank пересчитан: 0.247 + 0 + (-0.5) = -0.253
    assert data["trades"][0]["profit_bank"] == "0"
    assert data["trades"][1]["profit_bank"] == "0.247"
    assert data["trades"][2]["profit_bank"] == "0.247"
    assert data["trades"][3]["profit_bank"] == "-0.253"
    assert data["settings"]["profit_bank"] == "-0.253"


def test_fix_profit_bank_by_timestamp():
    """Поиск целевой сделки по timestamp."""
    data = {
        "trades": [
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "100", "timestamp": "2026-03-08T02:13:03.123Z", "profit_bank": "100"},
        ],
        "settings": {"profit_bank": "100"},
    }
    applied = fix_profit_bank_in_data(data, target_timestamp="2026-03-08T02:13:03")
    assert applied is True
    assert data["trades"][0]["profit"] == "0"
    assert data["trades"][0]["profit_bank"] == "0"
    assert data["settings"]["profit_bank"] == "0"


def test_fix_profit_bank_symbol_filter():
    """Исправляется только сделка по заданному symbol (KSM-USDT по умолчанию)."""
    data = {
        "trades": [
            {"type": "SELL", "symbol": "ETH-USDT", "profit": "20.22", "profit_bank": "20.22"},
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "20.22", "profit_bank": "40.44"},
        ],
        "settings": {"profit_bank": "40.44"},
    }
    applied = fix_profit_bank_in_data(data)
    assert applied is True
    # Должна обнулиться первая найденная SELL KSM-USDT с profit ~20.22 — вторая
    assert data["trades"][0]["symbol"] == "ETH-USDT"
    assert data["trades"][0]["profit"] == "20.22"
    assert data["trades"][1]["symbol"] == "KSM-USDT"
    assert data["trades"][1]["profit"] == "0"
    # profit_bank: только SELL считаются — 20.22 + 0 = 20.22
    assert data["trades"][1]["profit_bank"] == "20.22"
    assert data["settings"]["profit_bank"] == "20.22"


def test_fix_profit_bank_only_sell_contributes_to_bank():
    """К profit_bank добавляется только profit по сделкам типа SELL."""
    data = {
        "trades": [
            {"type": "BUY", "symbol": "KSM-USDT", "profit": "0", "profit_bank": "0"},
            {"type": "SELL", "symbol": "KSM-USDT", "profit": "20.22", "profit_bank": "20.22"},
        ],
        "settings": {"profit_bank": "20.22"},
    }
    applied = fix_profit_bank_in_data(data)
    assert applied is True
    assert data["trades"][1]["profit"] == "0"
    # После пересчёта: только один SELL с profit 0
    assert data["settings"]["profit_bank"] == "0"
