"""M4.1 against real PostgreSQL — the claim, and the identifier once it is stored.

Two of the increment's obligations cannot be established without a database, and they are the two
the exit criteria name: *two workers provably cannot claim one residual*, and the identifier is
*persisted before dispatch*. Everything about the derivation itself is proven in
``test_operation_identity.py``, which needs nothing but Python.

**The claim tests are deliberately not two coroutines gathered and hoped to overlap.** With
``SKIP LOCKED`` that shape is worse than weak, it is actively misleading: if the two workers happen
to run one after another — which is what usually happens — both claim the same residual and the test
fails intermittently for a reason that has nothing to do with the lock. Every race below is forced
with an explicit handshake, so the second worker provably runs while the first still holds its
transaction open.

The established helper in ``test_classification_postgres.py`` cannot be reused here for the same
reason: it waits for a backend to *block* on a lock, and the entire point of ``SKIP LOCKED`` is that
nothing ever blocks.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_operations_postgres.py -m integration
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import decimal
import os
import pathlib
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import (
    Approval,
    ExceptionClassification,
    TreatmentCode,
)
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from ledger_exception_control_plane.operations import (
    Claim,
    IdentifierContradictionError,
    OperationRecord,
    RecordingRefusedError,
    claim_residuals,
    derive_identity,
    record_operation,
)

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

#: Deliberately fixed, never `now()`: a test whose seeded ordering depends on wall-clock resolution
#: is a test that fails on a fast machine.
EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

#: How long a claim that must *not* block is allowed to take before the test calls it blocked.
NON_BLOCKING_BUDGET = 10.0

#: Real account codes from the demo policy. Spelled out rather than looked up, so a test asserting
#: on the stored value is not comparing the policy against itself — and four digits, because that
#: is what ``money.is_account_code`` accepts and what the boundary check now enforces.
REBOOK_ACCOUNT = "4100"
WRITE_OFF_ACCOUNT = "4900"


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    assert_target_is_disposable(_settings())
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
    yield


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A per-test engine holding no idle connections, for the reason recorded in the matching
    suite: a pooled function-scoped engine leaves idle sockets behind until asyncpg's timeout
    starts firing somewhere unrelated."""
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_slate() -> AsyncIterator[None]:
    await _wipe()
    yield


async def _wipe() -> None:
    """Delete in dependency order, deepest first.

    ``adjustment`` before ``approval`` before ``treatment_proposal`` before ``exception``, because
    every one of those foreign keys is RESTRICT — a leftover row blocks the deletion of the thing it
    points at, which is the constraint doing exactly what it exists to do.
    """
    connection = await asyncpg.connect(DSN)
    try:
        for table in (
            "adjustment",
            "approval",
            "treatment_proposal",
            "exception",
            "match_result",
            "settlement_line",
            "settlement_batch",
        ):
            await connection.execute(f"DELETE FROM {table}")
    finally:
        await connection.close()


async def _seed_residual(
    *, marker: str, created_at: dt.datetime = EPOCH, amount: str = "2799.97"
) -> uuid.UUID:
    """One settlement batch, one unmatched line, one open exception. Returns the exception id.

    Direct SQL rather than the ingestion and classification path: this file is about the claim and
    the identifier, and driving the whole pipeline to obtain one open exception would put a great
    deal of unrelated machinery between a failure and its cause.
    """
    batch_id, line_id, exception_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES ($1, $2, 'test', $3, $4, 'parsed')",
            batch_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            b"raw",
            EPOCH,
        )
        await connection.execute(
            "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
            " merchant_reference, transaction_type, amount, currency, value_date, match_state)"
            " VALUES ($1, $2, 1, $3, $4, 'capture', $5, 'EUR', $6, 'unmatched')",
            line_id,
            batch_id,
            f"psp_{marker}",
            f"ORD-{marker}",
            decimal.Decimal(amount),
            EPOCH.date(),
        )
        await connection.execute(
            "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
            " status, rule_id, classifier_version, correlation_id, created_at)"
            " VALUES ($1, $2, 'unmatched', 'fee_split', 'open', 'fees_deducted_from_a_capture',"
            " 'residual-r2', $3, $4)",
            exception_id,
            line_id,
            f"lecp:{marker}",
            created_at,
        )
    finally:
        await connection.close()
    return exception_id


async def _seed_approval(
    exception_id: uuid.UUID,
    *,
    treatment: str = "rebook",
    principal: str = "controller-a",
    decision: str = "approved",
    resolution_version: int = 1,
) -> uuid.UUID:
    """An approved resolution, inserted directly.

    The approval *gate* is 5.1; the approval *table* has existed since 1.2. Seeding rows here is
    what lets 4.1 be built and proven in the order the plan sequences it, and is exactly what
    ``test_schema_postgres.py`` already does.
    """
    approval_id = uuid.uuid4()
    approved = None if decision == "rejected" else treatment
    # M5.1's `requested_by_iff_edited`: an edited treatment carries the principal who *asked* for
    # it, and nothing else does. §16's countersignature rule is then a check constraint comparing
    # the two columns, so the requester has to be a different principal — an edit seeded with the
    # approver's own name would be refused, which is the control working rather than a nuisance.
    requested_by = "analyst-a" if decision == "edited" else None
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            # M5.1 made `approval_token` NOT NULL and unique. These seeds predate it, so each
            # carries the approval's own id: unique by construction, and recognisably not a
            # token anybody issued.
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, requested_by, approval_token, decided_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            approval_id,
            exception_id,
            resolution_version,
            decision,
            approved,
            principal,
            requested_by,
            str(approval_id),
            EPOCH,
        )
    finally:
        await connection.close()
    return approval_id


