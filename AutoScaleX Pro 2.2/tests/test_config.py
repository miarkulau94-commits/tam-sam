"""
Точечные тесты загрузки config: перезагрузка модуля с отключённым dotenv и контролируемым env.
После каждого теста — reload, чтобы не оставлять в процессе устаревший config.
"""

import importlib
from decimal import Decimal
from unittest.mock import patch

import pytest

import config


def _reload_config():
    """Перечитать config без подмешивания .env с диска."""
    with patch("dotenv.load_dotenv", lambda *a, **k: None):
        importlib.reload(config)
    return config


@pytest.fixture(autouse=True)
def _restore_config_module():
    yield
    _reload_config()


class TestSymbolFromEnv:
    def test_valid_pair_parsed(self, monkeypatch):
        monkeypatch.setenv("SYMBOL", "BTC-USDT")
        _reload_config()
        assert config.SYMBOL == "BTC-USDT"
        assert config.BASE == "BTC"
        assert config.QUOTE == "USDT"

    def test_invalid_format_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SYMBOL", "BTCUSDT")
        _reload_config()
        assert config.SYMBOL == "ETH-USDT"
        assert config.BASE == "ETH"
        assert config.QUOTE == "USDT"

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SYMBOL", raising=False)
        _reload_config()
        assert config.SYMBOL == "ETH-USDT"


class TestFeeRateFromEnv:
    def test_custom_fee(self, monkeypatch):
        monkeypatch.setenv("FEE_RATE", "0.002")
        _reload_config()
        assert config.FEE_RATE == Decimal("0.002")

    def test_empty_string_uses_default(self, monkeypatch):
        monkeypatch.setenv("FEE_RATE", "")
        _reload_config()
        assert config.FEE_RATE == Decimal("0.001")

    def test_invalid_string_uses_default(self, monkeypatch):
        monkeypatch.setenv("FEE_RATE", "not_a_decimal")
        _reload_config()
        assert config.FEE_RATE == Decimal("0.001")


class TestConsoleLogFromEnv:
    def test_false_values(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_LOG", "false")
        _reload_config()
        assert config.CONSOLE_LOG is False

    def test_truthy_aliases(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_LOG", "1")
        _reload_config()
        assert config.CONSOLE_LOG is True


class TestTgAdminIdFromEnv:
    def test_numeric(self, monkeypatch):
        monkeypatch.setenv("TG_ADMIN_ID", "424242")
        _reload_config()
        assert config.TG_ADMIN_ID == 424242

    def test_empty_is_zero(self, monkeypatch):
        monkeypatch.setenv("TG_ADMIN_ID", "")
        _reload_config()
        assert config.TG_ADMIN_ID == 0
