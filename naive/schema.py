"""The naive baseline's own tables — §19's RED baseline (increment 4.5).

**Its own tables, and that is the load-bearing decision in this file.** `src/`'s schema does much of
the reliability work itself: ``adjustment.operation_id`` is NOT NULL and unique, ``outbox`` exists,
``settlement_batch.content_hash`` is unique, ``uq_approval_token`` makes a token single-use. A naive
implementation writing into those tables would be protected by *main's* constraints and would show
no failure at all — the comparison would be rigged in the direction that flatters the baseline and
hides the defect the gate exists to demonstrate.

So the baseline gets a schema that reflects what it knows about, which is: residual work, an
approval, and an adjustment. Four tables, no constraints beyond primary and foreign keys, no
uniqueness anywhere it would matter. Nothing here is *wrong* in the sense of being malformed — it is
the schema you write when the only question you have asked is "what do I need to store".

Created by ``CREATE TABLE IF NOT EXISTS`` rather than by a migration, deliberately: Alembic owns
``src/``'s schema and its history, and a naive table inside that history would put the baseline into
every deployment of the real system. These tables exist for the duration of a chaos run.
"""

from __future__ import annotations

from typing import Final

import asyncpg

__all__ = ["NAIVE_TABLES", "create_schema", "drop_schema", "truncate"]

#: The baseline's tables, deepest first — the order a teardown needs.
NAIVE_TABLES: Final[tuple[str, ...]] = (
    "naive_adjustment",
    "naive_approval",
    "naive_residual",
    "naive_batch",
)

#: No unique constraint on ``content_hash``, no unique constraint on an approval token, no operation
#: identifier on an adjustment. Each absence is a §19 scenario.
_DDL: Final = """
CREATE TABLE IF NOT EXISTS naive_batch (
    id            uuid PRIMARY KEY,
    content_hash  text NOT NULL,
    received_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS naive_residual (
    id            uuid PRIMARY KEY,
    batch_id      uuid NOT NULL REFERENCES naive_batch (id),
    reference     text NOT NULL,
    amount        numeric(20, 4) NOT NULL,
    currency      char(3) NOT NULL,
    account_code  text NOT NULL,
    period        text NOT NULL,
    state         text NOT NULL DEFAULT 'open',
    claimed_by    text
);

CREATE TABLE IF NOT EXISTS naive_approval (
    id           uuid PRIMARY KEY,
    residual_id  uuid NOT NULL REFERENCES naive_residual (id),
    principal    text NOT NULL,
    token        text NOT NULL,
    decided_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS naive_adjustment (
    id            uuid PRIMARY KEY,
    residual_id   uuid NOT NULL REFERENCES naive_residual (id),
    approval_id   uuid NOT NULL REFERENCES naive_approval (id),
    amount        numeric(20, 4) NOT NULL,
    currency      char(3) NOT NULL,
    account_code  text NOT NULL,
    period        text NOT NULL,
    posting_ref   text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
"""


async def create_schema(connection: asyncpg.Connection) -> None:
    """Create the baseline's tables if they are absent. Idempotent."""
    await connection.execute(_DDL)


async def drop_schema(connection: asyncpg.Connection) -> None:
    """Remove them. Deepest first, because the foreign keys are real even here."""
    for table in NAIVE_TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {table}")


async def truncate(connection: asyncpg.Connection) -> None:
    """Empty them between scenarios, deepest first."""
    for table in NAIVE_TABLES:
        await connection.execute(f"DELETE FROM {table}")