def _instruction(exception_id: uuid.UUID, **overrides: Any) -> AdjustmentInstruction:
    """A realistic priced instruction for one exception, with any field replaced."""
    base = AdjustmentInstruction(
        exception_id=exception_id,
        treatment=TreatmentCode.REBOOK,
        amount=decimal.Decimal("2799.97"),
        currency="EUR",
        # A real account from the demo policy and the calculator's real rounding mode. The first
        # version used "4000-REVENUE" and ROUND_HALF_EVEN — neither producible by the money path,
        # which constrains account codes to four digits and fixes ROUND_HALF_UP. A reviewer pointed
        # out that the suite was affirmatively documenting a malformed account as acceptable, and
        # the boundary check added this round now refuses it, so the fixture had to become honest
        # before the tests could pass.
        account_code=REBOOK_ACCOUNT,
        period="2026-06",
        quantum=MONEY_QUANTUM,
        rounding=ROUNDING,
        ledger_context_version=DEMO_LEDGER_CONTEXT.version,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


async def _wait_until_a_backend_is_blocked_on_a_lock(timeout: float = 30.0) -> None:
    """Block until PostgreSQL reports a session waiting on a lock in this database.

    Copied in from ``test_classification_postgres.py``, where it replaced a sleep that looked like a
    handshake and was not: the earlier tests slept 250 ms and asserted the task was unfinished,
    which it was — because it was still opening its connection, not because it had reached the lock.

    **It is useless for the claim tests and essential for this one**, which is the distinction worth
    stating. ``SELECT … FOR UPDATE SKIP LOCKED`` never waits, so a claim test calling this would
    fail with "no backend ever blocked". ``record_operation`` takes a plain blocking ``FOR UPDATE``
    on the approval, so the second worker genuinely does park behind the first, and asking the
    database is the only way to know it has arrived rather than merely not finished yet.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    watcher = await asyncpg.connect(DSN)
    try:
        while loop.time() < deadline:
            waiting = await watcher.fetchval(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND state = 'active'"
                " AND wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.05)
    finally:
        await watcher.close()
    raise AssertionError("no backend ever blocked on a lock; the race under test did not happen")


async def _adjustment_rows() -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        return await connection.fetch(
            "SELECT id, approval_id, operation_id, instruction_payload_hash, amount, currency,"
            " account_code, period, approved_treatment, approving_principal FROM adjustment"
        )
    finally:
        await connection.close()


# ======================================================================================
# Two workers, one residual — the exit criterion
# ======================================================================================


async def _claim_and_hold(
    engine: AsyncEngine, *, limit: int, claimed: asyncio.Event, release: asyncio.Event
) -> Claim:
    """Claim, announce it, and keep the transaction open until told to let go.

    The transaction is what holds the lock, so this is the only honest way to have one worker
    *still holding* a residual while another tries for it.
    """
    async with AsyncSession(engine) as session, session.begin():
        claim = await claim_residuals(session, limit=limit)
        claimed.set()
        await release.wait()
        return claim


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_one_residual(engine: AsyncEngine) -> None:
    """**The exit criterion, and acceptance criterion 9.**

    One residual exists. The first worker claims it and holds its transaction open; only then does
    the second worker run. The handshake is what makes this a proof rather than a hope — without it
    the two would usually run in sequence and both would succeed, and the test would pass while
    establishing nothing.
    """
    exception_id = await _seed_residual(marker="one")
    second_engine = create_async_engine(async_dsn(_settings()), poolclass=NullPool)

    claimed, release = asyncio.Event(), asyncio.Event()
    try:
        first = asyncio.create_task(
            _claim_and_hold(engine, limit=10, claimed=claimed, release=release)
        )
        await asyncio.wait_for(claimed.wait(), timeout=NON_BLOCKING_BUDGET)

        async with AsyncSession(second_engine) as session, session.begin():
            second = await asyncio.wait_for(
                claim_residuals(session, limit=10), timeout=NON_BLOCKING_BUDGET
            )

        release.set()
        held = await asyncio.wait_for(first, timeout=NON_BLOCKING_BUDGET)
    finally:
        release.set()
        await second_engine.dispose()

    assert held.claimed + second.claimed == 1, "one residual was claimed twice"
    assert held.exception_ids == {exception_id}
    assert second.claimed == 0
    assert second.requested == 10, "the loser asked for work and was told there was none free"


@pytest.mark.asyncio
async def test_a_second_worker_skips_a_held_residual_and_takes_the_next_one(
    engine: AsyncEngine,
) -> None:
    """**The property that distinguishes this claim from every other lock in the repository.**

    ADR-041 claims a settlement batch with a blocking ``FOR UPDATE`` and is right to: two deliveries
    of one payload are the same work, so the loser should wait. A residual queue is the opposite —
    the second worker wants a *different* row, and blocking would serialise the whole queue behind
    the first worker for no reason.

    This is the test that fails if ``skip_locked`` is ever dropped: a plain ``FOR UPDATE`` would
    block on the held row rather than stepping over it, and the wait would expire.
    """
    held_id = await _seed_residual(marker="held", created_at=EPOCH)
    free_id = await _seed_residual(marker="free", created_at=EPOCH + dt.timedelta(hours=1))
    second_engine = create_async_engine(async_dsn(_settings()), poolclass=NullPool)

    claimed, release = asyncio.Event(), asyncio.Event()
    try:
        # Limit 1 so the first worker holds only the older residual.
        first = asyncio.create_task(
            _claim_and_hold(engine, limit=1, claimed=claimed, release=release)
        )
        await asyncio.wait_for(claimed.wait(), timeout=NON_BLOCKING_BUDGET)

        async with AsyncSession(second_engine) as session, session.begin():
            second = await asyncio.wait_for(
                claim_residuals(session, limit=10), timeout=NON_BLOCKING_BUDGET
            )

        release.set()
        held = await asyncio.wait_for(first, timeout=NON_BLOCKING_BUDGET)
    finally:
        release.set()
        await second_engine.dispose()

    assert held.exception_ids == {held_id}, "the first worker holds the oldest residual"
    assert second.exception_ids == {free_id}, "the second stepped over it and took the next"
    assert held_id not in second.exception_ids


@pytest.mark.asyncio
async def test_a_residual_held_by_a_worker_that_dies_is_claimable_again(
    engine: AsyncEngine,
) -> None:
    """§14: *"claimed work times out and returns"* — with no reaper, lease or expiry sweep.

    The claim is the transaction, so losing the transaction releases it. Rolling back is what a
    crashed worker's connection does on its behalf, which is why this design needs no expiry policy
    — and an expiry policy is exactly where a claim column would have gone wrong, since one that
    fires early hands a single residual to two workers.
    """
    exception_id = await _seed_residual(marker="dies")

    async with AsyncSession(engine) as session, session.begin():
        first = await claim_residuals(session, limit=10)
        assert first.exception_ids == {exception_id}
        await session.rollback()

    async with AsyncSession(engine) as session, session.begin():
        again = await claim_residuals(session, limit=10)

    assert again.exception_ids == {exception_id}, "the residual was stranded by a lost worker"


@pytest.mark.asyncio
async def test_a_committed_claim_also_releases_the_residual(engine: AsyncEngine) -> None:
    """The other end of the same transaction rule, and a limit worth stating out loud.

    Committing releases the lock too. Nothing at 4.1 marks a residual as handled — that is a status
    transition the dispatcher owns (4.2) — so a claim is exclusive only *while it is held*, and this
    records that plainly rather than leaving a reader to assume otherwise.
    """
    exception_id = await _seed_residual(marker="commit")

    async with AsyncSession(engine) as session, session.begin():
        await claim_residuals(session, limit=10)

    async with AsyncSession(engine) as session, session.begin():
        again = await claim_residuals(session, limit=10)

    assert again.exception_ids == {exception_id}


# ======================================================================================
# What the claim selects
# ======================================================================================


@pytest.mark.asyncio
async def test_residuals_are_claimed_oldest_first(engine: AsyncEngine) -> None:
    """Oldest first, so the residual closest to breaching any later SLA is taken first."""
    third = await _seed_residual(marker="c", created_at=EPOCH + dt.timedelta(hours=2))
    first = await _seed_residual(marker="a", created_at=EPOCH)
    second = await _seed_residual(marker="b", created_at=EPOCH + dt.timedelta(hours=1))

    async with AsyncSession(engine) as session, session.begin():
        claim = await claim_residuals(session, limit=2)

    assert [residual.exception_id for residual in claim.residuals] == [first, second]
    assert third not in claim.exception_ids


@pytest.mark.asyncio
async def test_residuals_created_at_the_same_instant_are_still_totally_ordered(
    engine: AsyncEngine,
) -> None:
    """**The tiebreaker, exercised.**

    ``order_by(created_at, id)`` is documented as a total order so two workers walk the queue in
    the same sequence — but every other test here seeds distinct timestamps, so deleting ``, id``
    left the whole suite green. A batch of residuals created by one classification run shares a
    timestamp to whatever resolution the column stores, which makes this the *ordinary* case rather
    than a contrived one.
    """
    # A list comprehension, not a generator: `sorted` cannot consume an async generator, and
    # PostgreSQL orders `uuid` as sixteen big-endian bytes, which is the order `UUID.__lt__`
    # gives too — so the expected sequence really is the sorted one.
    ids = sorted([await _seed_residual(marker=f"tie{n}", created_at=EPOCH) for n in range(4)])

    async with AsyncSession(engine) as session, session.begin():
        first = await claim_residuals(session, limit=2)
    async with AsyncSession(engine) as session, session.begin():
        again = await claim_residuals(session, limit=2)

    assert [r.exception_id for r in first.residuals] == ids[:2], "ties are not broken by id"
    assert [r.exception_id for r in again.residuals] == ids[:2], "and not broken the same way twice"


@pytest.mark.asyncio
async def test_a_claimed_residual_carries_a_real_classification_member(
    engine: AsyncEngine,
) -> None:
    """**A regression test for the StrEnum trap, met a third time in this increment.**

    ``exception.classification`` is a ``String(32)`` with a check constraint, an enum annotation and
    no type decorator, so the driver hands back a bare ``str``. A ``StrEnum`` compares *and hashes*
    equal to its value, so a consumer's ``==`` and ``in`` keep working while ``is`` silently fails
    and ``.value`` raises — and 4.2's dispatcher is the consumer. Four reviewers found this
    independently, forty lines from where the sibling module had already fixed it.
    """
    await _seed_residual(marker="enum")

    async with AsyncSession(engine) as session, session.begin():
        claim = await claim_residuals(session, limit=1)

    (residual,) = claim.residuals
    assert isinstance(residual.classification, ExceptionClassification)
    assert residual.classification is ExceptionClassification.FEE_SPLIT
    assert residual.classification.value == "fee_split"


@pytest.mark.asyncio
async def test_a_resolved_exception_is_not_claimable(engine: AsyncEngine) -> None:
    """``status = 'open'`` is the whole predicate, and this is what shows it is doing work."""
    open_id = await _seed_residual(marker="open")
    resolved_id = await _seed_residual(marker="resolved")

    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "UPDATE exception SET status = 'resolved' WHERE id = $1", resolved_id
        )
    finally:
        await connection.close()

    async with AsyncSession(engine) as session, session.begin():
        claim = await claim_residuals(session, limit=10)

    assert claim.exception_ids == {open_id}


@pytest.mark.asyncio
async def test_an_empty_queue_and_a_fully_held_queue_are_distinguishable(
    engine: AsyncEngine,
) -> None:
    """Both return nothing, and they are different events: an idle queue and a busy one.

    ``requested`` is on the result for exactly this reason. A caller that cannot tell them apart
    would treat contention as "there is no work" and stop.
    """
    async with AsyncSession(engine) as session, session.begin():
        idle = await claim_residuals(session, limit=5)

    assert idle.claimed == 0
    assert idle.requested == 5

    await _seed_residual(marker="busy")
    second_engine = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    claimed, release = asyncio.Event(), asyncio.Event()
    try:
        first = asyncio.create_task(
            _claim_and_hold(engine, limit=10, claimed=claimed, release=release)
        )
        await asyncio.wait_for(claimed.wait(), timeout=NON_BLOCKING_BUDGET)
        async with AsyncSession(second_engine) as session, session.begin():
            busy = await asyncio.wait_for(
                claim_residuals(session, limit=5), timeout=NON_BLOCKING_BUDGET
            )
        release.set()
        await asyncio.wait_for(first, timeout=NON_BLOCKING_BUDGET)
    finally:
        release.set()
        await second_engine.dispose()

    assert busy.claimed == 0
    assert busy.requested == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_a_claim_must_ask_for_at_least_one_residual(engine: AsyncEngine, limit: int) -> None:
    """``LIMIT 0`` would return nothing and read as an empty queue, which is a lie."""
    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ValueError, match="at least one"):
            await claim_residuals(session, limit=limit)


@pytest.mark.asyncio
async def test_claiming_writes_nothing_at_all(engine: AsyncEngine) -> None:
    """A claim is a read under a lock. It changes no status and creates no row."""
    exception_id = await _seed_residual(marker="readonly")

    async with AsyncSession(engine) as session, session.begin():
        await claim_residuals(session, limit=10)

    connection = await asyncpg.connect(DSN)
    try:
        status = await connection.fetchval(
            "SELECT status FROM exception WHERE id = $1", exception_id
        )
        assert status == "open"
        for table in ("adjustment", "outbox", "posting_attempt", "audit_event"):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 0
    finally:
        await connection.close()


# ======================================================================================
# The identifier, persisted before dispatch
# ======================================================================================


@pytest.mark.asyncio
async def test_the_identifier_is_persisted_with_its_payload_hash(engine: AsyncEngine) -> None:
    """The third deliverable: the identifier exists in the database before anything could send it.

    Derived at persist time and never at call time. An identifier computed at the moment of sending
    is computed from whatever the configuration happens to be then, so a re-send after an account
    mapping changed would put a *different* key on the wire for what the system believes is the same
    operation — and under ``ENFORCES_KEY`` the provider would apply it twice.
    """
    exception_id = await _seed_residual(marker="persist")
    approval_id = await _seed_approval(exception_id)
    instruction = _instruction(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        record = await record_operation(session, approval_id=approval_id, instruction=instruction)

    assert record.created is True
    expected = derive_identity(instruction, exception_id=exception_id, resolution_version=1)
    assert record.identity == expected

    (row,) = await _adjustment_rows()
    assert row["operation_id"] == expected.operation_id
    assert row["instruction_payload_hash"] == expected.instruction_payload_hash
    assert row["amount"] == decimal.Decimal("2799.9700")
    assert row["approved_treatment"] == "rebook"
    assert row["approving_principal"] == "controller-a"
    assert row["account_code"] == REBOOK_ACCOUNT


@pytest.mark.asyncio
async def test_recording_the_same_operation_twice_writes_one_row(engine: AsyncEngine) -> None:
    """Idempotent by re-derivation rather than by a flag: attempt one and attempt five agree."""
    exception_id = await _seed_residual(marker="twice")
    approval_id = await _seed_approval(exception_id)
    instruction = _instruction(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        first = await record_operation(session, approval_id=approval_id, instruction=instruction)
    async with AsyncSession(engine) as session, session.begin():
        second = await record_operation(session, approval_id=approval_id, instruction=instruction)

    assert first.identity == second.identity
    assert first.adjustment_id == second.adjustment_id
    assert first.created is True
    assert second.created is False, "the second call must not claim it created the row"
    assert len(await _adjustment_rows()) == 1


@pytest.mark.asyncio
async def test_two_workers_recording_one_approval_write_one_adjustment(
    engine: AsyncEngine,
) -> None:
    """**Idempotence under a real race, not just when called twice in sequence.**

    A gap the sequential test cannot close: two workers that both read "no adjustment yet" and both
    insert would collide on ``uq_adjustment_approval_id`` — the constraint doing its job, but
    surfacing as an integrity error rather than as the no-op it actually is.

    ``record_operation`` takes a row lock on the *approval* before looking, so the second worker
    waits and then observes the finished work. The session runs at READ COMMITTED, so the statement
    it issues after acquiring the lock takes a fresh snapshot and sees the first worker's committed
    row — which is what makes the lock sufficient rather than merely comforting.

    Forced with two separate engines and ``asyncio.gather``. Unlike the claim tests, a handshake is
    not needed and would not help: the whole point is that whichever order they arrive in, exactly
    one row exists and both callers get the same identifier.
    """
    exception_id = await _seed_residual(marker="racerecord")
    approval_id = await _seed_approval(exception_id)
    instruction = _instruction(exception_id)

    second_engine = create_async_engine(async_dsn(_settings()), poolclass=NullPool)

    async def record(target: AsyncEngine) -> OperationRecord:
        async with AsyncSession(target) as session, session.begin():
            return await record_operation(session, approval_id=approval_id, instruction=instruction)

    # Forced, not gathered. `asyncio.gather` usually runs these one after the other, and every
    # assertion below holds under sequential execution — so on its own it would establish nothing
    # about the lock. The first worker opens its transaction, records, and *waits* before
    # committing; only then is the second released. A reviewer showed the gathered version passing
    # with the row lock removed entirely.
    recorded, release = asyncio.Event(), asyncio.Event()

    async def record_and_hold(target: AsyncEngine) -> OperationRecord:
        async with AsyncSession(target) as session, session.begin():
            result = await record_operation(
                session, approval_id=approval_id, instruction=instruction
            )
            recorded.set()
            await release.wait()
            return result

    try:
        first = asyncio.create_task(record_and_hold(engine))
        await asyncio.wait_for(recorded.wait(), timeout=NON_BLOCKING_BUDGET)

        # The second worker must now block on the approval's row lock rather than proceeding to
        # its own insert. Proven, not assumed: it is still pending while the first holds.
        second = asyncio.create_task(record(second_engine))
        await _wait_until_a_backend_is_blocked_on_a_lock()
        assert not second.done(), "the second worker raced past the approval lock"

        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=NON_BLOCKING_BUDGET
        )
    finally:
        release.set()
        await second_engine.dispose()

    records: list[OperationRecord] = []
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), f"concurrent recording raised: {outcome!r}"
        records.append(outcome)

    rows = await _adjustment_rows()
    assert len(rows) == 1, "one approval produced two adjustments"

    assert {record.identity.operation_id for record in records} == {rows[0]["operation_id"]}, (
        "the two workers disagreed on the identifier"
    )
    assert sorted(record.created for record in records) == [False, True], (
        "exactly one worker may report that it created the row"
    )


@pytest.mark.asyncio
async def test_a_changed_instruction_for_a_recorded_approval_is_refused(
    engine: AsyncEngine,
) -> None:
    """**Never an overwrite.**

    A genuinely different instruction is a different operation, reached by superseding the
    resolution — which increments ``resolution_version`` and is interlocked (4.4). Overwriting would
    be the worst option available: the stored identifier may already be on the wire, so replacing it
    would leave the system unable to recognise its own operation.
    """
    exception_id = await _seed_residual(marker="changed")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        original = await record_operation(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(IdentifierContradictionError, match="different operation"):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(
                    exception_id,
                    account_code=WRITE_OFF_ACCOUNT,
                ),
            )

    (row,) = await _adjustment_rows()
    assert row["operation_id"] == original.identity.operation_id, "the stored identifier survived"
    assert row["account_code"] == REBOOK_ACCOUNT


@pytest.mark.asyncio
async def test_a_superseding_resolution_is_a_different_operation(engine: AsyncEngine) -> None:
    """§12.1's stated consequence, shown end to end rather than asserted in a docstring.

    Two approvals on one exception at versions 1 and 2, each with its own adjustment, and two
    different identifiers. This is not the supersession *interlock* — blocking a new version while a
    prior operation is non-terminal is 4.4's, and nothing here enforces it.
    """
    exception_id = await _seed_residual(marker="supersede")
    first_approval = await _seed_approval(exception_id, resolution_version=1)
    second_approval = await _seed_approval(exception_id, resolution_version=2)
    instruction = _instruction(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        first = await record_operation(session, approval_id=first_approval, instruction=instruction)
        second = await record_operation(
            session, approval_id=second_approval, instruction=instruction
        )

    assert first.identity.operation_id != second.identity.operation_id
    assert first.identity.instruction_payload_hash == second.identity.instruction_payload_hash
    assert len(await _adjustment_rows()) == 2


@pytest.mark.asyncio
async def test_changing_only_the_approver_does_not_change_the_identifier(
    engine: AsyncEngine,
) -> None:
    """**The plan's sixth obligation, end to end.**

    §16 permits the approver to differ for the same economic event — a re-approval, or an edit
    requiring a different principal. An identifier that varied with them would vary with a
    non-financial input, which is the mirror image of retry-dependence and fails just as silently.

    Establishing this needs the whole world rebuilt, because ``approval`` is uniquely constrained on
    ``(exception_id, resolution_version)``: there is no way to hold everything constant and vary the
    principal within one database state. So the first world is recorded, wiped, and rebuilt
    identically with a different controller — and the identifier must be the same value.
    """
    exception_id = uuid.uuid4()

    async def record_with(principal: str) -> str:
        await _wipe()
        seeded = await _seed_residual_with_id(exception_id, marker="approver")
        approval_id = await _seed_approval(seeded, principal=principal)
        async with AsyncSession(engine) as session, session.begin():
            record = await record_operation(
                session, approval_id=approval_id, instruction=_instruction(seeded)
            )
        (row,) = await _adjustment_rows()
        assert row["approving_principal"] == principal, "the approver really did change"
        return record.identity.operation_id

    assert await record_with("controller-a") == await record_with("controller-b")


async def _seed_residual_with_id(exception_id: uuid.UUID, *, marker: str) -> uuid.UUID:
    """Seed a residual under a caller-chosen exception id, so a world can be rebuilt identically."""
    batch_id, line_id = uuid.uuid4(), uuid.uuid4()
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES ($1, $2, 'test', $3, $4, 'parsed')",
            batch_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            b"raw",
            EPOCH,
        )
        await connection.execute(
            "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
            " merchant_reference, transaction_type, amount, currency, value_date, match_state)"
            " VALUES ($1, $2, 1, $3, $4, 'capture', 2799.97, 'EUR', $5, 'unmatched')",
            line_id,
            batch_id,
            f"psp_{marker}",
            f"ORD-{marker}",
            EPOCH.date(),
        )
        await connection.execute(
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
        await connection.close()
    return exception_id


# ======================================================================================
# What recording refuses
# ======================================================================================


@pytest.mark.asyncio
async def test_an_instruction_priced_for_another_exception_is_refused(
    engine: AsyncEngine,
) -> None:
    """**The cross-exception guard no constraint can provide.**

    ``adjustment`` has no ``exception_id`` column — it reaches the exception only through the
    approval — so nothing in the database would notice exception A's amount being posted under
    exception B's authorisation.
    """
    mine = await _seed_residual(marker="mine")
    theirs = await _seed_residual(marker="theirs")
    approval_id = await _seed_approval(mine)

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="prices exception"):
            await record_operation(
                session, approval_id=approval_id, instruction=_instruction(theirs)
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_an_instruction_priced_for_another_treatment_is_refused(
    engine: AsyncEngine,
) -> None:
    """An amount computed for one decision must not be posted under a different one.

    The composite foreign key carries the approval's treatment into ``adjustment`` and so keeps the
    *column* honest — but it cannot see that the amount was computed for something else.
    """
    exception_id = await _seed_residual(marker="treatment")
    approval_id = await _seed_approval(exception_id, treatment="write_off")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="prices rebook"):
            await record_operation(
                session, approval_id=approval_id, instruction=_instruction(exception_id)
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_a_rejected_approval_authorises_nothing(engine: AsyncEngine) -> None:
    """FR-7: no ledger write without a human decision that actually authorised one."""
    exception_id = await _seed_residual(marker="rejected")
    approval_id = await _seed_approval(exception_id, decision="rejected")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="authorises no ledger write"):
            await record_operation(
                session, approval_id=approval_id, instruction=_instruction(exception_id)
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_an_approval_that_does_not_exist_authorises_nothing(engine: AsyncEngine) -> None:
    """A forged approval id must not reach the composite foreign key and become an error there."""
    exception_id = await _seed_residual(marker="forged")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="nothing authorises"):
            await record_operation(
                session, approval_id=uuid.uuid4(), instruction=_instruction(exception_id)
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_an_edited_approval_still_authorises(engine: AsyncEngine) -> None:
    """The control for the refusals above. ``edited`` is an authorising decision, not a rejection —
    a controller who changed the treatment still decided."""
    exception_id = await _seed_residual(marker="edited")
    approval_id = await _seed_approval(exception_id, decision="edited", treatment="accrue")

    async with AsyncSession(engine) as session, session.begin():
        record = await record_operation(
            session,
            approval_id=approval_id,
            instruction=_instruction(exception_id, treatment=TreatmentCode.ACCRUE),
        )

    assert record.created is True
    (row,) = await _adjustment_rows()
    assert row["approved_treatment"] == "accrue"


@pytest.mark.asyncio
async def test_a_treatment_that_is_a_bare_string_is_refused_cleanly(engine: AsyncEngine) -> None:
    """**A regression test for a crash on the refusal path itself.**

    ``AdjustmentInstruction`` is a plain frozen dataclass with no runtime type check, so its
    ``treatment`` can hold a bare ``str`` — which is exactly what a caller gets when it rebuilds an
    instruction from persisted values, since ``adjustment.approved_treatment`` is a ``String(16)``
    with no type decorator.

    The comparison was ``is not``, chosen to avoid ``StrEnum``'s equal-to-its-value behaviour, so a
    bare string took the *refusal* branch and then raised ``AttributeError`` on ``.value``. A caller
    expecting ``RecordingRefusedError`` got an attribute error from inside the guard.
    """
    exception_id = await _seed_residual(marker="strtreat")
    approval_id = await _seed_approval(exception_id, treatment="write_off")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="prices rebook"):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(exception_id, treatment="rebook"),
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_a_treatment_outside_the_vocabulary_is_refused(engine: AsyncEngine) -> None:
    """The other half: a string that is not a treatment at all names nothing."""
    exception_id = await _seed_residual(marker="notreat")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="not a treatment"):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(exception_id, treatment="post_it_anyway"),
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "overrides", "expected"),
    [
        (
            "an account code the policy would never issue",
            {"account_code": "NOT-AN-ACCOUNT"},
            "account code",
        ),
        (
            "an account code with SQL punctuation",
            {"account_code": "'; DROP TABLE adjustment; --"},
            "account code",
        ),
        ("an account code of the wrong length", {"account_code": "41000"}, "account code"),
        ("a period that is not YYYY-MM", {"period": "June 2026"}, "accounting period"),
        ("a month of 13", {"period": "2026-13"}, "accounting period"),
        ("a currency that is not ISO 4217", {"currency": "euro"}, "ISO 4217"),
        ("a lower-case currency", {"currency": "eur"}, "ISO 4217"),
    ],
)
async def test_an_instruction_the_calculator_could_not_have_produced_is_refused(
    engine: AsyncEngine, label: str, overrides: dict[str, Any], expected: str
) -> None:
    """**A regression test for trusting the instruction's type instead of its contents.**

    ``record_operation`` takes an ``AdjustmentInstruction``, and that type validates nothing — so
    "it came from the calculator" was a convention rather than a fact. An adversarial review walked
    a 48-character account code carrying SQL punctuation into a priced, identified, persisted
    financial instruction.

    ``account_code`` is the sharpest of these: ``adjustment.account_code`` is a bare ``String(64)``
    with **no check constraint at all**, so unlike the period and the currency there is no database
    backstop behind it. The money policy's four-digit rule was the only definition there was, and
    nothing on this path was consulting it.
    """
    exception_id = await _seed_residual(marker=f"bad{abs(hash(label)) % 10000}")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match=expected):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(exception_id, **overrides),
            )

    assert await _adjustment_rows() == [], label


@pytest.mark.asyncio
async def test_a_zero_adjustment_is_refused(engine: AsyncEngine) -> None:
    """§7, and the one calculator refusal with no database backstop.

    A zero adjustment instructs the ledger to do nothing while carrying the full weight of an
    approved financial instruction. ``compute_adjustment`` refuses to emit one; nothing stopped a
    hand-built instruction from storing one.
    """
    exception_id = await _seed_residual(marker="zero")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="do nothing"):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(exception_id, amount=decimal.Decimal("0.0000")),
            )

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_an_amount_the_column_could_not_store_is_refused(engine: AsyncEngine) -> None:
    """Refused here rather than by the column, so the caller's transaction survives it."""
    exception_id = await _seed_residual(marker="precise")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="cannot be stored exactly"):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(exception_id, amount=decimal.Decimal("1.23456")),
            )

        assert (await claim_residuals(session, limit=1)).claimed == 1, "the transaction survived"

    assert await _adjustment_rows() == []


