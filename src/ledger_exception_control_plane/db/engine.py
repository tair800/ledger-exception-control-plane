"""Async engine construction.

Kept deliberately thin at M1.1. There is no session factory, no unit of work and no
repository layer, because nothing in this increment reads or writes rows — the deliverable
is the schema. Alembic and the schema tests need an engine; that is all this provides.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ledger_exception_control_plane.config import Settings

#: SQLAlchemy needs its own driver-qualified scheme. The application configuration holds a
#: plain ``postgresql://`` DSN, which asyncpg's own client also accepts, so the driver
#: suffix is applied here rather than duplicating the DSN in configuration.
_ASYNC_DRIVER_PREFIX = "postgresql+asyncpg://"


def async_dsn(settings: Settings) -> str:
    """Return the configured PostgreSQL DSN in SQLAlchemy's async driver form.

    The secret is unwrapped at exactly one point, which stays greppable.
    """
    dsn = settings.postgres_dsn.get_secret_value()
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return _ASYNC_DRIVER_PREFIX + dsn[len(prefix) :]
    raise ValueError("postgres_dsn must use a postgresql:// scheme")


def create_engine(settings: Settings) -> AsyncEngine:
    """Build an async engine. ``echo`` stays off so DSNs never reach the logs."""
    return create_async_engine(async_dsn(settings), echo=False, pool_pre_ping=True)
