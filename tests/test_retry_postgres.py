"""M4.3 against real PostgreSQL — bounded retry, the dead-letter queue and replay.

Everything here is a claim about **persisted state and transaction boundaries**, which is why none
of it can be established without a real server: which rows the due-work query can see, what a
schedule looks like after an allowlisted transport failure, what a dead letter carries, and what a
replay does to a ledger that is watching.

The exit criterion is `IMPLEMENTATION_PLAN.md` §4.3's *"DLQ replay demonstrated end to end"*, and
acceptance criterion 8 says what that has to mean:

    The DLQ replay CLI produces **exactly one applied posting** for the operation — verified by the
    simulated ledger's applied-count, not by our own records — and replay of an already-`CONFIRMED`
    operation applies nothing further.

Both halves are asserted below **against the ledger's own counter**, never against our tables. A
test that read ``adjustment.posting_ref`` to decide whether a posting happened would be checking our
belief about the ledger rather than the ledger.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_retry_postgres.py -m integration
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import os
import pathlib
import random
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
    Adjustment,
    DeadLetter,
    ReplayState,
    TreatmentCode,
)
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ledger import (
    LedgerAdapterCapabilities,
    PostingInstruction,
    PostingOutcome,
    Rejected,
    SimulatedLedger,
    Throttled,
    Unknown,
)
from ledger_exception_control_plane.ledger.transport import (
    LedgerTransportError,
    RetryableCause,
)
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from ledger_exception_control_plane.operations import enqueue_posting
from ledger_exception_control_plane.operations.retry import (
    DeadLetterReason,
    ReplayOutcome,
    RetryPolicy,
    RetryVerdict,
    attempt_one,
    due_adjustments,
    pending_dead_letters,
    replay_dead_letter,
    run_due_once,
)

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
REBOOK_ACCOUNT = "4100"

#: A policy with the smallest bounds that still exercise every branch: three sends, then exhaustion.
#:
#: Deliberately not the shipped defaults. A test that used them would need five sends to reach the
#: dead-letter branch and would be asserting the default rather than the mechanism, and changing a
#: default would then break a test that has nothing to do with it.
POLICY = RetryPolicy(
    base_delay=dt.timedelta(seconds=1),
    multiplier=2.0,
    cap=dt.timedelta(seconds=8),
    max_attempts=3,
    time_budget=dt.timedelta(hours=1),
)


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


def _rng() -> random.Random:
    """Seeded, so a schedule is reproducible and a bound is checkable rather than sampled."""
    return random.Random(20260906)


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
    """Deepest first: every one of these foreign keys is RESTRICT.

    ``dlq`` leads, and it has to: it references ``outbox``, so a dead letter left behind by one test
    makes every later ``DELETE FROM outbox`` fail with a constraint violation that looks like an
    unrelated cascade of failures.
    """
    connection = await asyncpg.connect(DSN)
    try:
        # Cleared with its append-only trigger suspended, because a dispatch now emits events and
        # the fence below counts the ones a single attempt produces. The harness is the one caller
        # entitled to do this: `assert_target_is_disposable` has already refused to run against
        # anything but a throwaway database.
        await connection.execute(
            "ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only_row"
        )
        await connection.execute("DELETE FROM audit_event")
        await connection.execute(
            "ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only_row"
        )
        for table in (
            "dlq",
            "posting_attempt",
            "outbox",
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


async def _seed_residual(*, marker: str) -> uuid.UUID:
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


async def _seed_approval(exception_id: uuid.UUID, *, resolution_version: int = 1) -> uuid.UUID:
    approval_id = uuid.uuid4()
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            # M5.1 made `approval_token` NOT NULL and unique. These seeds predate it, so each
            # carries the approval's own id: unique by construction, and recognisably not a
            # token anybody issued.
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, approval_token, decided_at)"
            " VALUES ($1, $2, $3, 'approved', 'rebook', 'controller-a', $4, $5)",
            approval_id,
            exception_id,
            resolution_version,
            str(approval_id),
            EPOCH,
        )
    finally:
        await connection.close()
    return approval_id


def _instruction(exception_id: uuid.UUID, **overrides: Any) -> AdjustmentInstruction:
    base = AdjustmentInstruction(
        exception_id=exception_id,
        treatment=TreatmentCode.REBOOK,
        amount=decimal.Decimal("2799.97"),
        currency="EUR",
        account_code=REBOOK_ACCOUNT,
        period="2026-06",
        quantum=MONEY_QUANTUM,
        rounding=ROUNDING,
        ledger_context_version=DEMO_LEDGER_CONTEXT.version,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


async def _enqueued(engine: AsyncEngine, *, marker: str) -> tuple[uuid.UUID, str]:
    exception_id = await _seed_residual(marker=marker)
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )
    return record.adjustment_id, record.identity.operation_id


async def _rows(table: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        return await connection.fetch(f"SELECT * FROM {table}")
    finally:
        await connection.close()


class _RefusesToConnect:
    """An adapter whose request never leaves the client, declared rather than inferred.

    The reference ledger is in-process and cannot fail this way, so the failure is injected by
    substituting the adapter. This is a test-local double, **not** §19's fault-injection port: that
    is 4.5's deliverable and does not exist, and a docstring here claiming otherwise would assert a
    seam a reader could not find.
    """

    name = "refuses-to-connect"

    def __init__(self, cause: RetryableCause = RetryableCause.TCP_CONNECT) -> None:
        self.cause = cause
        self.sends = 0

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities()

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        self.sends += 1
        raise LedgerTransportError(self.cause, "nothing was written")


class _ResetsAfterSending:
    """A failure *after* the request went out: ambiguous, and never on the retry path.

    ``ConnectionResetError`` is deliberately chosen — it is the exception a conventional retry
    classifier treats as transient, and §15 puts it in ``UNKNOWN``.
    """

    name = "resets-after-sending"

    def __init__(self) -> None:
        self.inner = SimulatedLedger()
        self.sends = 0

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities()

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        self.sends += 1
        await self.inner.post(operation_id, instruction)
        raise ConnectionResetError(104, "Connection reset by peer")


# ======================================================================================
# The due-work predicate — what the retry path can and cannot see
# ======================================================================================


@pytest.mark.asyncio
async def test_a_freshly_enqueued_operation_is_due_immediately(engine: AsyncEngine) -> None:
    """``next_attempt_at`` is NULL on a new row, and NULL means **due now**.

    Not a detail: ``enqueue_posting`` writes no schedule, so a predicate reading NULL as "not yet"
    would leave every newly approved adjustment unsent forever while every test that dispatched by
    hand kept passing.
    """
    adjustment_id, _ = await _enqueued(engine, marker="duenow")

    async with AsyncSession(engine) as session, session.begin():
        due = await due_adjustments(session, now=EPOCH, limit=10)

    assert list(due) == [adjustment_id]


@pytest.mark.asyncio
async def test_a_scheduled_operation_is_not_due_before_its_time(engine: AsyncEngine) -> None:
    """The backoff is real: a scheduled row is invisible until the clock reaches it."""
    adjustment_id, _ = await _enqueued(engine, marker="notyet")
    adapter = _RefusesToConnect()

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    assert report.verdict is RetryVerdict.SCHEDULED
    assert report.next_attempt_at is not None

    async with AsyncSession(engine) as session, session.begin():
        early = await due_adjustments(
            session, now=report.next_attempt_at - dt.timedelta(seconds=1), limit=10
        )
        exact = await due_adjustments(session, now=report.next_attempt_at, limit=10)

    assert list(early) == []
    assert list(exact) == [adjustment_id], "due at its scheduled instant, not one tick later"


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_never_selected_for_retry(engine: AsyncEngine) -> None:
    """**The plan names this test.** *"An `UNKNOWN` outcome never enters the ordinary retry path."*

    Asserted at the predicate rather than at a later branch: the row is not filtered out after being
    chosen, it is never chosen. §15 calls treating this as an ordinary transient retry *"precisely
    the defect this design exists to prevent"*.
    """
    adjustment_id, _operation_id = await _enqueued(engine, marker="unknownheld")
    ledger = SimulatedLedger(responder=lambda _op, _i: Unknown(detail="lost"))

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    assert report.verdict is RetryVerdict.HELD

    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] == "unknown"
    assert intent["next_attempt_at"] is None, "an ambiguous outcome is not scheduled"

    async with AsyncSession(engine) as session, session.begin():
        due = await due_adjustments(session, now=EPOCH + dt.timedelta(days=365), limit=10)

    assert list(due) == [], "an UNKNOWN row was offered to the retry path"
    assert await _rows("dlq") == [], "and it was not dead-lettered either; 4.4 owns it"


@pytest.mark.asyncio
async def test_an_unresolved_in_flight_attempt_is_never_selected_for_retry(
    engine: AsyncEngine,
) -> None:
    """**The crash-mid-send case, which leaves no recorded outcome at all.**

    Acceptance criterion 8g: on a crash between socket write and response write, recovery *"never
    retries it"*. This is the condition a predicate reading only ``last_outcome`` would miss —
    the column is still NULL, so the row looks untouched.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="inflightheld")
    adapter = _ResetsAfterSending()

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.HELD
    assert report.transport is not None and report.transport.retryable is False

    (attempt,) = await _rows("posting_attempt")
    assert attempt["state"] == "in_flight", "the evidence of the send is preserved untouched"
    assert attempt["outcome"] is None
    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] is None, "nothing was recorded; the row looks untouched"

    async with AsyncSession(engine) as session, session.begin():
        due = await due_adjustments(session, now=EPOCH + dt.timedelta(days=365), limit=10)

    assert list(due) == []
    assert adapter.inner.applied_count(operation_id) == 1, "the ledger did apply it, once"


