"""
Модуль управления ордерами: Order, дедупликация.
"""
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger("order_manager")


class Order:
    """Представление ордера."""

    def __init__(
        self,
        order_id: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        status: str = "open",
        amount_usdt: Optional[Decimal] = None,
        *,
        is_tail: bool = False,
        base_ladder_index: Optional[int] = None,
    ) -> None:
        self.order_id = order_id
        self.side = side.upper()
        self.price = price
        self.qty = qty
        self.status = status
        self.executed_qty = Decimal("0")
        self.amount_usdt = amount_usdt or (
            price * qty if self.side == "SELL" else config.BUY_ORDER_VALUE
        )
        self.created_at = time.time()
        self.is_tail = bool(is_tail)
        self.base_ladder_index = base_ladder_index

    def to_dict(self) -> Dict[str, Any]:
        """Преобразовать в словарь для сохранения."""
        d: Dict[str, Any] = {
            "order_id": self.order_id,
            "side": self.side,
            "price": str(self.price),
            "qty": str(self.qty),
            "status": self.status,
            "executed_qty": str(self.executed_qty),
            "amount_usdt": str(self.amount_usdt),
        }
        if self.is_tail:
            d["is_tail"] = True
        if self.base_ladder_index is not None:
            d["base_ladder_index"] = self.base_ladder_index
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "Order":
        """Создать из словаря."""
        if "order_id" not in data:
            raise KeyError("Order.from_dict: обязательное поле 'order_id'")
        if "price" not in data or "qty" not in data:
            raise KeyError("Order.from_dict: обязательны поля 'price' и 'qty'")
        price = Decimal(str(data["price"]))
        qty = Decimal(str(data["qty"]))
        side = data.get("side", "BUY").upper()
        amount_usdt_raw = data.get("amount_usdt", "0")
        amount_usdt = Decimal(amount_usdt_raw) if amount_usdt_raw else Decimal("0")
        if side == "SELL" and amount_usdt == 0 and price > 0 and qty > 0:
            amount_usdt = price * qty
        bl_raw = data.get("base_ladder_index")
        base_ladder_index = int(bl_raw) if bl_raw is not None else None
        order = cls(
            order_id=data["order_id"],
            side=side,
            price=price,
            qty=qty,
            status=data.get("status", "open"),
            amount_usdt=amount_usdt,
            is_tail=bool(data.get("is_tail", False)),
            base_ladder_index=base_ladder_index,
        )
        order.executed_qty = Decimal(data.get("executed_qty", "0"))
        order.created_at = data.get("created_at", 0)
        return order


def deduplicate_orders(orders: List[Order], user_id: int, symbol: str) -> None:
    """Удалить дубликаты ордеров по order_id (оставляем первое вхождение)."""
    seen = set()
    unique = []
    for o in orders:
        if o.order_id in seen:
            log.info(f"[DEDUP] user={user_id} symbol={symbol} Removed duplicate order {o.order_id} ({o.side})")
            continue
        seen.add(o.order_id)
        unique.append(o)
    if len(unique) < len(orders):
        orders[:] = unique
