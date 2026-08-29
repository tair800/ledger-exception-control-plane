"""Alembic environment.

Configuration comes from the application's :class:`Settings`, never from ``alembic.ini``.
Two consequences, both deliberate:

* no connection string is committed, and none is printed;
* migrations can only run against an environment the application itself is configured for,
  so there is no developer-specific path or credential in the migration machinery.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import Base
from ledger_exception_control_plane.db.engine import async_dsn, create_engine

# Importing the models registers every table on ``Base.metadata``. Without this,
# autogenerate would see an empty model set and cheerfully propose dropping every table.
import ledger_exception_control_plane.db.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _settings() -> Settings:
    return Settings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful for review and for handing a DDL script to a DBA.
    """
    context.configure(
        url=async_dsn(_settings()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Both comparisons on: without them, autogenerate silently misses a changed column
        # type or a changed server default, and the model and the database drift apart
        # while every migration still appears to succeed.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database using the async engine."""
    engine = create_engine(_settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
