"""Shared harness for the chaos suite — one world, two branches (increment 4.5).

**Both branches face the same world, and this file is what makes that literal.** The same
PostgreSQL instance, the same three capability configurations, the same fault-injection port, the
same amount, the same fixed instant. §19's comparison is only worth something if the only difference
between the two columns is the code under test, so everything else lives here and is handed to both.

Marked ``integration``: every scenario needs a real database, because most of what `main` does about
these failures *is* database behaviour — a transaction-scoped claim, a unique constraint, an
append-only trail.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import os
import pathlib
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from naive.schema import create_schema, drop_schema, truncate
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from tests.chaos.results import OBSERVATIONS
from tests.chaos.scenarios import AMOUNT, EPOCH

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

REBOOK_ACCOUNT = "4100"

#: The tables the chaos suite clears between scenarios, deepest first.
#:
#: Both schemas, because both branches run against the same database and a residual left by one
#: column would be work the other column found waiting for it.
_WIPE_ORDER = (
    "naive_adjustment",
    "naive_approval",
    "naive_residual",
    "naive_batch",
    "recovery_queue",
    "dlq",
    "posting_attempt",
    "outbox",
    "adjustment",
    "approval",
    "treatment_proposal_evidence",
    "treatment_proposal",
    "evidence",
    "exception",
    "match_result",
    "settlement_line",
    "settlement_batch",
    "ledger_entry",
)

#: Append-only by trigger, so clearing them means suspending a control the suite also asserts.
_APPEND_ONLY = (
    ("audit_event", "audit_event_append_only_row"),
    ("reconciliation_query", "reconciliation_query_append_only_row"),
)


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


def _prepare() -> None:
    """Migrate `main`'s schema to head and create the baseline's own tables beside it."""

    async def naive_tables() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            await create_schema(connection)
        finally:
            await connection.close()

    asyncio.run(naive_tables())


def _clear_audit_trail() -> None:
    """Empty ``audit_event``, suspending the trigger that makes it append-only.

    **This suite writes verbs an earlier schema cannot express, so it has to leave the database as
    it found it.** The 4.4 migration's downgrade *refuses* to re-narrow the audit vocabulary while
    `reconcile` or `recover` events exist — deliberately, because the table is append-only and the
    alternative would be a migration deciding on an operator's behalf to destroy history. The chaos
    suite emits both: reconciliation under ``BY_OPERATION_ID`` and an operator hand-off under
    ``NONE``/``NONE``.

    Without this, a chaos run killed part-way would leave those rows behind, and the *next* run's
    ``downgrade base`` would meet a refusal it did not cause — every test in both modules erroring
    at setup with a migration message about audit verbs. `chaos-table` and `chaos-check` depend on
    `chaos-verify`, so the flagship table could not be regenerated until someone reset the database.

    Called before the downgrade *and* after the module, which is the arrangement
    ``test_reconcile_postgres.py`` and ``test_audit_contract_postgres.py`` already use — the two
    other modules that emit these verbs. This one was written without it, and a reviewer found the
    omission by reading the three side by side.

    ``assert_target_is_disposable`` has already refused to run against anything but a throwaway
    database.
    """

    async def clear() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            if await connection.fetchval("SELECT to_regclass('public.audit_event')") is None:
                return
            await connection.execute(
                "ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only_row"
            )
            await connection.execute("DELETE FROM audit_event")
            await connection.execute(
                "ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only_row"
            )
        finally:
            await connection.close()

    asyncio.run(clear())


def _teardown() -> None:
    """Remove the baseline's tables and the audit rows a later downgrade could not re-narrow."""
    _clear_audit_trail()

    async def drop() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()

    asyncio.run(drop())