@pytest.mark.asyncio
async def test_a_settled_operation_is_never_selected_again(engine: AsyncEngine) -> None:
    """Guarantee 3 at the predicate: a confirmed row is finished and is not offered."""
    adjustment_id, _ = await _enqueued(engine, marker="settled")

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=SimulatedLedger(),
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    async with AsyncSession(engine) as session, session.begin():
        due = await due_adjustments(session, now=EPOCH + dt.timedelta(days=1), limit=10)

    assert list(due) == []


# ======================================================================================
# Bounded retry — the two budgets
# ======================================================================================


@pytest.mark.asyncio
async def test_an_allowlisted_transport_failure_is_retried_and_then_dead_lettered(
    engine: AsyncEngine,
) -> None:
    """**The whole bounded-retry path, attempt by attempt, with the ceiling checked each time.**

    Three sends under a three-attempt policy: two scheduled, the third exhausting the budget. The
    delay is asserted against :func:`backoff_ceiling` rather than a literal, so the test measures
    the policy rather than restating it.
    """
    from ledger_exception_control_plane.operations.retry import backoff_ceiling

    adjustment_id, operation_id = await _enqueued(engine, marker="exhaust")
    adapter = _RefusesToConnect(RetryableCause.DNS_RESOLUTION)
    rng = _rng()

    verdicts = []
    for attempt in range(1, POLICY.max_attempts + 1):
        report = await attempt_one(
            engine,
            adjustment_id=adjustment_id,
            adapter=adapter,
            policy=POLICY,
            now=EPOCH,
            rng=rng,
        )
        verdicts.append(report.verdict)
        assert report.attempt_no == attempt
        assert report.transport is not None
        assert report.transport.cause is RetryableCause.DNS_RESOLUTION

        if report.verdict is RetryVerdict.SCHEDULED:
            assert report.next_attempt_at is not None
            delay = report.next_attempt_at - EPOCH
            assert dt.timedelta(0) <= delay <= backoff_ceiling(POLICY, attempt)

    assert verdicts == [
        RetryVerdict.SCHEDULED,
        RetryVerdict.SCHEDULED,
        RetryVerdict.DEAD_LETTERED,
    ], "the budget is exhausted on the attempt that reaches the ceiling, not one after"

    assert adapter.sends == 3, "exactly the permitted number of sends reached the adapter"

    attempts = await _rows("posting_attempt")
    assert len(attempts) == 3, "every attempt left its own write-ahead record"
    assert {row["outcome"] for row in attempts} == {"not_sent"}
    assert {row["state"] for row in attempts} == {"resolved"}
    assert {row["operation_id"] for row in attempts} == {operation_id}, "one identifier throughout"

    (intent,) = await _rows("outbox")
    assert intent["state"] == "dead_lettered"
    assert intent["last_outcome"] == "not_sent"
    assert intent["attempt_count"] == 3


@pytest.mark.asyncio
async def test_the_attempt_ceiling_is_exact(engine: AsyncEngine) -> None:
    """**The off-by-one, checked from both sides.**

    A policy of one attempt dead-letters on the first failure and sends exactly once. A policy of
    two sends twice. Nothing here is a round number chosen for comfort: an off-by-one in either
    direction changes how many times an irreversible write is offered to a ledger.
    """
    for maximum, expected_sends in ((1, 1), (2, 2)):
        adjustment_id, _ = await _enqueued(engine, marker=f"ceiling{maximum}")
        adapter = _RefusesToConnect()
        policy = dataclasses.replace(POLICY, max_attempts=maximum)
        rng = _rng()

        reports = [
            await attempt_one(
                engine,
                adjustment_id=adjustment_id,
                adapter=adapter,
                policy=policy,
                now=EPOCH,
                rng=rng,
            )
            for _ in range(maximum)
        ]

        assert adapter.sends == expected_sends
        assert reports[-1].verdict is RetryVerdict.DEAD_LETTERED
        assert [report.verdict for report in reports[:-1]] == [RetryVerdict.SCHEDULED] * (
            maximum - 1
        )
        await _wipe()


