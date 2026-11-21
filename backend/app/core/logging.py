"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = re.compile(
    r"(password|secret|token|authorization|api[_-]?key|cookie|pin|otp|code)", re.IGNORECASE
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_REDACTED = "[REDACTED]"


def redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEYS.search(key):
        return _REDACTED
    if isinstance(value, str):
        return _BEARER.sub(r"\1" + _REDACTED, value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key == "event":
            continue
        event_dict[key] = redact_value(key, event_dict[key])
    return event_dict


QUIET_HTTP_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    # httpx logs every request's full URL at INFO, query string included. Meta's
    # OAuth and debug_token calls carry the app secret there, so keep those quiet.
    for name in QUIET_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