@pytest.fixture(scope="session", autouse=True)
def only_this_runs_observations() -> Iterator[None]:
    """Clear ``.chaos-observations/`` once per session, so a rendered table is one run's work.

    **What this closes.** The renderer requires all forty-two cells and refuses to publish a table
    with a gap — but a *stale* cell is not a gap. Run a subset (``-k`` something), render, and the
    published table would mix this run's observations with whatever an earlier one left on disk. The
    numbers would be real measurements of the right code; they would just not all be measurements of
    the same run, and §19's table is the one place in this repository where that distinction is the
    whole point.

    Clearing at session start converts every such case into the failure the renderer already
    handles: a subset run now leaves the other cells *missing*, and ``render`` names them and exits
    non-zero. Six of the eight reviewers of this increment raised the stale-cell risk and every
    adversarial verifier refused to confirm it, on the grounds that ``make chaos-verify`` runs the
    whole suite and a failing run never reaches the renderer. That is true of the documented path
    and not of the module invoked directly, so the hole was closed rather than argued about.
    """
    if OBSERVATIONS.exists():
        shutil.rmtree(OBSERVATIONS)
    yield


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """Head schema from zero, then the baseline's tables — and the baseline's tables removed after.

    The baseline's tables are created *after* the migration and are not part of it — Alembic owns
    `main`'s schema and its history, and a `naive_*` table inside that history would ship the RED
    baseline into every deployment of the real system.

    **Two things are cleaned up on the way out, and neither is tidiness.**

    `audit_event` is emptied before the downgrade and after the module — see
    :func:`_clear_audit_trail`, which explains what refuses what.

    The baseline's tables are dropped for a different reason. `test_schema_postgres.py`
    asserts that a migrated database holds *exactly* the tables the migration creates — "unexpected
    tables were created" — and in the whole-suite coverage run this package is collected first,
    because ``chaos`` sorts before ``test_``. A leftover `naive_*` table would therefore have made
    the migration's own integrity test fail, several modules later, for a reason that has nothing to
    do with migrations. Since the tables are not in Alembic's history, no later
    ``downgrade base`` removes them either: this fixture is the only thing that can.
    """
    assert_target_is_disposable(_settings())
    _clear_audit_trail()
    env = {**os.environ, "LECP_POSTGRES_DSN": DSN}
    for args in (("downgrade", "base"), ("upgrade", "head")):
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    _prepare()
    try:
        yield
    finally:
        _teardown()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def connection() -> AsyncIterator[asyncpg.Connection]:
    """A raw connection, for the baseline — which uses no ORM and shares no session machinery."""
    opened = await asyncpg.connect(DSN)
    try:
        yield opened
    finally:
        await opened.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_slate() -> AsyncIterator[None]:
    await wipe()
    yield


async def wipe() -> None:
    """Empty both schemas. Deepest first, because every foreign key here is real.

    **Each trigger suspension is its own transaction, and that is a correctness requirement rather
    than tidiness.** ``ALTER TABLE … DISABLE TRIGGER`` outside a transaction commits immediately, so
    a ``DELETE`` that raised between the disable and the enable would leave the append-only control
    switched **off for the rest of the session** — and `test_audit_contract_postgres.py` and
    `test_reconcile_postgres.py` assert that control in the same session during the coverage gate.
    Their assertions would then pass by having nothing to enforce them, which is the worst way for a
    guard to succeed.

    PostgreSQL rolls DDL back like anything else, so wrapping the three statements makes the
    suspension atomic: either the table is empty and the trigger is back, or neither happened.
    Several reviewers raised this independently; the mechanism was checked rather than argued.
    """
    opened = await asyncpg.connect(DSN)
    try:
        for table, trigger in _APPEND_ONLY:
            async with opened.transaction():
                await opened.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
                await opened.execute(f"DELETE FROM {table}")
                await opened.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        for table in _WIPE_ORDER:
            await opened.execute(f"DELETE FROM {table}")
        await truncate(opened)
    finally:
        await opened.close()


# ======================================================================================
# `main`'s side of the world
# ======================================================================================


