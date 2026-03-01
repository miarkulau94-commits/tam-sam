"""
Классификация ошибок и фильтрация уведомлений.

- Критичные → в Telegram (с троттлингом) и log.error
- Предупреждения → только log.warning
- Не-критичные (ожидаемые) → log.debug, не в Telegram
"""
import re
from typing import Optional

# Фразы, при которых НЕ слать в Telegram (ожидаемые/временные)
NON_CRITICAL_PATTERNS = [
    r"order\s+not\s+exist",
    r"order\s+does\s+not\s+exist",
    r"balance\s+not\s+enough",
    r"balance\s+not\s+sufficient",
    r"insufficient\s+balance",
    r"entrust\s+volume\s+to\s+low",
    r"minvolume",
    r"timestamp\s+mismatch",
    r"null\s+timestamp",
    r"min\s*notional",
    r"price\s+precision",
    r"lot\s+size",
    r"qty.*less\s+than\s+min",
    r"validation\s+failed",
    r"doesn't\s+meet\s+requirements",
    r"api\s+returned\s+(no\s+)?orderid",
    r"api\s+returned\s+none",
    r"market\s+buy\s+failed",
    r"readtimeout",
    r"connectiontimeout",
    r"timed\s+out",
    r"timeout",
    r"connection\s+(refused|reset|error)",
    r"temporarily\s+unavailable",
    r"rate\s+limit",
    r"too\s+many\s+requests",
    r"symbol\s+not\s+found",  # может быть из-за неверного ввода
]

_compiled_non_critical = [re.compile(p, re.I) for p in NON_CRITICAL_PATTERNS]


def is_telegram_critical(msg: str, category: str = "") -> bool:
    """
    Нужно ли слать это в Telegram?
    Только действительно критичные ошибки.
    """
    if not msg or not isinstance(msg, str):
        return False
    s = msg.strip().lower()
    # Явно критичные
    if any(k in s for k in ("критическ", "ошибка api ключа", "incorrect apikey", "api key", "circuit breaker", "ошибка разрешений api")):
        if any(k in s for k in ("order not exist", "balance not enough", "insufficient", "timestamp mismatch")):
            return False
        return True
    if "ошибка инициализации бота" in s:
        return True
    # Не-критичные — не слать
    for pat in _compiled_non_critical:
        if pat.search(msg):
            return False
    return False


def is_non_critical_api_error(msg: str) -> bool:
    """Ошибка API, которую не нужно ретровать и не слать в Telegram."""
    if not msg:
        return False
    for pat in _compiled_non_critical:
        if pat.search(msg):
            return True
    return False


def get_user_friendly_message(error: Exception, context: str = "") -> Optional[str]:
    """
    Краткое понятное сообщение для пользователя (если критично).
    """
    msg = str(error).strip()
    if not msg:
        return None
    # API ключ
    if "incorrect apikey" in msg.lower() or "api key" in msg.lower():
        return (
            "❌ Ошибка API ключа!\n\n"
            "Проверьте правильность API ключа и секрета.\n"
            "Убедитесь, что ключ имеет разрешение Spot Trading.\n\n"
            "Используйте /set_api для обновления ключей."
        )
    # Timeout
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return (
            "⚠️ Превышено время ожидания ответа от API BingX.\n\n"
            "Возможные причины:\n"
            "• Проблемы с интернетом\n"
            "• Временные проблемы на стороне биржи\n\n"
            "Бот продолжит работу после восстановления."
        )
    # Остальные критичные — коротко
    if is_telegram_critical(msg):
        return f"🚨 Ошибка: {msg[:200]}" + ("..." if len(msg) > 200 else "")
    return None