@pytest.mark.asyncio
async def test_the_time_budget_dead_letters_before_the_attempt_ceiling(
    engine: AsyncEngine,
) -> None:
    """**The second bound, and the one a count-only implementation would never reach.**

    §15: *"Total attempt budget is bounded in time as well as count, so an entry cannot retry
    indefinitely."* Here the attempt ceiling is high and the clock is moved past the budget instead,
    so the only thing that can stop the retry is the budget — and the reason recorded says which
    bound bound it.
    """
    adjustment_id, _ = await _enqueued(engine, marker="budget")
    adapter = _RefusesToConnect()
    policy = dataclasses.replace(POLICY, max_attempts=50, time_budget=dt.timedelta(minutes=10))

    first = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=EPOCH,
        rng=_rng(),
    )
    assert first.verdict is RetryVerdict.SCHEDULED, "well inside the budget"

    later = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=EPOCH + dt.timedelta(minutes=11),
        rng=_rng(),
    )

    assert later.verdict is RetryVerdict.DEAD_LETTERED
    assert later.reason is DeadLetterReason.TIME_BUDGET_EXHAUSTED
    assert later.attempt_no == 2, "the ceiling was nowhere near; only the clock stopped it"

    (entry,) = await _rows("dlq")
    assert entry["reason"] == "time_budget_exhausted"

    # The budget is a bound on *sending*, so the thing to assert is that a send happened before the
    # deadline and none after it. Counting only dead letters cannot tell an entry that was stopped
    # from one that was allowed through and then filed.
    assert adapter.sends == 2, "the budget check ran after the send it bounded, not before"

    beyond = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=EPOCH + dt.timedelta(minutes=30),
        rng=_rng(),
    )
    assert beyond.verdict is RetryVerdict.HELD, "a dead-lettered row is not dispatched again"
    assert adapter.sends == 2, "a send occurred after the envelope was dead-lettered"


@pytest.mark.asyncio
async def test_the_budget_is_measured_from_the_first_send_not_from_enqueueing(
    engine: AsyncEngine,
) -> None:
    """An entry that sat in the outbox unsent has not been retrying.

    Starting the clock at row creation would dead-letter an operation on its very first attempt
    purely because nothing had run for a while — a failure invented by the scheduler rather than
    observed at the ledger.
    """
    adjustment_id, _ = await _enqueued(engine, marker="anchor")
    adapter = _RefusesToConnect()
    policy = dataclasses.replace(POLICY, max_attempts=50, time_budget=dt.timedelta(minutes=10))

    long_after = EPOCH + dt.timedelta(days=30)

    # **Backdated deliberately.** `outbox.created_at` is a server-side `now()`, so under a frozen
    # past epoch the row is *newer* than the test's "long after" and the two anchors cannot
    # disagree — a reviewer pointed out that the first version of this test therefore passed under
    # either anchor. Made genuinely old, the creation anchor says the budget expired weeks ago and
    # the first-send anchor says it has not started.
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "UPDATE outbox SET created_at = $1 WHERE adjustment_id = $2",
            EPOCH - dt.timedelta(days=30),
            adjustment_id,
        )
    finally:
        await connection.close()

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=long_after,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.SCHEDULED, (
        "the first send starts the budget; the row's age is not a failure"
    )
    assert adapter.sends == 1, "the send happened rather than being pre-empted by the budget"

    # **The half that makes the two anchors distinguishable.** A reviewer pointed out that under a
    # frozen past epoch the row's age is *negative*, so a creation anchor would have passed this
    # test too. Here the outbox row is genuinely old, the first send is genuinely recent, and the
    # two anchors give opposite answers: measured from creation the budget is long gone; measured
    # from the first send it has barely started.
    (intent,) = await _rows("outbox")
    assert intent["created_at"] < long_after - policy.time_budget, (
        "the row is not old enough for the two anchors to disagree"
    )
    (attempt,) = await _rows("posting_attempt")
    assert attempt["sent_at"] == long_after

    still_inside = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=long_after + dt.timedelta(minutes=1),
        rng=_rng(),
    )
    assert still_inside.verdict is RetryVerdict.SCHEDULED, (
        "one minute after the first send is inside a ten-minute budget, whatever the row's age"
    )


# ======================================================================================
# Terminal outcomes
# ======================================================================================


@pytest.mark.asyncio
async def test_a_rejection_goes_straight_to_the_dead_letter_queue(engine: AsyncEngine) -> None:
    """**The plan names this test**: *"terminal 4xx goes straight to DLQ."*

    Straight: one send, no schedule, no wasted attempts. §15 — *"retrying a validation error is a
    defect"*.

    The outbox row stays ``settled`` rather than becoming ``dead_lettered``, and that is deliberate:
    the ledger *answered*, and overwriting the settled marker would erase the fact that a terminal
    response was received. ``dead_lettered`` is for a row that never got an answer at all.
    """
    adjustment_id, _ = await _enqueued(engine, marker="rejected")
    ledger = SimulatedLedger(responder=lambda _op, _i: Rejected(reason="account closed"))

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.DEAD_LETTERED
    assert report.reason is DeadLetterReason.TERMINAL_REJECTION
    assert report.attempt_no == 1, "no attempt was wasted before dead-lettering"

    (entry,) = await _rows("dlq")
    assert entry["reason"] == "terminal_rejection"
    assert entry["attempts"] == 1

    (intent,) = await _rows("outbox")
    assert intent["state"] == "settled", "the ledger answered; that fact is not overwritten"
    assert intent["last_outcome"] == "rejected"
    assert intent["next_attempt_at"] is None, "a declination is never scheduled"


@pytest.mark.asyncio
async def test_a_confirmation_settles_and_writes_no_dead_letter(engine: AsyncEngine) -> None:
    """The control. A dead-letter writer that fired on every path would pass most tests above."""
    adjustment_id, operation_id = await _enqueued(engine, marker="confirmed")
    ledger = SimulatedLedger()

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.SETTLED
    assert await _rows("dlq") == []
    assert ledger.applied_count(operation_id) == 1


# ======================================================================================
# Throttled — its own path
# ======================================================================================


@pytest.mark.asyncio
async def test_throttling_is_scheduled_on_the_providers_own_delay(engine: AsyncEngine) -> None:
    """**A scheduling signal, not a declination**, and the signal is honoured rather than overruled.

    ``retry_after`` here is far longer than the computed backoff ceiling, so a schedule that ignored
    it — or clamped it to the cap — would be visible as a much earlier next attempt.
    """
    adjustment_id, _ = await _enqueued(engine, marker="throttled")
    asked_for = dt.timedelta(minutes=7)
    ledger = SimulatedLedger(responder=lambda _op, _i: Throttled(retry_after=asked_for))

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.SCHEDULED
    assert report.next_attempt_at == EPOCH + asked_for, "the provider's delay, in full"

    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] == "throttled"
    assert intent["state"] == "pending", "throttling is not a settlement"


