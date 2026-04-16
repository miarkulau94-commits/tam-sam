"""
Unit tests for structured_logging — contextvars and StructuredContextFilter.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from structured_logging import (
    StructuredContextFilter,
    clear_log_context,
    get_log_context,
    set_log_context,
)


@pytest.fixture(autouse=True)
def _isolated_log_context():
    clear_log_context()
    yield
    clear_log_context()


class TestSetGetLogContext:
    def test_defaults_are_dash(self):
        assert get_log_context() == {"user_id": "-", "symbol": "-", "order_id": "-"}

    def test_set_partial_only_updates_passed_fields(self):
        set_log_context(user_id=42)
        assert get_log_context()["user_id"] == 42
        assert get_log_context()["symbol"] == "-"
        assert get_log_context()["order_id"] == "-"

    def test_none_arguments_do_not_overwrite(self):
        set_log_context(user_id=1, symbol="BTCUSDT", order_id="a1")
        set_log_context(user_id=None, symbol=None, order_id=None)
        ctx = get_log_context()
        assert ctx["user_id"] == 1
        assert ctx["symbol"] == "BTCUSDT"
        assert ctx["order_id"] == "a1"

    def test_user_id_zero_is_preserved(self):
        set_log_context(user_id=0)
        assert get_log_context()["user_id"] == 0

    def test_empty_string_symbol_sets_then_shows_dash_in_get(self):
        set_log_context(symbol="")
        assert get_log_context()["symbol"] == "-"


class TestClearLogContext:
    def test_clear_resets_to_defaults(self):
        set_log_context(user_id=99, symbol="ETHUSDT", order_id="x")
        clear_log_context()
        assert get_log_context() == {"user_id": "-", "symbol": "-", "order_id": "-"}

class TestStructuredContextFilter:
    def test_filter_adds_context_fields_to_record(self):
        set_log_context(user_id=7, symbol="BNBUSDT", order_id="oid-1")
        flt = StructuredContextFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert record.user_id == 7
        assert record.symbol == "BNBUSDT"
        assert record.order_id == "oid-1"
