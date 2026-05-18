"""Tests del logger JSON."""

from __future__ import annotations

import json
import logging

from app.observability.logger import JsonFormatter, setup_logging


def test_json_formatter_basic() -> None:
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hola",
        args=(),
        exc_info=None,
    )
    out = json.loads(fmt.format(record))
    assert out["level"] == "INFO"
    assert out["logger"] == "test"
    assert out["message"] == "hola"
    assert "ts" in out


def test_json_formatter_with_extra() -> None:
    """extra= se serializa al payload."""
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="agendado",
        args=(),
        exc_info=None,
    )
    record.session_id = "whatsapp:521..."
    record.tokens_input = 1234
    out = json.loads(fmt.format(record))
    assert out["session_id"] == "whatsapp:521..."
    assert out["tokens_input"] == 1234


def test_setup_logging_configures_root() -> None:
    setup_logging(level="DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1


def test_json_formatter_handles_non_serializable() -> None:
    """Valores no-JSON se convierten a str sin crashear."""
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="ok",
        args=(),
        exc_info=None,
    )
    record.bad = object()  # no serializable
    out = json.loads(fmt.format(record))
    assert "bad" in out
    assert isinstance(out["bad"], str)