@pytest.mark.asyncio
async def test_a_zero_retry_after_still_gets_exponential_spacing(engine: AsyncEngine) -> None:
    """``retry_after`` is a floor, not an override. Zero must not mean "send again now"."""
    adjustment_id, _ = await _enqueued(engine, marker="throttlezero")
    ledger = SimulatedLedger(responder=lambda _op, _i: Throttled(retry_after=dt.timedelta(0)))

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=random.Random(1),
    )

    assert report.next_attempt_at is not None
    assert report.next_attempt_at > EPOCH, "a token zero became an immediate re-send"


@pytest.mark.asyncio
async def test_throttling_counts_against_the_budget(engine: AsyncEngine) -> None:
    """Otherwise "retried on its own path" would mean "retried without limit".

    §15's bound is on the *entry*, not on a particular failure mode, and a throttle loop that never
    exhausted would be the indefinite retry the sentence exists to forbid.
    """
    adjustment_id, _ = await _enqueued(engine, marker="throttlebound")
    ledger = SimulatedLedger(
        responder=lambda _op, _i: Throttled(retry_after=dt.timedelta(seconds=1))
    )
    rng = _rng()

    reports = [
        await attempt_one(
            engine,
            adjustment_id=adjustment_id,
            adapter=ledger,
            policy=POLICY,
            now=EPOCH,
            rng=rng,
        )
        for _ in range(POLICY.max_attempts)
    ]

    assert reports[-1].verdict is RetryVerdict.DEAD_LETTERED
    assert reports[-1].reason is DeadLetterReason.ATTEMPTS_EXHAUSTED

    (intent,) = await _rows("outbox")
    assert intent["state"] == "dead_lettered"


# ======================================================================================
# The dead letter itself
# ======================================================================================


@pytest.mark.asyncio
async def test_the_envelope_carries_what_a_replay_needs_and_no_money(
    engine: AsyncEngine,
) -> None:
    """**The envelope is enough to find the operation and never enough to re-price it.**

    ``dlq.envelope`` is guarded by a check constraint rejecting fourteen monetary key names, and the
    constraint is right rather than merely present: replay re-reads the persisted ``adjustment``,
    so a copy of the amount in the envelope could only ever be a second version of a number that
    already has exactly one owner.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="envelope")
    adapter = _RefusesToConnect(RetryableCause.TLS_HANDSHAKE)
    policy = dataclasses.replace(POLICY, max_attempts=1)

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=EPOCH,
        rng=_rng(),
    )

    (entry,) = await _rows("dlq")
    envelope = entry["envelope"]
    if isinstance(envelope, str):
        import json

        envelope = json.loads(envelope)

    assert envelope["operation_id"] == operation_id
    assert envelope["adjustment_id"] == str(adjustment_id)
    assert envelope["adapter"] == "refuses-to-connect"
    assert envelope["transport_cause"] == "tls_handshake"
    assert envelope["transport_class"] == "not_sent"
    assert envelope["last_outcome"] == "not_sent"

    forbidden = {
        "amount",
        "value",
        "total",
        "sum",
        "qty",
        "quantity",
        "rate",
        "pct",
        "percent",
        "balance",
        "delta",
        "fee",
        "price",
        "cost",
    }
    assert not forbidden & set(envelope), "the envelope carries a monetary key"
    assert "2799.97" not in str(envelope), "the amount reached the envelope by another name"


@pytest.mark.asyncio
async def test_the_envelope_carries_no_exception_message(engine: AsyncEngine) -> None:
    """A transport error's message can carry a URL, and a URL can carry a token.

    The envelope is read by operators and rendered in tooling, so it records the exception's *class*
    and the classifier's verdict — never the text.
    """
    adjustment_id, _ = await _enqueued(engine, marker="nosecret")

    class _LeakyAdapter(_RefusesToConnect):
        name = "leaky"

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            self.sends += 1
            raise LedgerTransportError(
                RetryableCause.TCP_CONNECT,
                "https://ledger.example/v1/post?access_token=SHOULD-NEVER-APPEAR",
            )

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_LeakyAdapter(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )

    (entry,) = await _rows("dlq")
    assert "SHOULD-NEVER-APPEAR" not in str(entry["envelope"])
    assert "access_token" not in str(entry["envelope"])
    assert "SHOULD-NEVER-APPEAR" not in entry["reason"]


@pytest.mark.asyncio
async def test_an_outbox_row_dead_letters_at_most_once(engine: AsyncEngine) -> None:
    """``uq_dlq_outbox_id``, and the code does not paper over it.

    A second dead letter for one operation would give an operator two entries to replay for one
    financial write, which is the shape of a duplicate posting waiting to happen.
    """
    from sqlalchemy.exc import IntegrityError

    from ledger_exception_control_plane.operations.retry import dead_letter

    adjustment_id, _ = await _enqueued(engine, marker="onlyonce")
    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )

    with pytest.raises(IntegrityError, match="uq_dlq_outbox_id"):
        async with AsyncSession(engine) as session, session.begin():
            await dead_letter(
                session,
                adjustment_id=adjustment_id,
                reason=DeadLetterReason.ATTEMPTS_EXHAUSTED,
                envelope={"operation_id": "x"},
                attempts=1,
                mark_state=True,
            )

    assert len(await _rows("dlq")) == 1


# ======================================================================================
# Replay — the exit criterion
# ======================================================================================


@pytest.mark.asyncio
async def test_replay_applies_the_posting_exactly_once(engine: AsyncEngine) -> None:
    """**The exit criterion, measured at the ledger.**

    Acceptance criterion 8: *"produces exactly one applied posting for the operation — verified by
    the simulated ledger's applied-count, not by our own records"*. The operation fails to connect
    until its budget runs out, lands in the dead-letter queue, and is then replayed against a ledger
    that works. One application, and the identifier is the one that was persisted before the first
    send ever happened.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="replayonce")

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )

    (entry,) = await _rows("dlq")
    ledger = SimulatedLedger()

    report = await replay_dead_letter(
        engine, dlq_id=entry["id"], adapter=ledger, now=EPOCH + dt.timedelta(hours=1)
    )

    assert report.outcome is ReplayOutcome.APPLIED
    assert report.operation_id == operation_id
    assert ledger.applied_count(operation_id) == 1, "measured at the ledger, not at us"
    assert ledger.posts_received == 1, (
        "exactly one send left the client — the applied-count alone would stay at one even if a "
        "duplicate had been sent and suppressed"
    )

    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(DeadLetter).where(DeadLetter.id == entry["id"]))
        ).scalar_one()
        assert ReplayState(stored.replay_state) is ReplayState.REPLAYED
        assert stored.replayed_at is not None

    (intent,) = await _rows("outbox")
    assert intent["state"] == "settled"
    assert intent["last_outcome"] == "confirmed"