@pytest.mark.asyncio
async def test_the_approval_is_re_read_under_the_lock_not_taken_from_the_session(
    engine: AsyncEngine,
) -> None:
    """**A regression test for a lock that took the row without taking its data.**

    ``select(...).with_for_update()`` locks the current row, and for an ``Approval`` already in the
    session's identity map the ORM hands back the *cached* instance — so the identifier would be
    derived from pre-lock values. ``resolution_version`` is the value at risk: it feeds the
    identifier, and unlike the treatment and the principal no column on ``adjustment`` re-checks it.

    Latent at 4.1, because no caller here loads an approval first. 4.2's dispatcher, which must read
    one to build the instruction, is exactly the caller that makes it live — which is why it is
    fixed now rather than when it bites.
    """
    exception_id = await _seed_residual(marker="stale")
    approval_id = await _seed_approval(exception_id, resolution_version=1)
    instruction = _instruction(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        # Load it first, exactly as a dispatcher would, so it is in the identity map.
        cached = (
            await session.execute(select(Approval).where(Approval.id == approval_id))
        ).scalar_one()
        assert cached.resolution_version == 1

        # Another connection supersedes it while this session holds its stale copy.
        connection = await asyncpg.connect(DSN)
        try:
            await connection.execute(
                "UPDATE approval SET resolution_version = 7 WHERE id = $1", approval_id
            )
        finally:
            await connection.close()

        record = await record_operation(session, approval_id=approval_id, instruction=instruction)

    expected = derive_identity(instruction, exception_id=exception_id, resolution_version=7)
    assert record.identity == expected, (
        "the identifier was derived from the session's cached resolution version, not the row's"
    )


@pytest.mark.asyncio
async def test_a_stored_payload_hash_that_disagrees_is_a_contradiction(
    engine: AsyncEngine,
) -> None:
    """**A regression test for comparing one of two digests.**

    ``operation_id`` and ``instruction_payload_hash`` are independent columns with nothing tying
    them together beyond a hex-shape check. A row whose identifier is right and whose payload hash
    is not was returned as agreement — and the payload hash is the one that says *what* was priced.
    ADR-030's rule for a duplicated value is that it is verified rather than copied.

    Written around the application path deliberately, which is the same threat model the two
    unique-constraint tests below use: the question is what happens when the row is not what this
    code would have written.
    """
    exception_id = await _seed_residual(marker="halfright")
    approval_id = await _seed_approval(exception_id)
    instruction = _instruction(exception_id)
    identity = derive_identity(instruction, exception_id=exception_id, resolution_version=1)

    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO adjustment (id, approval_id, approved_treatment, approving_principal,"
            " amount, currency, account_code, period, operation_id, instruction_payload_hash)"
            " VALUES ($1, $2, 'rebook', 'controller-a', 2799.9700, 'EUR', $3, '2026-06', $4, $5)",
            uuid.uuid4(),
            approval_id,
            REBOOK_ACCOUNT,
            identity.operation_id,
            "d" * 64,  # the right identifier, the wrong payload
        )
    finally:
        await connection.close()

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(IdentifierContradictionError):
            await record_operation(session, approval_id=approval_id, instruction=instruction)


# ======================================================================================
# The database remains the final guard (ADR-041)
# ======================================================================================


@pytest.mark.asyncio
async def test_the_unique_constraint_refuses_a_duplicate_identifier_written_around_the_code(
    engine: AsyncEngine,
) -> None:
    """§12.2: *application logic is a convenience; the database is the guarantee.*

    The refusals above all live in Python, so on their own they would prove only that this code is
    careful. This bypasses every one of them and writes a second adjustment carrying an identifier
    that is already taken — and ``uq_adjustment_operation_id`` still refuses it.
    """
    exception_id = await _seed_residual(marker="dupe")
    approval_id = await _seed_approval(exception_id)
    other_approval = await _seed_approval(exception_id, resolution_version=2)

    async with AsyncSession(engine) as session, session.begin():
        record = await record_operation(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.UniqueViolationError, match="uq_adjustment_operation_id"):
            await connection.execute(
                "INSERT INTO adjustment (id, approval_id, approved_treatment, approving_principal,"
                " amount, currency, account_code, period, operation_id, instruction_payload_hash)"
                " VALUES ($1, $2, 'rebook', 'controller-a', 1.0000, 'EUR', 'acc', '2026-06',"
                " $3, $4)",
                uuid.uuid4(),
                other_approval,
                record.identity.operation_id,
                record.identity.instruction_payload_hash,
            )
    finally:
        await connection.close()

    assert len(await _adjustment_rows()) == 1


@pytest.mark.asyncio
async def test_one_approval_can_carry_only_one_adjustment(engine: AsyncEngine) -> None:
    """``uq_adjustment_approval_id``, also bypassing the application path."""
    exception_id = await _seed_residual(marker="oneper")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        await record_operation(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.UniqueViolationError, match="uq_adjustment_approval_id"):
            await connection.execute(
                "INSERT INTO adjustment (id, approval_id, approved_treatment, approving_principal,"
                " amount, currency, account_code, period, operation_id, instruction_payload_hash)"
                " VALUES ($1, $2, 'rebook', 'controller-a', 1.0000, 'EUR', 'acc', '2026-06',"
                " $3, $4)",
                uuid.uuid4(),
                approval_id,
                "b" * 64,
                "c" * 64,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_an_escalated_treatment_can_never_be_recorded(engine: AsyncEngine) -> None:
    """§6.2: escalation is the outcome *because* the case cannot be priced deterministically.

    An adjustment for an escalated treatment is a contradiction, and the database says so
    independently of anything this module does.
    """
    exception_id = await _seed_residual(marker="escalate")
    approval_id = await _seed_approval(exception_id, treatment="escalate")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(RecordingRefusedError, match="no priceable amount"):
            await record_operation(
                session,
                approval_id=approval_id,
                instruction=_instruction(exception_id, treatment=TreatmentCode.ESCALATE),
            )

        # **The transaction is still usable**, which is the half the previous version could not
        # assert. Accepting either `RecordingRefusedError` or `IntegrityError` was satisfied by two
        # entirely different systems, and the one that was actually happening — the database check
        # refusing the INSERT — deactivates the session: a failed flush rolls back to the root
        # transaction, so every later statement in the caller's unit of work fails too. A refusal
        # one step earlier is the same answer without the collateral damage.
        assert (await claim_residuals(session, limit=1)).claimed == 1

    assert await _adjustment_rows() == []


# ======================================================================================
# Scope: 4.1 touches nothing a later increment owns
# ======================================================================================


@pytest.mark.asyncio
async def test_recording_creates_no_row_in_any_later_increment_table(
    engine: AsyncEngine,
) -> None:
    """4.1 persists an identifier. It does not dispatch, attempt, retry, recover or audit."""
    exception_id = await _seed_residual(marker="scope")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        await record_operation(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    connection = await asyncpg.connect(DSN)
    try:
        for table in ("outbox", "posting_attempt", "dlq", "recovery_queue", "audit_event"):
            count = await connection.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"4.1 wrote to {table}, which belongs to a later increment"
        assert await connection.fetchval("SELECT count(*) FROM adjustment") == 1
    finally:
        await connection.close()
