"""Async engine construction.

Kept deliberately thin at M1.1. There is no session factory, no unit of work and no
repository layer, because nothing in this increment reads or writes rows — the deliverable
is the schema. Alembic and the schema tests need an engine; that is all this provides.
"""

from __future__ import annotations

import urllib.parse
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ledger_exception_control_plane.config import Settings

#: SQLAlchemy needs its own driver-qualified scheme. The application configuration holds a
#: plain ``postgresql://`` DSN, which asyncpg's own client also accepts, so the driver
#: suffix is applied here rather than duplicating the DSN in configuration.
_ASYNC_DRIVER_PREFIX = "postgresql+asyncpg://"

#: libpq query parameters a managed provider puts in its connection string, which **asyncpg's
#: keyword path cannot take**, mapped to what it can.
#:
#: The asymmetry this exists for is worth stating, because it produces a deployment that looks
#: healthy and is not. ``asyncpg.connect(dsn_string)`` parses the URL itself and understands
#: ``sslmode`` — so ``/readyz``, which passes the DSN as a string, goes **green**. SQLAlchemy's
#: asyncpg dialect instead splits the query string into keyword arguments and calls
#: ``asyncpg.connect(**kwargs)``, and that signature has no ``**kwargs`` catch-all: ``sslmode`` and
#: ``channel_binding`` raise ``TypeError``. Every actual query fails while the health check reports
#: the database reachable.
#:
#: ``sslmode`` becomes ``ssl``, which asyncpg accepts and SQLAlchemy passes through. Anything that
#: asks for TLS maps to ``require``; ``disable`` is dropped rather than translated, because asyncpg
#: defaults to no TLS and inventing ``ssl=disable`` would be a value it does not define.
_TLS_MODES: Final = frozenset({"require", "verify-ca", "verify-full", "prefer", "allow"})

#: Dropped outright. asyncpg negotiates SCRAM channel binding as part of authentication; it is not
#: a connect argument, and passing it is the same ``TypeError`` as above.
_UNSUPPORTED_PARAMS: Final = frozenset({"channel_binding"})

#: A pooled endpoint runs pgBouncer in transaction mode, where a prepared statement created on one
#: server connection is not there on the next. asyncpg caches prepared statements by default, so
#: the second query on a recycled connection fails. Neon marks these hosts ``-pooler``.
_POOLED_HOST_MARKER: Final = "-pooler."


def _normalise_query(query: str, host: str) -> str:
    """Rewrite a libpq query string into what SQLAlchemy's asyncpg dialect can pass on.

    Unknown parameters are **kept**, not dropped: this function knows about the two that are known
    to break, and silently discarding anything else would turn a future provider's required option
    into a connection that quietly ignores it.
    """
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    seen = set()
    for key, value in pairs:
        lowered = key.lower()
        if lowered in _UNSUPPORTED_PARAMS:
            continue
        if lowered == "sslmode":
            if value.lower() in _TLS_MODES:
                out.append(("ssl", "require"))
                seen.add("ssl")
            continue
        out.append((key, value))
        seen.add(lowered)

    if _POOLED_HOST_MARKER in host and "prepared_statement_cache_size" not in seen:
        out.append(("prepared_statement_cache_size", "0"))

    return urllib.parse.urlencode(out)


def async_dsn(settings: Settings) -> str:
    """Return the configured PostgreSQL DSN in SQLAlchemy's async driver form.

    The secret is unwrapped at exactly one point, which stays greppable.

    The DSN is also normalised for asyncpg's keyword path — see :data:`_TLS_MODES`. A local
    development DSN carries no query string and comes back through this unchanged.
    """
    dsn = settings.postgres_dsn.get_secret_value()
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            remainder = dsn[len(prefix) :]
            break
    else:
        raise ValueError("postgres_dsn must use a postgresql:// scheme")

    parsed = urllib.parse.urlsplit(_ASYNC_DRIVER_PREFIX + remainder)
    query = _normalise_query(parsed.query, parsed.hostname or "")
    return urllib.parse.urlunsplit(parsed._replace(query=query))


def create_engine(settings: Settings) -> AsyncEngine:
    """Build an async engine. ``echo`` stays off so DSNs never reach the logs."""
    return create_async_engine(async_dsn(settings), echo=False, pool_pre_ping=True)