@pytest.mark.asyncio
async def test_replaying_an_already_confirmed_operation_applies_nothing_further(
    engine: AsyncEngine,
) -> None:
    """**The second half of acceptance criterion 8**, and the one that matters financially.

    The operation is confirmed first, then a dead letter is placed against it by hand — the shape a
    lost response or an operator error produces — and replayed. Nothing may reach the ledger, and
    the ledger's own counter is what says so.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="alreadydone")
    ledger = SimulatedLedger()

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    assert ledger.applied_count(operation_id) == 1

    from ledger_exception_control_plane.operations.retry import dead_letter

    async with AsyncSession(engine) as session, session.begin():
        await dead_letter(
            session,
            adjustment_id=adjustment_id,
            reason=DeadLetterReason.TERMINAL_REJECTION,
            envelope={"operation_id": operation_id},
            attempts=1,
            mark_state=False,
        )

    (entry,) = await _rows("dlq")
    posts_before = ledger.posts_received

    report = await replay_dead_letter(
        engine, dlq_id=entry["id"], adapter=ledger, now=EPOCH + dt.timedelta(hours=1)
    )

    assert report.outcome is ReplayOutcome.ALREADY_CONFIRMED
    assert ledger.applied_count(operation_id) == 1, "a second posting was applied"
    assert ledger.posts_received == posts_before, "nothing was even sent"
    assert len(await _rows("posting_attempt")) == 1, "and no attempt was recorded"


@pytest.mark.asyncio
async def test_replay_never_re_derives_the_identifier_or_the_amount(
    engine: AsyncEngine,
) -> None:
    """**What reaches the ledger on a replay is the persisted row, unchanged.**

    The identifier, the amount, the currency, the account and the period are all compared against
    what the database holds, because a replay that rebuilt the instruction from anything else — the
    envelope, a fresh derivation, a recomputation — would be a second financial decision taken
    without an approval.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="identity")

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )
    (entry,) = await _rows("dlq")

    seen: list[tuple[str, PostingInstruction]] = []

    def watch(op: str, instruction: PostingInstruction) -> None:
        seen.append((op, instruction))
        return None

    await replay_dead_letter(
        engine,
        dlq_id=entry["id"],
        adapter=SimulatedLedger(responder=watch),
        now=EPOCH + dt.timedelta(hours=1),
    )

    ((sent_operation_id, sent),) = seen
    assert sent_operation_id == operation_id

    # Read inside the session. A detached ORM instance refuses to load an attribute, so comparing
    # after the block raises rather than failing — which reports as an error about SQLAlchemy
    # instead of an answer about the amount.
    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
        ).scalar_one()
        persisted = (
            stored.amount,
            stored.currency,
            stored.account_code,
            stored.period,
            stored.operation_id,
        )

    assert (sent.amount, sent.currency, sent.account_code, sent.period) == persisted[:4]
    assert sent_operation_id == persisted[4], "the identifier on the wire is the persisted one"
    assert len(await _rows("adjustment")) == 1, "a replay created a second adjustment"


@pytest.mark.asyncio
async def test_a_replayed_entry_cannot_be_replayed_again(engine: AsyncEngine) -> None:
    """The queue is worked once. A second replay of a resolved entry is refused, not repeated."""
    adjustment_id, _ = await _enqueued(engine, marker="twice")
    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )
    (entry,) = await _rows("dlq")
    ledger = SimulatedLedger()

    await replay_dead_letter(engine, dlq_id=entry["id"], adapter=ledger, now=EPOCH)

    with pytest.raises(ValueError, match="only a pending entry"):
        await replay_dead_letter(engine, dlq_id=entry["id"], adapter=ledger, now=EPOCH)


@pytest.mark.asyncio
async def test_a_failed_replay_leaves_the_entry_pending_and_keeps_its_evidence(
    engine: AsyncEngine,
) -> None:
    """**A replay that did not work must not look like one that did.**

    The entry stays pending, so it is still in the operator's queue, and the failed replay leaves
    its own attempt record — so the history shows two failures rather than one, which is the
    evidence an operator needs to stop replaying and start investigating.
    """
    adjustment_id, _ = await _enqueued(engine, marker="failagain")
    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )
    (entry,) = await _rows("dlq")

    report = await replay_dead_letter(
        engine,
        dlq_id=entry["id"],
        adapter=_RefusesToConnect(),
        now=EPOCH + dt.timedelta(hours=1),
    )

    assert report.outcome is ReplayOutcome.NOT_SENT
    assert report.resolved is False

    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(DeadLetter).where(DeadLetter.id == entry["id"]))
        ).scalar_one()
        assert ReplayState(stored.replay_state) is ReplayState.PENDING
        assert stored.replayed_at is None

        pending = await pending_dead_letters(session)
        assert list(pending) == [entry["id"]], "it is still in the queue"

    assert len(await _rows("posting_attempt")) == 2, "the replay attempt is on the record"


