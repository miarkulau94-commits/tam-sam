"""
Структурированное логирование: контекст user_id, symbol, order_id в каждом сообщении.
Используются contextvars — контекст привязан к текущей задаче (корректно при async).
"""
import logging
from contextvars import ContextVar
from typing import Any, Optional

# Контекст для текущей задачи (устанавливается при входе в логику бота/ордера)
_log_user_id: ContextVar[Optional[int]] = ContextVar("log_user_id", default=None)
_log_symbol: ContextVar[Optional[str]] = ContextVar("log_symbol", default=None)
_log_order_id: ContextVar[Optional[str]] = ContextVar("log_order_id", default=None)


def set_log_context(
    user_id: Optional[int] = None,
    symbol: Optional[str] = None,
    order_id: Optional[str] = None,
) -> None:
    """Установить контекст для последующих логов в этой задаче. None — не менять."""
    if user_id is not None:
        _log_user_id.set(user_id)
    if symbol is not None:
        _log_symbol.set(symbol)
    if order_id is not None:
        _log_order_id.set(order_id)


def clear_log_context() -> None:
    """Сбросить контекст (опционально, при выходе из скоупа бота)."""
    try:
        _log_user_id.set(None)
    except LookupError:
        pass
    try:
        _log_symbol.set(None)
    except LookupError:
        pass
    try:
        _log_order_id.set(None)
    except LookupError:
        pass


def get_log_context() -> dict:
    """Текущий контекст для логов (для использования в extra= или в формате)."""
    return {
        "user_id": _log_user_id.get() if _log_user_id.get() is not None else "-",
        "symbol": _log_symbol.get() or "-",
        "order_id": _log_order_id.get() or "-",
    }


class StructuredContextFilter(logging.Filter):
    """Добавляет в каждый LogRecord поля user_id, symbol, order_id из contextvars."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_log_context()
        record.user_id = ctx["user_id"]
        record.symbol = ctx["symbol"]
        record.order_id = ctx["order_id"]
        return True
