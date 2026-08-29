"""Structured JSON logging with correlation-id propagation.

Built on the standard library rather than ``structlog`` or ``python-json-logger``: the
requirement is a stable field set rendered as JSON, and a formatter plus a ``ContextVar``
covers it in a few dozen lines. A dependency here would buy nothing and would have to be
carried by every project that copies this convention.

The field set is deliberately fixed, because later portfolio projects re-implement this
same shape and because the observability increment (8.1) attaches to these names:

``timestamp`` · ``level`` · ``event`` · ``service`` · ``environment`` · ``correlation_id``

Anything a caller passes as ``extra`` is nested under ``context`` so application data can
never collide with, or overwrite, a stable field.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

#: Marks handlers installed by :func:`configure_logging`, so repeated calls replace only
#: their own handler and never a foreign one.
_OWNED_HANDLER_FLAG = "_lecp_json_handler"

#: Correlation id for the request being handled, or ``None`` outside a request.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

#: ``LogRecord`` attributes that are part of the standard library's own record shape.
#: Everything else on a record came from ``extra=`` and belongs under ``context``.
_STANDARD_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def new_correlation_id() -> str:
    """Generate a correlation id. Hex only, so it always satisfies the inbound policy."""
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    """Return the correlation id for the current context, if one is bound."""
    return _correlation_id.get()


def set_correlation_id(correlation_id: str | None) -> None:
    """Bind a correlation id to the current context."""
    _correlation_id.set(correlation_id)


@contextmanager
def correlation_id_scope(correlation_id: str) -> Iterator[str]:
    """Bind a correlation id for the duration of a block, restoring the previous value."""
    token = _correlation_id.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _correlation_id.reset(token)


class JsonLogFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "service": self._service_name,
            "environment": self._environment,
            "correlation_id": get_correlation_id(),
        }

        context = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        if context:
            payload["context"] = context

        if record.exc_info:
            # The exception *type* is useful for triage. The formatted traceback is not
            # emitted here: it is verbose, and it is a common route for connection strings
            # and other secrets to reach a log aggregator.
            exc_type = record.exc_info[0]
            payload["error_type"] = exc_type.__name__ if exc_type else "UnknownError"

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(*, service_name: str, environment: str, level: str) -> None:
    """Install the JSON formatter on the root logger.

    Replaces existing handlers so a second call — in tests, or under a reloader — does not
    stack duplicate output.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter(service_name=service_name, environment=environment))
    setattr(handler, _OWNED_HANDLER_FLAG, True)

    root = logging.getLogger()
    # Remove only handlers this function installed previously, so a repeated call does not
    # stack duplicate output. Foreign handlers are left alone: clearing the root wholesale
    # would silently disable an embedding application's logging — and, as this project
    # found the hard way, pytest's caplog, which made a secret-leak test pass against an
    # empty record list rather than against real output.
    for existing in list(root.handlers):
        if getattr(existing, _OWNED_HANDLER_FLAG, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn installs its own handlers; let its records propagate to ours instead so every
    # line in the process shares one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # Uvicorn's access log is silenced deliberately. It is written after the correlation
    # middleware has unbound the context variable, so every one of its lines carried
    # `correlation_id: null` — useless exactly where a correlation id matters most. The
    # application emits its own request line instead, inside the correlation scope, with
    # method, path, status and duration. Leaving both on would duplicate every request.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