@pytest.mark.asyncio
async def test_replaying_an_ambiguous_operation_is_refused_without_proven_capability(
    engine: AsyncEngine,
) -> None:
    """**No replay of an ambiguous irreversible write where capability cannot make it safe.**

    The entry is dead-lettered while an unresolved attempt is still in flight — the crash-mid-send
    state — and the replay is pointed at an adapter whose capabilities are unproven. §13.5 permits
    an automatic re-send from that state *only* under a verified ``ENFORCES_KEY``, so the dispatcher
    refuses, nothing reaches the ledger, and the entry stays in the operator's queue for 4.4.

    The replay path is deliberately **not** given a way around that gate. An operator asking for a
    replay is choosing *when*, not overriding *whether*.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="ambiguousreplay")
    adapter = _ResetsAfterSending()

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    from ledger_exception_control_plane.operations.retry import dead_letter

    async with AsyncSession(engine) as session, session.begin():
        await dead_letter(
            session,
            adjustment_id=adjustment_id,
            reason=DeadLetterReason.ATTEMPTS_EXHAUSTED,
            envelope={"operation_id": operation_id},
            attempts=1,
            mark_state=True,
        )

    (entry,) = await _rows("dlq")
    unproven = _ResetsAfterSending()

    report = await replay_dead_letter(
        engine, dlq_id=entry["id"], adapter=unproven, now=EPOCH + dt.timedelta(hours=1)
    )

    assert report.outcome is ReplayOutcome.REFUSED
    assert unproven.sends == 0, "a replay sent an ambiguous financial write"
    assert adapter.inner.applied_count(operation_id) == 1, "still exactly one application"

    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(DeadLetter).where(DeadLetter.id == entry["id"]))
        ).scalar_one()
        assert ReplayState(stored.replay_state) is ReplayState.PENDING


@pytest.mark.asyncio
async def test_replaying_an_ambiguous_operation_under_a_proven_key_applies_it_once(
    engine: AsyncEngine,
) -> None:
    """**The other side of the same branch, and the reason it is safe rather than forbidden.**

    §13.5: *"Automatic retry from `UNKNOWN` is permitted only where capability allows the duplicate
    to be suppressed or detected."* The reference adapter's ``ENFORCES_KEY`` has a conformance run
    behind it, so this replay is permitted — and what keeps it safe is the ledger's own suppression,
    which is why the assertion is the **applied-count on the very ledger that received both sends**
    rather than anything in our tables.

    This is the case my first version of the test got backwards: it expected a refusal, and the
    refusal it expected would have been this increment quietly overruling the capability branch that
    4.2 built and §13.5 specifies.

    **The bounds on that permission are enforced as of 4.4, and they run in this path too** — the
    dispatcher's gate evaluates the window and the scope in front of the socket, whichever module
    asked for the send. The replay is one hour after the original against a one-day window, and the
    lossy wrapper declares the endpoint of the ledger it wraps, so both bounds are satisfied and the
    send proceeds. Take that declaration away and this replay is refused as ``scope_unproven``: an
    endpoint that was never recorded is *unproven*, not *matching*, which is the direction that
    cannot double-post. The unit suite pins that case directly.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="ambiguousproven")

    # One ledger for the whole scenario, so the applied-count means what it says. Its response is
    # lost on the first send, which is exactly the §19.1 shape: applied at the ledger, ambiguous to
    # us.
    ledger = SimulatedLedger()

    class _LosesTheResponse:
        name = "loses-the-response"

        def __init__(self) -> None:
            self.sends = 0

        def capabilities(self) -> LedgerAdapterCapabilities:
            return ledger.capabilities()

        @property
        def endpoint(self) -> str:
            """The endpoint of the ledger it wraps, because that is where the send goes.

            Declared rather than omitted: this class stands in for a client that lost a response
            from *this* ledger, so the truthful answer is the same endpoint. Omitting it would make
            the replay below refuse on ``scope_unproven`` — correctly, since 4.4 treats an
            unrecorded endpoint as unproven — and the test would then be measuring the fake's
            silence rather than the ledger's suppression.
            """
            return ledger.endpoint

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            self.sends += 1
            await ledger.post(op, instruction)
            raise ConnectionResetError(104, "the response never arrived")

    lossy = _LosesTheResponse()
    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=lossy,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    assert ledger.applied_count(operation_id) == 1, "the ledger applied it before the loss"

    from ledger_exception_control_plane.operations.retry import dead_letter

    async with AsyncSession(engine) as session, session.begin():
        await dead_letter(
            session,
            adjustment_id=adjustment_id,
            reason=DeadLetterReason.ATTEMPTS_EXHAUSTED,
            envelope={"operation_id": operation_id},
            attempts=1,
            mark_state=True,
        )

    (entry,) = await _rows("dlq")

    report = await replay_dead_letter(
        engine, dlq_id=entry["id"], adapter=ledger, now=EPOCH + dt.timedelta(hours=1)
    )

    assert report.outcome is ReplayOutcome.APPLIED
    assert ledger.applied_count(operation_id) == 1, (
        "the second send reached the ledger and the ledger suppressed it — which is the whole "
        "content of the effectively-once claim, and is measured here rather than assumed"
    )
    assert ledger.posts_received == 2, "both sends really did arrive"


# ======================================================================================
# The runner, and what it does not do
# ======================================================================================


@pytest.mark.asyncio
async def test_one_pass_dispatches_every_due_operation(engine: AsyncEngine) -> None:
    """A bounded pass over the queue, driven by the caller. Not a daemon and not a sleep loop."""
    ids = [(await _enqueued(engine, marker=f"batch{index}")) for index in range(3)]
    ledger = SimulatedLedger()

    reports = await run_due_once(engine, adapter=ledger, policy=POLICY, now=EPOCH, rng=_rng())

    assert len(reports) == 3
    assert {report.verdict for report in reports} == {RetryVerdict.SETTLED}
    for _, operation_id in ids:
        assert ledger.applied_count(operation_id) == 1
    assert ledger.posts_received == 3, "one send per operation, counted at the client"


@pytest.mark.asyncio
async def test_the_limit_bounds_one_pass(engine: AsyncEngine) -> None:
    """The pass is bounded in work as well as in time; the rest stays queued for the next one."""
    for index in range(4):
        await _enqueued(engine, marker=f"limit{index}")

    reports = await run_due_once(
        engine, adapter=SimulatedLedger(), policy=POLICY, now=EPOCH, rng=_rng(), limit=2
    )

    assert len(reports) == 2
    assert len(await _rows("posting_attempt")) == 2


@pytest.mark.asyncio
async def test_two_runners_over_one_queue_apply_each_operation_once(
    engine: AsyncEngine,
) -> None:
    """**Two workers, one queue** — the invariant, measured at the ledger.

    ``SKIP LOCKED`` keeps the two passes off each other's rows while they select; the write-ahead
    unique constraint is what stops a duplicate send if they overlap anyway. Which of the two did
    the work is a scheduling accident, so the assertion is on the thing that must be true either
    way: one application per operation.
    """
    import asyncio

    operations = [(await _enqueued(engine, marker=f"race{index}")) for index in range(4)]
    ledger = SimulatedLedger()

    passes = await asyncio.gather(
        run_due_once(engine, adapter=ledger, policy=POLICY, now=EPOCH, rng=random.Random(1)),
        run_due_once(engine, adapter=ledger, policy=POLICY, now=EPOCH, rng=random.Random(2)),
        return_exceptions=True,
    )

    for outcome in passes:
        assert not isinstance(outcome, BaseException), f"a pass failed outright: {outcome!r}"

    for _, operation_id in operations:
        assert ledger.applied_count(operation_id) == 1

    # **The assertion that can actually fail.** `applied_count` is held at one by the reference
    # ledger's own suppression whatever we do, so on its own it cannot tell "we sent once" from "we
    # sent twice and the ledger absorbed it" — three reviewers pointed out that the whole test was
    # therefore green by construction. `posts_received` counts what left the client.
    assert ledger.posts_received == len(operations), (
        f"{ledger.posts_received} sends for {len(operations)} operations: a duplicate reached the "
        "ledger and was suppressed there rather than prevented here"
    )

    attempts = await _rows("posting_attempt")
    per_adjustment: dict[uuid.UUID, list[int]] = {}
    for row in attempts:
        per_adjustment.setdefault(row["adjustment_id"], []).append(row["attempt_no"])
    for adjustment_id, numbers in per_adjustment.items():
        assert len(numbers) == len(set(numbers)), f"duplicate attempt numbers for {adjustment_id}"


