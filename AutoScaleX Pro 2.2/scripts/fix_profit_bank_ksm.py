#!/usr/bin/env python3
"""
Однократное исправление файла сделок: обнулить ошибочный profit по одной сделке SELL
и пересчитать profit_bank для всех последующих сделок и settings.

Использование:
  python scripts/fix_profit_bank_ksm.py path/to/35812365.json
  python scripts/fix_profit_bank_ksm.py path/to/file.json --symbol ETH-USDT
  python scripts/fix_profit_bank_ksm.py path/to/file.json --symbol DOT-USDT --timestamp "2026-03-10T03:25:25"
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
        print("Usage: python fix_profit_bank_ksm.py <path_to_file.json> [--symbol SYMBOL] [--timestamp ISO_TIMESTAMP]")
        sys.exit(1)
    path = sys.argv[1]
    target_timestamp = None
    symbol = "KSM-USDT"
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--timestamp" and i + 1 < len(args):
            target_timestamp = args[i + 1]
            i += 2
        elif args[i] == "--symbol" and i + 1 < len(args):
            symbol = args[i + 1]
            i += 2
        else:
            i += 1
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not fix_profit_bank_in_data(data, symbol=symbol, target_timestamp=target_timestamp):
        print("Target SELL trade not found. No changes.")
        sys.exit(0)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    final = data["settings"].get("profit_bank", "0")
    print(f"Updated profit_bank in all trades and settings. Final profit_bank: {final}")
    print("File saved. Copy it back to server user_data/35812365.json and restart bot if needed.")


if __name__ == "__main__":
    main()
