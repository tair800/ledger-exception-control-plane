"""Integration checks against the real Docker Compose stack.

Excluded from the default run (``-m "not integration"`` in ``addopts``) so ordinary testing
stays deterministic and Docker-free. The same behaviour is already proven with fakes in
``test_health.py``; these tests exist to catch what fakes cannot — that the real wiring,
credentials, service names and drivers actually work.

Run with the stack up::

    make up
    make smoke
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("LECP_SMOKE_BASE_URL", "http://127.0.0.1:8000")
TIMEOUT = httpx.Timeout(10.0)
HEADER = "X-Request-ID"


def test_liveness_against_the_running_stack() -> None:
    response = httpx.get(f"{BASE_URL}/healthz", timeout=TIMEOUT)

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_against_the_running_stack() -> None:
    """Proves the app really reaches PostgreSQL and Redis over the Compose network."""
    response = httpx.get(f"{BASE_URL}/readyz", timeout=TIMEOUT)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert {d["name"]: d["status"] for d in body["dependencies"]} == {
        "postgres": "healthy",
        "redis": "healthy",
    }


def test_correlation_id_round_trip_against_the_running_stack() -> None:
    supplied = "smoke-test-correlation-id"

    response = httpx.get(f"{BASE_URL}/healthz", headers={HEADER: supplied}, timeout=TIMEOUT)

    assert response.headers[HEADER] == supplied


def test_health_responses_from_the_running_stack_leak_no_dsn() -> None:
    for path in ("/healthz", "/readyz"):
        body = httpx.get(f"{BASE_URL}{path}", timeout=TIMEOUT).text
        assert "postgresql://" not in body
        assert "redis://" not in body
        assert "lecp_local_dev" not in body