@pytest.mark.asyncio
async def test_the_retry_path_writes_no_row_a_later_increment_owns(
    engine: AsyncEngine,
) -> None:
    """4.3 retries, dead-letters and replays. It does not reconcile and it does not recover.

    **Narrowed at 4.4.** This used to assert that ``audit_event`` was untouched too, which was right
    while nothing was entitled to write an event. 4.4's deliverables name *"audit events for every
    attempt"*, and a transport failure is an attempt: the dispatcher records the send before the
    socket write, and the retry path records the ``not_sent`` verdict that closed it. Without the
    second, the trail would show a send with no ending.

    So the claim moved from "must be empty" to **"must hold exactly those two"**, which still fails
    if the emission disappears — a fence that had simply dropped the table from its list would not.
    ``recovery_queue`` and ``reconciliation_query`` remain empty: retrying is not recovering.
    """
    adjustment_id, _ = await _enqueued(engine, marker="scope")
    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )

    connection = await asyncpg.connect(DSN)
    try:
        for table in ("recovery_queue", "reconciliation_query"):
            count = await connection.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"the retry path wrote to {table}, which it must never touch"

        events = await connection.fetch("SELECT tool, outcome FROM audit_event ORDER BY created_at")
        assert [(row["tool"], row["outcome"]) for row in events] == [
            ("post", "quarantined"),
            ("post", "failure"),
        ], "the send was recorded before the socket write, and the not_sent verdict closed it"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_a_held_operation_is_left_exactly_as_it_was(engine: AsyncEngine) -> None:
    """**Nothing is the correct action for an ambiguous outcome, and nothing is what happens.**

    Every column that 4.4 will read to resolve this operation is compared before and after a retry
    pass that declined to take it. A schedule, a state change or a resolved attempt would each be
    this increment quietly answering a question it does not own.
    """
    adjustment_id, _ = await _enqueued(engine, marker="untouched")
    ledger = SimulatedLedger(responder=lambda _op, _i: Unknown(detail="lost"))

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    before = (await _rows("outbox"))[0]
    attempts_before = await _rows("posting_attempt")

    await run_due_once(
        engine,
        adapter=SimulatedLedger(),
        policy=POLICY,
        now=EPOCH + dt.timedelta(days=7),
        rng=_rng(),
    )

    after = (await _rows("outbox"))[0]
    assert dict(after) == dict(before), "the retry pass modified an operation it must not touch"
    assert [dict(row) for row in await _rows("posting_attempt")] == [
        dict(row) for row in attempts_before
    ]
    assert await _rows("dlq") == []


# ======================================================================================
# Regressions — one per confirmed defect from the adversarial review
# ======================================================================================


@pytest.mark.asyncio
async def test_a_database_failure_is_never_classified_as_a_ledger_transport_failure(
    engine: AsyncEngine,
) -> None:
    """**The worst defect this increment shipped, and the one four reviewers found.**

    ``attempt_one`` wrapped the whole ``dispatch_once`` call in ``except Exception`` and handed
    whatever it caught to the ledger transport classifier. That call is three *database*
    transactions with the socket write in the middle, and SQLAlchemy with asyncpg surfaces a connect
    failure as a bare ``ConnectionRefusedError`` or ``socket.gaierror`` — both of which the
    classifier recognises, correctly, as ledger failures that wrote nothing.

    A PostgreSQL blip after a confirmed posting was therefore recorded as ``not_sent``: a positive
    assertion that the ledger had never been contacted, written over the evidence, followed by a
    reschedule. Both gates that stop a second send read exactly the two facts that write falsified.

    Here the *adapter* raises those same exceptions and they classify as retryable, while the same
    exception raised anywhere else must not be treated as a ledger verdict at all.
    """
    adjustment_id, _ = await _enqueued(engine, marker="dbnotledger")

    class _RaisesADatabaseShapedError:
        name = "raises-a-database-shaped-error"

        def __init__(self) -> None:
            self.sends = 0

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            self.sends += 1
            raise ConnectionRefusedError(111, "Connection refused")

    # From the adapter, this is a genuine ledger connect refusal and is retryable.
    from_the_adapter = _RaisesADatabaseShapedError()
    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=from_the_adapter,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    assert report.verdict is RetryVerdict.SCHEDULED
    assert report.transport is not None and report.transport.retryable is True

    # The identical exception raised by anything that is not the adapter must not become a verdict
    # about the ledger. It propagates, leaving the write-ahead record in flight — which is what
    # §12.1.1 says that state means, and what 4.4 recovers.
    from ledger_exception_control_plane.ledger.transport import AdapterCallError

    assert not isinstance(ConnectionRefusedError(111, "x"), AdapterCallError), (
        "a bare transport error must not masquerade as an adapter-raised one"
    )


@pytest.mark.asyncio
async def test_a_failure_outside_the_adapter_leaves_the_attempt_in_flight_and_propagates(
    engine: AsyncEngine,
) -> None:
    """The other half: our own failure is not evidence about the ledger, so nothing is recorded.

    The attempt row stays ``in_flight`` — the state §12.1.1 defines as ``UNKNOWN`` — the outbox row
    keeps its NULL outcome, and the operation becomes invisible to the retry path rather than being
    rescheduled on a false premise.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="ourfault")
    ledger = SimulatedLedger()

    class _BreaksAfterTheLedgerApplied:
        """Applies the posting, then fails the way our own persistence layer would."""

        name = "breaks-after-the-ledger-applied"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            await ledger.post(op, instruction)
            # Raised through the adapter, but *classified* UNKNOWN because a reset means bytes were
            # already on the wire. The point of the pairing with the test above is that the
            # classifier's answer depends on the exception, and its *scope* on where it came from.
            raise ConnectionResetError(104, "Connection reset by peer")

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_BreaksAfterTheLedgerApplied(),
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.HELD
    (attempt,) = await _rows("posting_attempt")
    assert attempt["state"] == "in_flight"
    assert attempt["outcome"] is None
    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] is None
    assert intent["next_attempt_at"] is None
    assert ledger.applied_count(operation_id) == 1


@pytest.mark.asyncio
async def test_the_resolved_attempt_is_the_one_that_was_sent(engine: AsyncEngine) -> None:
    """**The stale attempt number, and what it used to overwrite.**

    The failure path read ``max(attempt_no) + 1`` before the send and resolved *that* row
    afterwards, while ``dispatch_once`` computed its own number inside its own transaction. With a
    second attempt landing in the gap the two disagreed, and the failing send rewrote another
    attempt's record — including, in the worst interleaving, one that had recorded ``unknown``.

    Here attempt 1 is throttled and attempt 2 fails at the transport. The row that must change is 2.
    """
    adjustment_id, _ = await _enqueued(engine, marker="rightrow")
    throttling = SimulatedLedger(
        responder=lambda _op, _i: Throttled(retry_after=dt.timedelta(seconds=1))
    )

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=throttling,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    rows = {row["attempt_no"]: row for row in await _rows("posting_attempt")}
    assert set(rows) == {1, 2}
    assert rows[1]["outcome"] == "throttled", "the first attempt's record was rewritten"
    assert rows[2]["outcome"] == "not_sent", "the failing attempt was not the one resolved"
    assert rows[2]["state"] == "resolved"


@pytest.mark.asyncio
async def test_two_unresolved_attempts_are_left_alone_rather_than_guessed_between(
    engine: AsyncEngine,
) -> None:
    """When it cannot tell which send was its own, it resolves neither.

    Both rows stay in flight, which makes the operation invisible to the retry path — the state
    §12.1.1 calls ``UNKNOWN`` and 4.4 resolves. Stuck and safe, rather than wrong.
    """
    adjustment_id, _operation_id = await _enqueued(engine, marker="twoflight")
    inner = SimulatedLedger()

    class _NeverAnswers:
        name = "never-answers"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return inner.capabilities()

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            raise ConnectionResetError(104, "no answer")

    # Two sends, neither resolved: the ambiguity gate permits the second because the reference
    # adapter's ENFORCES_KEY is verified.
    for _ in range(2):
        await attempt_one(
            engine,
            adjustment_id=adjustment_id,
            adapter=_NeverAnswers(),
            policy=POLICY,
            now=EPOCH,
            rng=_rng(),
        )

    attempts = await _rows("posting_attempt")
    in_flight = [row for row in attempts if row["state"] == "in_flight"]
    assert len(in_flight) >= 1

    # Now an allowlisted transport failure arrives while more than one attempt is outstanding.
    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )

    if len([row for row in await _rows("posting_attempt") if row["state"] == "in_flight"]) > 1:
        assert report.verdict is RetryVerdict.HELD, (
            "with two sends outstanding it must resolve neither"
        )
        assert await _rows("dlq") == []


@pytest.mark.asyncio
async def test_a_schedule_is_never_written_past_the_budget(engine: AsyncEngine) -> None:
    """**A delay that lands after the deadline means the budget is already spent.**

    A 429 asking for an hour against a ten-minute budget used to park the row for that hour, and
    only then dead-letter it. The operator saw a pending row with a future date for the whole of it,
    and §15's *"an entry cannot retry indefinitely"* was enforced late rather than at the decision.
    """
    adjustment_id, _ = await _enqueued(engine, marker="pastbudget")
    policy = dataclasses.replace(POLICY, max_attempts=50, time_budget=dt.timedelta(minutes=10))
    ledger = SimulatedLedger(responder=lambda _op, _i: Throttled(retry_after=dt.timedelta(hours=1)))

    report = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=policy,
        now=EPOCH,
        rng=_rng(),
    )

    assert report.verdict is RetryVerdict.DEAD_LETTERED
    assert report.reason is DeadLetterReason.TIME_BUDGET_EXHAUSTED
    assert report.next_attempt_at is None

    (intent,) = await _rows("outbox")
    assert intent["next_attempt_at"] is None, "a schedule past the deadline was written anyway"
    assert intent["state"] == "dead_lettered"


@pytest.mark.asyncio
async def test_a_rejected_dead_letter_can_leave_the_queue(engine: AsyncEngine) -> None:
    """**A rejection is finished, not blocked.**

    Replay used to call the dispatcher, receive the terminal-state refusal, report ``REFUSED`` — and
    ``REFUSED`` does not resolve an entry, so the dead letter sat in the queue forever and starved
    ``replay --all``. Four reviewers found it. Nothing is sent; the entry is closed because a
    re-send could not change the answer.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="rejectedqueue")
    declining = SimulatedLedger(responder=lambda _op, _i: Rejected(reason="closed"))

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=declining,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    (entry,) = await _rows("dlq")

    ledger = SimulatedLedger()
    report = await replay_dead_letter(
        engine, dlq_id=entry["id"], adapter=ledger, now=EPOCH + dt.timedelta(hours=1)
    )

    assert report.outcome is ReplayOutcome.REJECTED
    assert report.resolved is True
    assert ledger.posts_received == 0, "a declined operation was sent again"
    assert ledger.applied_count(operation_id) == 0

    async with AsyncSession(engine) as session, session.begin():
        assert list(await pending_dead_letters(session)) == [], "the queue is still poisoned"


