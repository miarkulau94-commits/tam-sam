"""
Unit tests for error_handling - is_telegram_critical, is_non_critical_api_error, get_user_friendly_message
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from error_handling import (
    is_telegram_critical,
    is_non_critical_api_error,
    get_user_friendly_message,
)


class TestIsTelegramCritical:
    def test_empty_or_none_returns_false(self):
        assert is_telegram_critical("") is False
        assert is_telegram_critical(None) is False

    def test_not_string_returns_false(self):
        assert is_telegram_critical(123) is False

    def test_critical_api_key(self):
        assert is_telegram_critical("Ошибка API ключа! Incorrect apiKey") is True
        assert is_telegram_critical("api key invalid") is True

    def test_critical_circuit_breaker(self):
        assert is_telegram_critical("Circuit breaker открыт") is True

    def test_critical_bot_init(self):
        assert is_telegram_critical("Ошибка инициализации бота") is True

    def test_critical_overridden_by_non_critical(self):
        assert is_telegram_critical("Критическая ошибка: order not exist") is False
        assert is_telegram_critical("Balance not enough") is False

    def test_non_critical_timeout(self):
        assert is_telegram_critical("request timed out") is False

    def test_normal_message_not_critical(self):
        assert is_telegram_critical("Баланс: 100 USDT") is False


class TestIsNonCriticalApiError:
    def test_empty_returns_false(self):
        assert is_non_critical_api_error("") is False

    def test_order_not_exist(self):
        assert is_non_critical_api_error("order not exist") is True

    def test_balance_errors(self):
        assert is_non_critical_api_error("balance not enough") is True

    def test_timeout(self):
        assert is_non_critical_api_error("request timed out") is True


class TestGetUserFriendlyMessage:
    def test_empty_error_returns_none(self):
        assert get_user_friendly_message(Exception("")) is None

    def test_api_key_error(self):
        msg = get_user_friendly_message(Exception("Incorrect apiKey"))
        assert msg is not None
        assert "API" in msg

    def test_timeout_error(self):
        msg = get_user_friendly_message(Exception("request timed out"))
        assert msg is not None
