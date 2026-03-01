"""
Модуль для отслеживания позиций покупки (FIFO)
"""

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

log = logging.getLogger("buy_position")


class BuyPosition:
    """Позиция покупки для расчета прибыли по FIFO"""

    def __init__(self, price: Decimal, qty: Decimal, timestamp: float = None):
        self.price = price
        self.qty = qty
        self.timestamp = timestamp or time.time()

    def __repr__(self):
        return f"BuyPosition(price={self.price}, qty={self.qty}, ts={self.timestamp})"


class PositionManager:
    """Менеджер позиций для расчета прибыли по FIFO"""

    def __init__(self):
        self.positions: List[BuyPosition] = []

    def add_position(self, price: Decimal, qty: Decimal):
        """Добавить позицию покупки"""
        position = BuyPosition(price, qty)
        self.positions.append(position)
        self.positions.sort(key=lambda x: x.timestamp)

    def calculate_profit_for_sell(self, sell_qty: Decimal, sell_price: Decimal, fee_rate: Decimal) -> Decimal:
        """Рассчитать прибыль от продажи по методу FIFO"""
        if sell_qty <= 0:
            return Decimal("0")
        if not self.positions:
            return Decimal("0")

        remaining_qty = sell_qty
        total_cost = Decimal("0")

        positions_to_remove = []
        for position in self.positions:
            if remaining_qty <= 0:
                break

            if remaining_qty >= position.qty:
                total_cost += position.price * position.qty
                remaining_qty -= position.qty
                positions_to_remove.append(position)
            else:
                total_cost += position.price * remaining_qty
                position.qty -= remaining_qty
                remaining_qty = Decimal("0")

        for pos in positions_to_remove:
            self.positions.remove(pos)

        gross_revenue = sell_qty * sell_price
        net_revenue = gross_revenue * (Decimal("1") - fee_rate)
        profit = net_revenue - total_cost

        return profit

    def get_total_qty(self) -> Decimal:
        """Получить общее количество в позициях"""
        return sum(pos.qty for pos in self.positions)

    def get_average_price(self) -> Decimal:
        """Получить среднюю цену покупки (VWAP)"""
        if not self.positions:
            return Decimal("0")

        total_cost = sum(pos.price * pos.qty for pos in self.positions)
        total_qty = self.get_total_qty()

        if total_qty == 0:
            return Decimal("0")

        return total_cost / total_qty

    def restore_from_trades(self, trades: List[Dict], fee_rate: Decimal, symbol: Optional[str] = None) -> int:
        """Восстановить позиции FIFO из истории сделок (для restore после рестарта)."""
        self.positions.clear()
        if not trades:
            return 0

        filtered = [t for t in trades if t.get("type") in ("BUY", "SELL") and (symbol is None or t.get("symbol") == symbol)]
        if not filtered:
            return 0

        sorted_trades = sorted(filtered, key=lambda t: t.get("timestamp", ""))
        for t in sorted_trades:
            try:
                price = Decimal(str(t.get("price", "0")))
                qty = Decimal(str(t.get("qty", "0")))
                if price <= 0 or qty <= 0:
                    continue
                if t.get("type") == "BUY":
                    self.add_position(price, qty)
                elif t.get("type") == "SELL":
                    self.calculate_profit_for_sell(qty, price, fee_rate)
            except Exception as e:
                log.warning(f"restore_from_trades: skip trade {t.get('type')} {t.get('timestamp')}: {e}")

        log.info(f"PositionManager restored from {len(sorted_trades)} trades, {len(self.positions)} positions, total_qty={self.get_total_qty():.8f}")
        return len(self.positions)