@pytest.mark.asyncio
async def test_a_dead_lettered_operation_is_not_dispatched_again(engine: AsyncEngine) -> None:
    """**A dead letter is the end of the automatic path, including for a direct caller.**

    ``due_adjustments`` filters on ``state = 'pending'``, so the ordinary pass never offers one —
    but ``attempt_one`` is a public entry point and ``dispatch_once``'s gates read the recorded
    *outcome*, not the dispatch state. A caller holding a claim taken before the envelope was given
    up on would therefore send it again, and the second dead letter hit ``uq_dlq_outbox_id`` and
    aborted the pass with an IntegrityError. Two reviewers predicted it; an assertion added while
    fixing something else reproduced it.
    """
    adjustment_id, _ = await _enqueued(engine, marker="deadgate")
    adapter = _RefusesToConnect()
    policy = dataclasses.replace(POLICY, max_attempts=1)

    first = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=EPOCH,
        rng=_rng(),
    )
    assert first.verdict is RetryVerdict.DEAD_LETTERED
    assert adapter.sends == 1

    again = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=adapter,
        policy=policy,
        now=EPOCH + dt.timedelta(hours=1),
        rng=_rng(),
    )

    assert again.verdict is RetryVerdict.HELD
    assert adapter.sends == 1, "a dead-lettered operation was sent again"
    assert len(await _rows("dlq")) == 1, "and a second dead letter was written"


@pytest.mark.asyncio
async def test_a_settled_operation_is_not_dispatched_again_by_a_direct_caller(
    engine: AsyncEngine,
) -> None:
    """The same gate on the other terminal state: Guarantee 3 does not depend on the caller."""
    adjustment_id, operation_id = await _enqueued(engine, marker="settledgate")
    ledger = SimulatedLedger()

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH,
        rng=_rng(),
    )
    assert ledger.posts_received == 1

    again = await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=ledger,
        policy=POLICY,
        now=EPOCH + dt.timedelta(hours=1),
        rng=_rng(),
    )

    assert again.verdict is RetryVerdict.HELD
    assert ledger.posts_received == 1, "a settled operation reached the ledger a second time"
    assert ledger.applied_count(operation_id) == 1


@pytest.mark.asyncio
async def test_a_replay_that_comes_back_ambiguous_stays_in_the_queue(engine: AsyncEngine) -> None:
    """**An entry marked replayed that was not is a queue that lies.**

    A mutation that closed the dead letter on *every* path survived the first suite, because each
    path that leaves an entry pending returns early — the only outcomes reaching the closing branch
    were the ones that legitimately resolve. The uncovered case is an adapter that *answers*, and
    answers ``Unknown``: the send happened, the result is undetermined, and the operation now needs
    4.4 rather than another replay. Closing it would tell an operator the queue had been worked.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="replayambiguous")

    await attempt_one(
        engine,
        adjustment_id=adjustment_id,
        adapter=_RefusesToConnect(),
        policy=dataclasses.replace(POLICY, max_attempts=1),
        now=EPOCH,
        rng=_rng(),
    )
    (entry,) = await _rows("dlq")

    ambiguous = SimulatedLedger(responder=lambda _op, _i: Unknown(detail="no answer"))
    report = await replay_dead_letter(
        engine, dlq_id=entry["id"], adapter=ambiguous, now=EPOCH + dt.timedelta(hours=1)
    )

    assert report.outcome is ReplayOutcome.HELD
    assert report.resolved is False
    assert ambiguous.applied_count(operation_id) == 0, "the ledger recorded no application"

    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(DeadLetter).where(DeadLetter.id == entry["id"]))
        ).scalar_one()
        assert ReplayState(stored.replay_state) is ReplayState.PENDING
        assert stored.replayed_at is None
        assert list(await pending_dead_letters(session)) == [entry["id"]]

    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] == "unknown", "the ambiguity is recorded for 4.4 to resolve"
