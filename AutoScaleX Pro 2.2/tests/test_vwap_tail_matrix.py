"""
Матрица сценариев: VWAP (авто/ручной), хвост, классический ребаланс (ТЗ п.9–10, п.12).

Документация «на бумаге» — как сочетаются пути (реализация разнесена по модулям).

+---------------------------+--------------------------+--------------------------+------------------------+
| Сценарий                  | Где вызывается           | Порог open_SELL (п.8/9)  | Примечание             |
+===========================+==========================+==========================+========================+
| Ручной SELL от VWAP (TG)  | telegram → create_     | не применяется (п.9.2)   | Лог [VWAP_GRID]        |
|                           | critical_sell_grid(      |                          | source=manual_telegram |
|                           | vwap_source=             |                          |                        |
|                           | manual_telegram)         |                          |                        |
+---------------------------+--------------------------+--------------------------+------------------------+
| Авто SELL от VWAP         | rebalance.               | при open_SELL ≥ порога   | source=                |
| (все BUY съедены)         | check_rebalancing_       | авто не вызывается       | auto_after_all_buy     |
|                           | after_all_buy_filled     | (п.9.1)                  |                        |
+---------------------------+--------------------------+--------------------------+------------------------+
| Классический ребаланс     | rebalance.               | пороги п.9 не касаются   | market buy + rebuild;  |
| (0 открытых SELL)         | check_rebalancing        |                          | не create_critical_*   |
+---------------------------+--------------------------+--------------------------+------------------------+
| Хвост ATR                 | trading_bot.             | активация не режется по  | kline; анти-дребезг    |
|                           | try_activate_tail_grid   | числу SELL; отмена п.8   | п.4.6                  |
+---------------------------+--------------------------+--------------------------+------------------------+
| Критический уровень       | check_critical_level →   | —                        | source=critical_level  |
| (депозит)                 | create_critical_sell_grid|                          |                        |
+---------------------------+--------------------------+--------------------------+------------------------+
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rebalance


def test_rebalance_after_all_buy_passes_auto_vwap_source_in_source():
    """Регрессия: авто-VWAP остаётся с меткой auto_after_all_buy (п.12 vs ручной)."""
    src = inspect.getsource(rebalance.check_rebalancing_after_all_buy_filled)
    assert 'vwap_source="auto_after_all_buy"' in src


def test_telegram_manual_vwap_source_in_source():
    """Ручной VWAP из Telegram — manual_telegram (п.12)."""
    telegram_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "telegram_bot.py")
    with open(telegram_path, encoding="utf-8") as f:
        tg = f.read()
    assert 'vwap_source="manual_telegram"' in tg
