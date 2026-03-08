#!/usr/bin/env python3
"""
Однократное исправление файла сделок: обнулить ошибочный profit по одной сделке SELL
и пересчитать profit_bank для всех последующих сделок и settings.

Использование:
  python scripts/fix_profit_bank_ksm.py path/to/35812365.json

Или с указанием какой именно profit обнулить (по timestamp или по индексу SELL):
  python scripts/fix_profit_bank_ksm.py path/to/35812365.json --timestamp "2026-03-08T02:13:03"
"""
import json
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional


def fix_profit_bank_in_data(
    data: Dict[str, Any],
    symbol: str = "KSM-USDT",
    target_timestamp: Optional[str] = None,
    erroneous_profit_min: float = 15.0,
    erroneous_profit_center: float = 20.22,
    erroneous_profit_tolerance: float = 1.0,
) -> bool:
    """
    Обнуляет ошибочный profit у одной SELL-сделки и пересчитывает profit_bank.
    Возвращает True, если правка применена, False если целевая сделка не найдена.
    """
    trades: List[Dict[str, Any]] = list(data.get("trades", []))
    target_idx: Optional[int] = None
    for i, t in enumerate(trades):
        if t.get("type") != "SELL" or t.get("symbol") != symbol:
            continue
        if target_timestamp:
            if (t.get("timestamp") or "").startswith(target_timestamp[:19]):
                target_idx = i
                break
        else:
            p = float(t.get("profit", 0))
            if p > erroneous_profit_min and abs(p - erroneous_profit_center) < erroneous_profit_tolerance:
                target_idx = i
                break

    if target_idx is None:
        return False

    trades[target_idx]["profit"] = "0"

    running = Decimal("0")
    for t in trades:
        if t.get("type") == "SELL":
            running += Decimal(str(t.get("profit", "0")))
        t["profit_bank"] = str(running)

    if "settings" not in data:
        data["settings"] = {}
    data["settings"]["profit_bank"] = str(running)
    data["trades"] = trades
    return True


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python fix_profit_bank_ksm.py <path_to_35812365.json> [--timestamp ISO_TIMESTAMP]")
        sys.exit(1)
    path = sys.argv[1]
    target_timestamp = None
    if len(sys.argv) >= 4 and sys.argv[2] == "--timestamp":
        target_timestamp = sys.argv[3]
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not fix_profit_bank_in_data(data, target_timestamp=target_timestamp):
        print("Target SELL trade (erroneous profit ~20.22) not found. No changes.")
        sys.exit(0)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    final = data["settings"].get("profit_bank", "0")
    print(f"Updated profit_bank in all trades and settings. Final profit_bank: {final}")
    print("File saved. Copy it back to server user_data/35812365.json and restart bot if needed.")


if __name__ == "__main__":
    main()