async def seed_residual(*, marker: str, amount: decimal.Decimal = AMOUNT) -> uuid.UUID:
    """One batch, one unmatched line, one open exception. Returns the exception id.

    Direct SQL rather than driving ingestion and classification: most scenarios are about what
    happens *after* an exception exists, and putting the whole upstream pipeline between a failure
    and its cause would make a red result hard to attribute. The two scenarios that genuinely need
    real ingestion — duplicate delivery — call ``ingest`` itself.
    """
    batch_id, line_id, exception_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    opened = await asyncpg.connect(DSN)
    try:
        await opened.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES ($1, $2, 'chaos', $3, $4, 'parsed')",
            batch_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            b"raw",
            EPOCH,
        )
        await opened.execute(
            "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
            " merchant_reference, transaction_type, amount, currency, value_date, match_state)"
            " VALUES ($1, $2, 1, $3, $4, 'capture', $5, 'EUR', $6, 'unmatched')",
            line_id,
            batch_id,
            f"psp_{marker}",
            f"ORD-{marker}",
            amount,
            EPOCH.date(),
        )
        await opened.execute(
            "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
            " status, rule_id, classifier_version, correlation_id, created_at)"
            " VALUES ($1, $2, 'unmatched', 'fee_split', 'open', 'fees_deducted_from_a_capture',"
            " 'residual-r2', $3, $4)",
            exception_id,
            line_id,
            f"lecp:{marker}",
            EPOCH,
        )
    finally:
        await opened.close()
    return exception_id


async def seed_approval(
    exception_id: uuid.UUID, *, principal: str = "controller-a", token: str | None = None
) -> uuid.UUID:
    """An approved decision, inserted directly. Returns the approval id."""
    approval_id = uuid.uuid4()
    opened = await asyncpg.connect(DSN)
    try:
        await opened.execute(
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, approval_token, decided_at)"
            " VALUES ($1, $2, 1, 'approved', 'rebook', $3, $4, $5)",
            approval_id,
            exception_id,
            principal,
            token or str(approval_id),
            EPOCH,
        )
    finally:
        await opened.close()
    return approval_id


def instruction(
    exception_id: uuid.UUID, *, amount: decimal.Decimal = AMOUNT
) -> AdjustmentInstruction:
    """The financial instruction every scenario posts. One amount, so a duplicate is a count."""
    return AdjustmentInstruction(
        exception_id=exception_id,
        treatment=TreatmentCode.REBOOK,
        amount=amount,
        currency="EUR",
        account_code=REBOOK_ACCOUNT,
        period="2026-06",
        quantum=MONEY_QUANTUM,
        rounding=ROUNDING,
        ledger_context_version=DEMO_LEDGER_CONTEXT.version,
    )


async def enqueued(engine: AsyncEngine, *, marker: str) -> tuple[uuid.UUID, str]:
    """Seed, approve and enqueue one posting through `main`'s real path.

    Returns ``(adjustment_id, operation_id)``.
    """
    from ledger_exception_control_plane.operations import enqueue_posting

    exception_id = await seed_residual(marker=marker)
    approval_id = await seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=instruction(exception_id)
        )
    return record.adjustment_id, record.identity.operation_id


async def rows(table: str, **where: object) -> list[asyncpg.Record]:
    opened = await asyncpg.connect(DSN)
    try:
        if not where:
            return await opened.fetch(f"SELECT * FROM {table}")
        clause = " AND ".join(f"{column} = ${index + 1}" for index, column in enumerate(where))
        return await opened.fetch(f"SELECT * FROM {table} WHERE {clause}", *where.values())
    finally:
        await opened.close()


# ======================================================================================
# The baseline's side of the world
# ======================================================================================


async def naive_work(
    opened: asyncpg.Connection, *, marker: str, references: list[str] | None = None
) -> uuid.UUID:
    """One delivered payload of residual work in the baseline's own tables.

    Returns the batch id. The content hash is derived from the marker so a redelivery can present
    the identical payload — which is the whole of the duplicate-webhook scenario.
    """
    from naive.pipeline import ingest as naive_ingest

    return await naive_ingest(
        opened,
        content_hash=f"chaos-{marker}",
        residuals=[(reference, str(AMOUNT)) for reference in (references or [f"REF-{marker}"])],
        received_at=EPOCH,
    )


def one_day_later() -> dt.datetime:
    """Far enough past every §13.5 window that a negative answer becomes trustworthy."""
    return EPOCH + dt.timedelta(days=1)
