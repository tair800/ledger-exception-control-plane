"""Health probe and endpoint behaviour.

Covers the properties M0.2 exists to establish: liveness is independent of dependencies,
readiness reflects real availability, probes are bounded, and nothing leaks.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from ledger_exception_control_plane import api
from ledger_exception_control_plane.api import create_app
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.health import (
    DependencyHealth,
    DependencyStatus,
    run_probe,
)
from tests.conftest import SECRET_PASSWORD


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


async def _all_healthy(_: Settings) -> list[DependencyHealth]:
    return [
        DependencyHealth(name="postgres", status=DependencyStatus.HEALTHY),
        DependencyHealth(name="redis", status=DependencyStatus.HEALTHY),
    ]


# --------------------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------------------


def test_liveness_succeeds_while_the_application_is_running(settings: Settings) -> None:
    with _client(settings) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_liveness_does_not_depend_on_postgres_or_redis(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness must stay green with both dependencies down.

    This is the property that stops a database blip becoming a restart storm: an
    orchestrator restarts containers that fail liveness, so a liveness probe coupled to
    PostgreSQL removes the capacity needed to recover.
    """

    async def every_dependency_is_down(_: Settings) -> list[DependencyHealth]:
        raise AssertionError("liveness must not probe dependencies at all")

    monkeypatch.setattr(api, "gather_dependency_health", every_dependency_is_down)

    with _client(settings) as client:
        response = client.get("/healthz")

    assert response.status_code == 200


# --------------------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------------------


def test_readiness_succeeds_when_both_dependencies_are_healthy(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "gather_dependency_health", _all_healthy)

    with _client(settings) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert {d["name"]: d["status"] for d in body["dependencies"]} == {
        "postgres": "healthy",
        "redis": "healthy",
    }


@pytest.mark.parametrize(
    ("failing", "healthy"),
    [("postgres", "redis"), ("redis", "postgres")],
)
def test_readiness_fails_when_a_mandatory_dependency_is_unavailable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, failing: str, healthy: str
) -> None:
    """Either dependency being down must make the instance not-ready."""

    async def one_is_down(_: Settings) -> list[DependencyHealth]:
        return [
            DependencyHealth(name=failing, status=DependencyStatus.UNAVAILABLE),
            DependencyHealth(name=healthy, status=DependencyStatus.HEALTHY),
        ]

    monkeypatch.setattr(api, "gather_dependency_health", one_is_down)

    with _client(settings) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    statuses = {d["name"]: d["status"] for d in body["dependencies"]}
    assert statuses[failing] == "unavailable"
    assert statuses[healthy] == "healthy"


# --------------------------------------------------------------------------------------
# Probe mechanics
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_is_bounded_by_its_timeout() -> None:
    """A hanging dependency must not hang the readiness endpoint.

    A dependency that accepts a connection and then never answers is the dangerous case:
    without a bound, readiness blocks for as long as the dependency chooses.
    """

    async def hangs_forever() -> object:
        await asyncio.sleep(30)
        return None

    started = time.monotonic()
    result = await run_probe("slow", hangs_forever, timeout_seconds=0.05)
    elapsed = time.monotonic() - started

    assert result.status is DependencyStatus.TIMED_OUT
    assert result.is_healthy is False
    assert elapsed < 5, f"probe took {elapsed:.2f}s; the timeout did not bound it"


@pytest.mark.asyncio
async def test_probe_converts_a_driver_exception_into_a_status() -> None:
    """A probe must never propagate a driver exception into the response path."""

    async def raises_with_a_dsn_in_the_message() -> object:
        raise ConnectionRefusedError(
            f"could not connect to postgresql://lecp:{SECRET_PASSWORD}@db:5432/lecp"
        )

    result = await run_probe("postgres", raises_with_a_dsn_in_the_message, timeout_seconds=1)

    assert result.status is DependencyStatus.UNAVAILABLE
    assert SECRET_PASSWORD not in repr(result)


@pytest.mark.asyncio
async def test_probe_reports_healthy_on_success() -> None:
    async def succeeds() -> object:
        return 1

    result = await run_probe("postgres", succeeds, timeout_seconds=1)

    assert result.status is DependencyStatus.HEALTHY
    assert result.is_healthy is True


# --------------------------------------------------------------------------------------
# Secret containment
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_responses_never_expose_secret_configuration(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """No health response may carry a password, a DSN, a host or a stack trace."""

    async def one_is_down(_: Settings) -> list[DependencyHealth]:
        return [
            DependencyHealth(name="postgres", status=DependencyStatus.UNAVAILABLE),
            DependencyHealth(name="redis", status=DependencyStatus.HEALTHY),
        ]

    monkeypatch.setattr(api, "gather_dependency_health", one_is_down)

    with _client(settings) as client:
        body = client.get(path).text

    assert SECRET_PASSWORD not in body
    assert "postgresql://" not in body
    assert "redis://" not in body
    assert "Traceback" not in body


def test_settings_repr_masks_secrets(settings: Settings) -> None:
    """A settings object reaching a log line or a traceback must not expose credentials."""
    assert SECRET_PASSWORD not in repr(settings)
    assert SECRET_PASSWORD not in str(settings.postgres_dsn)
    assert SECRET_PASSWORD not in str(settings.model_dump())
    # The real value is still reachable, but only through an explicit, greppable call.
    assert SECRET_PASSWORD in settings.postgres_dsn.get_secret_value()
