"""
Модуль статистики и аналитики
"""

import csv
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

log = logging.getLogger("statistics")


class Statistics:
    """Класс для сбора и сохранения статистики"""

    def __init__(self, csv_file: str = "trades.csv", json_file: str = "statistics.json", uid: Optional[str] = None, persistence=None):
        self.csv_file = csv_file
        self.json_file = json_file
        self.uid = uid
        self.persistence = persistence
        self.trades: List[Dict] = []
        self._ensure_csv_header()

        if self.uid and self.persistence:
            self._load_trades_from_file()
        if not self.trades and os.path.exists(self.json_file):
            self._load_from_json()

    def _ensure_csv_header(self):
        """Создать CSV файл с заголовками если не существует"""
        if not os.path.exists(self.csv_file):
            with open(self.csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "type", "symbol", "price", "qty", "amount_usdt", "profit", "profit_bank", "total_equity"])

    def _load_trades_from_file(self):
        """Загрузить сделки из файла пользователя"""
        try:
            if self.uid and self.persistence:
                user_data = self.persistence.load_user_trades(self.uid)
                self.trades = user_data.get("trades", [])
                log.info(f"Loaded {len(self.trades)} trades from file for UID {self.uid}")
        except (OSError, json.JSONDecodeError, KeyError) as e:
            log.error(f"Error loading trades from file: {e}")

    def _load_from_json(self):
        """Загрузить статистику из JSON-файла (fallback)"""
        try:
            with open(self.json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.trades = data.get("trades", [])
            if self.trades:
                log.info(f"Loaded {len(self.trades)} trades from JSON {self.json_file}")
        except (OSError, json.JSONDecodeError, KeyError) as e:
            log.warning(f"Could not load statistics from JSON: {e}")

    def _save_to_json(self):
        """Сохранить статистику в JSON"""
        try:
            data = {"trades": self.trades, "total_trades": len(self.trades), "last_updated": datetime.now().isoformat()}
            with open(self.json_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        except OSError as e:
            log.warning(f"Could not save statistics to JSON: {e}")

    def clear_all(self):
        """Очистить все сделки (при нажатии «Стоп»): файл пользователя, json, csv, память."""
        self.trades = []
        try:
            if self.uid and self.persistence:
                user_data = self.persistence.load_user_trades(self.uid)
                self.persistence.save_user_trades(self.uid, [], user_data.get("settings", {}))
                log.info(f"Cleared trades for UID {self.uid}")
            self._save_to_json()
            if os.path.exists(self.csv_file):
                with open(self.csv_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["timestamp", "type", "symbol", "price", "qty", "amount_usdt", "profit", "profit_bank", "total_equity"])
        except (OSError, KeyError, TypeError) as e:
            log.warning(f"Error clearing statistics: {e}")

    def save_trade(self, trade: Dict):
        """Сохранить сделку в CSV, память и файл пользователя"""
        try:
            if "timestamp" not in trade:
                trade["timestamp"] = datetime.now().isoformat()

            trade_copy = trade.copy()
            self.trades.append(trade_copy)

            # Сохраняем в CSV (для совместимости)
            try:
                with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        [
                            trade.get("timestamp", ""),
                            trade.get("type", ""),
                            trade.get("symbol", ""),
                            str(trade.get("price", "0")),
                            str(trade.get("qty", "0")),
                            str(trade.get("amount_usdt", "0")),
                            str(trade.get("profit", "0")),
                            str(trade.get("profit_bank", "0")),
                            str(trade.get("total_equity", "0")),
                        ]
                    )
            except OSError as e:
                log.warning(f"Error saving trade to CSV: {e}")

            if self.uid and self.persistence:
                try:
                    self.persistence.add_trade(self.uid, trade_copy)
                except (OSError, ValueError) as e:
                    log.error(f"Error saving trade to user file for UID {self.uid}: {e}")
            self._save_to_json()
        except (KeyError, TypeError) as e:
            log.error(f"Error saving trade: {e}")

    def generate_report(self, current_equity: Decimal, profit_bank: Decimal, initial_equity: Decimal) -> str:
        """Сгенерировать текстовый отчет"""
        try:
            total_trades = len(self.trades)
            buy_trades = len([t for t in self.trades if t.get("type") == "BUY"])
            sell_trades = len([t for t in self.trades if t.get("type") == "SELL"])

            total_profit = current_equity - initial_equity if initial_equity > 0 else Decimal("0")
            roi = (total_profit / initial_equity * Decimal("100")) if initial_equity > 0 else Decimal("0")

            profitable_trades = [t for t in self.trades if Decimal(str(t.get("profit", "0"))) > 0]
            avg_profit = (
                sum(Decimal(str(t.get("profit", "0"))) for t in profitable_trades) / len(profitable_trades) if profitable_trades else Decimal("0")
            )

            report = f"""
📊 Статистика торговли
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Всего сделок: {total_trades}
  • BUY: {buy_trades}
  • SELL: {sell_trades}

💰 Финансы:
  • Начальный депозит: {initial_equity:.2f} USDT
  • Текущий equity: {current_equity:.2f} USDT
  • Общая прибыль: {total_profit:.2f} USDT
  • ROI: {roi:.2f}%
  • Profit bank: {profit_bank:.2f} USDT

📈 Производительность:
  • Прибыльных сделок: {len(profitable_trades)}
  • Средняя прибыль: {avg_profit:.2f} USDT
            """.strip()

            return report
        except (KeyError, TypeError, ZeroDivisionError) as e:
            log.error("Error generating report: %s", e)
            return "Ошибка генерации отчета"
