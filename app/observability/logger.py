"""Logger estructurado JSON con stdlib.

Decisión: stdlib + JSONFormatter, sin structlog. Ver `docs/DECISIONS.md` ADR-004.

Uso:
    from app.observability.logger import get_logger

    log = get_logger(__name__)
    log.info("mensaje", extra={"session_id": "...", "tokens": 1234})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Atributos estándar de LogRecord que NO van al campo `extra`
_RESERVED_LOG_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Formatter que emite logs como una línea JSON por evento."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Cualquier extra= que el caller haya pasado va al payload
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configura el root logger con JsonFormatter sobre stdout.

    Llamar UNA vez al arranque (desde main.py).
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Calmar loggers ruidosos de terceros
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    """Devuelve un logger con el nombre dado (usa __name__ típicamente)."""
    return logging.getLogger(name)
