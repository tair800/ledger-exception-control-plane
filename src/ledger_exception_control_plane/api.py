"""FastAPI application shell: lifecycle, correlation middleware, liveness and readiness.

This module contains no business behaviour and must not acquire any. Its whole purpose is
to prove the service starts, is configured, is observable, and can honestly report whether
its mandatory dependencies are reachable.

Liveness and readiness are deliberately separate endpoints with different failure meanings.
Collapsing them is a common and expensive mistake: an orchestrator restarts a container that
fails liveness, so a liveness probe that depends on PostgreSQL turns a database blip into a
restart storm that removes the very capacity needed to recover.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ledger_exception_control_plane import __version__
from ledger_exception_control_plane.config import Settings, is_valid_correlation_id
from ledger_exception_control_plane.db.engine import create_engine
from ledger_exception_control_plane.health import (
    DependencyHealth,
    check_postgres,
    check_redis,
    run_probe,
)
from ledger_exception_control_plane.log import (
    configure_logging,
    new_correlation_id,
    set_correlation_id,
)
from ledger_exception_control_plane.routes import router
from ledger_exception_control_plane.security import PrincipalRegistry

logger = logging.getLogger(__name__)

LIVENESS_PATH: Final = "/healthz"
READINESS_PATH: Final = "/readyz"

POSTGRES_DEPENDENCY: Final = "postgres"
REDIS_DEPENDENCY: Final = "redis"


class LivenessResponse(BaseModel):
    """Liveness payload. Carries nothing that could be sensitive."""

    model_config = ConfigDict(extra="forbid")

    status: str
    service: str
    version: str


class DependencyReport(BaseModel):
    """One dependency's readiness result: a name and a status, never a reason string."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str


class ReadinessResponse(BaseModel):
    """Readiness payload."""

    model_config = ConfigDict(extra="forbid")

    status: str
    dependencies: list[DependencyReport]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Accepts settings so tests can construct an app without touching the environment.
    """
    resolved = settings or Settings()

    configure_logging(
        service_name=resolved.service_name,
        environment=resolved.environment,
        level=resolved.log_level,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Logged without any DSN: the point is to prove startup happened and under which
        # configuration identity, not to echo connection strings.
        logger.info(
            "service starting",
            extra={"version": __version__, "environment": resolved.environment},
        )
        yield
        logger.info("service stopping", extra={"version": __version__})

    app = FastAPI(
        title=resolved.service_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None if resolved.environment == "production" else "/docs",
        redoc_url=None,
    )
    app.state.settings = resolved
    # Parsed once at construction so a malformed registry fails at startup rather than at the first
    # approval — an authentication table that silently parses to empty is an outage that presents as
    # a permissions problem. Empty is legal and means "no principal can authenticate": fail closed.
    app.state.principals = PrincipalRegistry.from_json(resolved.principals)
    app.state.engine = create_engine(resolved)
    app.include_router(router)

    @app.middleware("http")
    async def correlation_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Bind a correlation id for the request and return it to the caller.

        An inbound id is trusted only if it satisfies the documented policy — short, and
        drawn from ``[A-Za-z0-9_-]``. Anything else is silently replaced with a generated
        id rather than rejected: a malformed header does not merit failing a request, but
        it must never reach a log record verbatim, or the header becomes a log-injection
        vector.
        """
        header_name = resolved.correlation_id_header
        supplied = request.headers.get(header_name)
        correlation_id = (
            supplied
            if supplied is not None and is_valid_correlation_id(supplied)
            else new_correlation_id()
        )

        set_correlation_id(correlation_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            # Emitted here, inside the correlation scope, rather than relying on uvicorn's
            # access log. Uvicorn writes its line after this middleware has unbound the
            # context variable, so that line always carried `correlation_id: null` — which
            # is useless precisely where a correlation id is most wanted. Metadata only:
            # no request or response body, by policy.
            logger.info(
                "http request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            response.headers[header_name] = correlation_id
            return response
        finally:
            set_correlation_id(None)

    @app.get(LIVENESS_PATH, response_model=LivenessResponse, tags=["health"])
    async def liveness() -> LivenessResponse:
        """Liveness: is this process alive and serving?

        Checks nothing external, by design. It must stay green while PostgreSQL or Redis is
        down, otherwise a dependency outage becomes a restart loop.
        """
        return LivenessResponse(status="alive", service=resolved.service_name, version=__version__)

    @app.get(READINESS_PATH, response_model=ReadinessResponse, tags=["health"])
    async def readiness() -> Response:
        """Readiness: can this instance accept work that needs its dependencies?

        Probes PostgreSQL and Redis under a bounded timeout, read-only. Returns 503 if
        either is unavailable, with per-dependency status and no diagnostic detail.
        """
        results = await gather_dependency_health(resolved)
        healthy = all(result.is_healthy for result in results)

        body = ReadinessResponse(
            status="ready" if healthy else "not_ready",
            dependencies=[
                DependencyReport(name=result.name, status=result.status.value) for result in results
            ],
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content=body.model_dump(),
        )

    return app


async def gather_dependency_health(settings: Settings) -> list[DependencyHealth]:
    """Probe every mandatory dependency concurrently, each under its own bounded timeout.

    Concurrency is not a micro-optimisation here, it is what keeps the bound meaningful.
    Awaited in sequence, readiness latency is the *sum* of the per-dependency timeouts — a
    measured 3.07s against the real stack with two dependencies at 2s each, and worse with
    every dependency added. Run together, the endpoint is bounded by the *slowest single*
    probe instead, so the worst case stays flat as the dependency list grows.

    ``run_probe`` never raises, so no ``return_exceptions`` handling is needed: every
    failure has already been converted into a status by the time it arrives here.
    """
    timeout = settings.readiness_timeout_seconds

    async def postgres() -> object:
        return await check_postgres(settings.postgres_dsn.get_secret_value())

    async def redis() -> object:
        return await check_redis(settings.redis_dsn.get_secret_value())

    return list(
        await asyncio.gather(
            run_probe(POSTGRES_DEPENDENCY, postgres, timeout_seconds=timeout),
            run_probe(REDIS_DEPENDENCY, redis, timeout_seconds=timeout),
        )
    )
