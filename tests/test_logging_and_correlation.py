"""Structured logging and the correlation-id contract.

The correlation id is externally supplied, so it is untrusted input that ends up inside a
log record. These tests pin the policy that keeps a header from becoming a log-injection
payload, and pin the stable field set later observability work depends on.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from ledger_exception_control_plane import api
from ledger_exception_control_plane.api import create_app
from ledger_exception_control_plane.config import Settings, is_valid_correlation_id
from ledger_exception_control_plane.health import DependencyHealth, DependencyStatus
from ledger_exception_control_plane.log import (
    JsonLogFormatter,
    correlation_id_scope,
    get_correlation_id,
    new_correlation_id,
)
from tests.conftest import SECRET_PASSWORD

HEADER = "X-Request-ID"


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --------------------------------------------------------------------------------------
# Correlation id policy
# --------------------------------------------------------------------------------------


def test_valid_incoming_correlation_id_is_propagated_to_the_response(
    settings: Settings,
) -> None:
    supplied = "req-01HZX9K4T7QP2ABC_de-f"

    with _client(settings) as client:
        response = client.get("/healthz", headers={HEADER: supplied})

    assert response.headers[HEADER] == supplied


def test_missing_correlation_id_is_generated(settings: Settings) -> None:
    with _client(settings) as client:
        response = client.get("/healthz")

    generated = response.headers[HEADER]
    assert generated
    assert is_valid_correlation_id(generated)


@pytest.mark.parametrize(
    ("label", "supplied"),
    [
        ("oversized", "a" * 129),
        ("newline injection", 'abc"}\n{"level":"CRITICAL","event":"forged'),
        ("carriage return", "abc\rdef"),
        ("whitespace", "has spaces"),
        ("empty", ""),
        ("punctuation", "abc;DROP"),
    ],
)
def test_invalid_correlation_id_is_replaced_not_echoed(
    settings: Settings, label: str, supplied: str
) -> None:
    """Anything failing the policy is replaced with a generated id.

    The request still succeeds — a malformed header does not merit a 4xx — but the supplied
    value must never be returned or reach a log record, or the header is a log-injection
    vector. ``label`` names the attack shape for readable failure output.
    """
    with _client(settings) as client:
        response = client.get("/healthz", headers={HEADER: supplied})

    returned = response.headers[HEADER]
    assert response.status_code == 200, label
    assert returned != supplied, label
    assert is_valid_correlation_id(returned), label


def test_correlation_id_is_bound_during_the_request_and_cleared_after(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Application logs for a request must carry that request's id, and only during it."""
    seen: list[str | None] = []

    async def capture_then_report(_: Settings) -> list[DependencyHealth]:
        seen.append(get_correlation_id())
        return [DependencyHealth(name="postgres", status=DependencyStatus.HEALTHY)]

    monkeypatch.setattr(api, "gather_dependency_health", capture_then_report)
    supplied = "correlation-abc123"

    with _client(settings) as client:
        client.get("/readyz", headers={HEADER: supplied})

    assert seen == [supplied], "handler did not observe the request's correlation id"
    assert get_correlation_id() is None, "correlation id leaked outside the request scope"


def test_generated_correlation_ids_are_unique() -> None:
    assert len({new_correlation_id() for _ in range(100)}) == 100


# --------------------------------------------------------------------------------------
# Structured log records
# --------------------------------------------------------------------------------------


def test_log_record_contains_the_required_stable_fields() -> None:
    formatter = JsonLogFormatter(service_name="lecp", environment="ci")

    with correlation_id_scope("abc123"):
        payload = json.loads(formatter.format(_record()))

    for field in ("timestamp", "level", "event", "logger", "service", "environment"):
        assert field in payload, f"missing stable field {field!r}"

    assert payload["level"] == "INFO"
    assert payload["event"] == "something happened"
    assert payload["service"] == "lecp"
    assert payload["environment"] == "ci"
    assert payload["correlation_id"] == "abc123"


def test_log_output_is_one_json_object_per_line() -> None:
    """Line-delimited JSON is what makes the output machine-readable downstream."""
    formatter = JsonLogFormatter(service_name="lecp", environment="ci")
    formatted = formatter.format(_record())

    assert "\n" not in formatted
    assert isinstance(json.loads(formatted), dict)


def test_extra_fields_are_nested_so_they_cannot_overwrite_stable_fields() -> None:
    """Application data must not be able to forge ``level`` or ``service``."""
    formatter = JsonLogFormatter(service_name="lecp", environment="ci")

    payload = json.loads(formatter.format(_record(service="forged", dependency="postgres")))

    assert payload["service"] == "lecp", "extra= overwrote a stable field"
    assert payload["context"]["service"] == "forged"
    assert payload["context"]["dependency"] == "postgres"


def test_correlation_id_is_null_outside_a_request() -> None:
    formatter = JsonLogFormatter(service_name="lecp", environment="ci")

    payload = json.loads(formatter.format(_record()))

    assert payload["correlation_id"] is None


def test_exception_logging_records_the_type_but_not_the_traceback() -> None:
    """Tracebacks routinely carry DSNs; the type is enough for triage."""
    formatter = JsonLogFormatter(service_name="lecp", environment="ci")
    try:
        raise ConnectionRefusedError(f"postgresql://lecp:{SECRET_PASSWORD}@db:5432/lecp")
    except ConnectionRefusedError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
        payload = json.loads(formatter.format(record))

    assert payload["error_type"] == "ConnectionRefusedError"
    assert SECRET_PASSWORD not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)


def test_secret_configuration_is_never_emitted_into_logs(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Startup and probe logging must not contain a DSN or a password."""
    formatter = JsonLogFormatter(
        service_name=settings.service_name, environment=settings.environment
    )

    with caplog.at_level(logging.DEBUG), _client(settings) as client:
        client.get("/readyz")

    rendered = "\n".join(formatter.format(record) for record in caplog.records)

    assert SECRET_PASSWORD not in rendered
    assert "postgresql://" not in rendered
    assert "redis://" not in rendered


def test_request_log_line_carries_the_correlation_id_and_metadata(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The per-request log line must be emitted inside the correlation scope.

    Uvicorn's own access log is written after the middleware unbinds the context variable,
    so it always reported ``correlation_id: null`` — useless exactly where a correlation id
    matters most. The application emits its own line instead; this pins that it carries the
    id plus request metadata, and no body.
    """
    supplied = "log-line-check-123"

    with caplog.at_level(logging.INFO), _client(settings) as client:
        client.get("/healthz", headers={HEADER: supplied})

    records = [r for r in caplog.records if r.getMessage() == "http request"]
    assert records, "no per-request log line was emitted"

    record = records[-1]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.path == "/healthz"  # type: ignore[attr-defined]
    assert record.status_code == 200  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, float)  # type: ignore[attr-defined]

    formatter = JsonLogFormatter(service_name="lecp", environment="ci")
    with correlation_id_scope(supplied):
        payload = json.loads(formatter.format(record))
    assert payload["correlation_id"] == supplied
    assert "body" not in payload.get("context", {})
