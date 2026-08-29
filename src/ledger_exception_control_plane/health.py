"""Dependency probes backing the readiness endpoint.

Three properties matter here and each is covered by a test:

* **Bounded.** Every probe runs under ``asyncio.timeout``. A dependency that hangs is worse
  than one that refuses connections, because an unbounded probe turns a slow dependency into
  a slow health endpoint and then into a false outage across every caller.
* **Non-mutating.** ``SELECT 1`` and ``PING``. A readiness probe that writes is a readiness
  probe that can corrupt.
* **Silent about detail.** A probe returns only healthy/unhealthy and a fixed reason code.
  The exception, its message and the DSN never leave this module — the readiness response is
  reachable by anyone who can reach the service.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class DependencyStatus(enum.StrEnum):
    """Outcome of a single dependency probe."""

    HEALTHY = "healthy"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    """Result of probing one dependency.

    Carries no exception, no message and no connection detail — only a name and a status —
    so it is safe to serialise straight into an HTTP response.
    """

    name: str
    status: DependencyStatus

    @property
    def is_healthy(self) -> bool:
        return self.status is DependencyStatus.HEALTHY


class DependencyProbe(Protocol):
    """A single dependency check.

    Implementations must not raise: a probe that raises would let a driver-specific
    exception, and whatever it carries, escape into the response path.
    """

    name: str

    async def __call__(self) -> DependencyHealth: ...


class PlainProbe(Protocol):
    """The awaitable a dependency check performs, with no return value of interest."""

    async def __call__(self) -> object: ...


async def run_probe(
    name: str,
    probe: PlainProbe,
    *,
    timeout_seconds: float,
) -> DependencyHealth:
    """Run one probe under a bounded timeout, converting every failure into a status.

    This is the only place a dependency exception is caught, and it is caught broadly on
    purpose: the drivers raise different hierarchies, and the caller must never receive one.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            await probe()
    except TimeoutError:
        logger.warning("dependency probe timed out", extra={"dependency": name})
        return DependencyHealth(name=name, status=DependencyStatus.TIMED_OUT)
    except Exception:
        # Deliberately not logging the exception object or its message: a driver error
        # commonly embeds the DSN, and the DSN carries a password.
        logger.warning("dependency probe failed", extra={"dependency": name})
        return DependencyHealth(name=name, status=DependencyStatus.UNAVAILABLE)
    return DependencyHealth(name=name, status=DependencyStatus.HEALTHY)


async def check_postgres(dsn: str) -> object:
    """Open a connection, run ``SELECT 1``, close it. Read-only by construction."""
    import asyncpg  # imported lazily so importing the package needs no live driver

    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval("SELECT 1")
    finally:
        await connection.close()


async def check_redis(dsn: str) -> object:
    """Issue a ``PING``. Read-only by construction."""
    import redis.asyncio as redis  # imported lazily, as above

    client = redis.from_url(dsn)
    try:
        return await client.ping()
    finally:
        await client.aclose()
