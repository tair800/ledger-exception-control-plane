"""M4.2 against real PostgreSQL — the transactional outbox and one dispatch, end to end.

Three of 4.2's obligations cannot be established without a database, and they are the three its exit
criteria name: dispatch works end to end, the dispatcher branches on declared capability, and a
crash between the socket write and the response write leaves a recoverable ``IN_FLIGHT`` record.
The plan's remaining two database obligations live here too — no outbox row without its state
change and none lost, and the identifier identical across attempts one and five.

**The crash criterion is a claim about a *record*, not about a recovery action.** The plan's verb is
"leaves": what 4.2 owes is the write-ahead row committed before the send, and evidence that it
survives with ``state = in_flight`` and no outcome. Acting on that row — re-sending under
``ENFORCES_KEY``, reconciling under ``BY_OPERATION_ID``, routing to manual recovery otherwise — is
§13.5's capability branch and belongs to 4.4. A test here that resolved an ``UNKNOWN`` would be that
increment arriving without its increment.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_dispatch_postgres.py -m integration
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import (
    Adjustment,
    AttemptState,
    PostingAttempt,
    TreatmentCode,
)
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ledger import (
    Confirmed,
    Found,
    IdempotencyMode,
    LedgerAdapterCapabilities,
    PartiallyApplied,
    PostingInstruction,
    PostingOutcome,
    PostingQueryMode,
    QueryOutcome,
    Rejected,
    SimulatedLedger,
    Throttled,
    Unknown,
    capabilities_for,
)
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from ledger_exception_control_plane.operations import (
    DispatchRefusedError,
    dispatch_once,
    enqueue_posting,
    reconciliation_is_available,
    record_operation,
)

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
REBOOK_ACCOUNT = "4100"


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
    """Deepest first: every one of these foreign keys is RESTRICT."""
    connection = await asyncpg.connect(DSN)
    try:
        for table in (
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
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, decided_at)"
            " VALUES ($1, $2, $3, 'approved', 'rebook', 'controller-a', $4)",
            approval_id,
            exception_id,
            resolution_version,
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
    """Seed a residual, approve it, and enqueue the posting.

    Returns ``(adjustment_id, operation_id)``.
    """
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


# ======================================================================================
# The transactional outbox — Guarantee 2
# ======================================================================================


@pytest.mark.asyncio
async def test_the_state_change_and_the_dispatch_intent_commit_together(
    engine: AsyncEngine,
) -> None:
    """§13.2: *"The state change and the dispatch intent are written in a single database
    transaction."*"""
    exception_id = await _seed_residual(marker="together")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    (adjustment,) = await _rows("adjustment")
    (intent,) = await _rows("outbox")
    assert intent["adjustment_id"] == adjustment["id"] == record.adjustment_id
    assert intent["state"] == "pending"
    assert intent["last_outcome"] is None
    assert intent["attempt_count"] == 0
    assert intent["next_attempt_at"] is None, "scheduling is 4.3's; 4.2 sets no policy"


@pytest.mark.asyncio
async def test_a_rolled_back_state_change_leaves_no_orphan_intent(engine: AsyncEngine) -> None:
    """**The first half of "no outbox row without its state change".**

    There is no window in which one row is durable and the other is not, because there is no second
    transaction for a crash to fall between.
    """
    exception_id = await _seed_residual(marker="rollback")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )
        await session.rollback()

    assert await _rows("adjustment") == []
    assert await _rows("outbox") == []


@pytest.mark.asyncio
async def test_a_committed_state_change_never_loses_its_intent(engine: AsyncEngine) -> None:
    """**The second half.** Every committed adjustment has an outbox row, checked by set equality
    rather than by count — a count would pass if the two sets were the same size and disjoint."""
    for index in range(3):
        exception_id = await _seed_residual(marker=f"paired{index}")
        approval_id = await _seed_approval(exception_id)
        async with AsyncSession(engine) as session, session.begin():
            await enqueue_posting(
                session, approval_id=approval_id, instruction=_instruction(exception_id)
            )

    adjustments = {row["id"] for row in await _rows("adjustment")}
    intents = {row["adjustment_id"] for row in await _rows("outbox")}
    assert len(adjustments) == 3
    assert intents == adjustments


@pytest.mark.asyncio
async def test_enqueueing_twice_writes_one_intent(engine: AsyncEngine) -> None:
    """``uq_outbox_adjustment_id`` is the guarantee; this only spares the caller an integrity error
    where a no-op is the truth."""
    exception_id = await _seed_residual(marker="twice")
    approval_id = await _seed_approval(exception_id)
    instruction = _instruction(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        first = await enqueue_posting(session, approval_id=approval_id, instruction=instruction)
    async with AsyncSession(engine) as session, session.begin():
        second = await enqueue_posting(session, approval_id=approval_id, instruction=instruction)

    assert first.identity == second.identity
    assert len(await _rows("outbox")) == 1
    assert len(await _rows("adjustment")) == 1


@pytest.mark.asyncio
async def test_recording_an_operation_alone_still_writes_no_intent(engine: AsyncEngine) -> None:
    """4.1's primitive keeps its old shape, and that is the point of having two entry points.

    ``record_operation`` records an identifier. ``enqueue_posting`` records an identifier *and* the
    intent to dispatch it. Keeping them separate is what lets 4.1's scope fence stay true while 4.2
    satisfies §13.2 — rather than the fence being edited to accommodate a widened function.
    """
    exception_id = await _seed_residual(marker="norow")
    approval_id = await _seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        await record_operation(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    assert len(await _rows("adjustment")) == 1
    assert await _rows("outbox") == []


# ======================================================================================
# Dispatch, end to end
# ======================================================================================


@pytest.mark.asyncio
async def test_dispatch_works_end_to_end_and_records_the_posting_reference(
    engine: AsyncEngine,
) -> None:
    """**The first exit criterion.** Intent → write-ahead record → adapter → outcome recorded.

    "Posting reference recorded on `Confirmed`" is written to both columns that exist for it: the
    attempt row is the per-send evidence, and ``adjustment.posting_ref`` is the durable answer to
    "was this operation applied, and where" without walking the attempt history.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="e2e")
    ledger = SimulatedLedger()

    result = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)

    assert isinstance(result.outcome, Confirmed)
    assert result.settled is True
    assert result.attempt_no == 1
    assert result.operation_id == operation_id

    assert ledger.applied_count(operation_id) == 1

    (attempt,) = await _rows("posting_attempt")
    assert attempt["state"] == "resolved"
    assert attempt["outcome"] == "confirmed"
    assert attempt["operation_id"] == operation_id
    assert attempt["attempt_no"] == 1
    assert attempt["posting_ref"] == result.outcome.posting_ref

    (intent,) = await _rows("outbox")
    assert intent["state"] == "settled"
    assert intent["last_outcome"] == "confirmed"
    assert intent["attempt_count"] == 1

    (adjustment,) = await _rows("adjustment")
    assert adjustment["posting_ref"] == result.outcome.posting_ref


@pytest.mark.asyncio
async def test_the_identifier_on_the_wire_is_the_one_that_was_persisted(
    engine: AsyncEngine,
) -> None:
    """§12.1: *"The identifier is **persisted before** the external call, never derived at call
    time."* The adapter is asked to record what it was sent, and it must be the stored value."""
    adjustment_id, operation_id = await _enqueued(engine, marker="wire")
    seen: list[str] = []

    def watch(sent_operation_id: str, _instruction: PostingInstruction) -> None:
        seen.append(sent_operation_id)
        return None

    await dispatch_once(
        engine,
        adjustment_id=adjustment_id,
        adapter=SimulatedLedger(responder=watch),
        sent_at=EPOCH,
    )

    (adjustment,) = await _rows("adjustment")
    assert seen == [operation_id] == [adjustment["operation_id"]]


@pytest.mark.asyncio
async def test_the_instruction_reaching_the_adapter_is_the_persisted_row(
    engine: AsyncEngine,
) -> None:
    """**M2.4 remains the sole owner of the amount.**

    What crosses to the adapter is read back from the ``adjustment`` row — a value the calculator
    produced and the database's own money constraints already accepted. Nothing between recomputes,
    re-rounds or re-derives it, and the comparison below is on the digit tuple so a mere re-spelling
    would fail too.
    """
    adjustment_id, _ = await _enqueued(engine, marker="amount")
    seen: list[PostingInstruction] = []

    def watch(_operation_id: str, instruction: PostingInstruction) -> None:
        seen.append(instruction)
        return None

    await dispatch_once(
        engine,
        adjustment_id=adjustment_id,
        adapter=SimulatedLedger(responder=watch),
        sent_at=EPOCH,
    )

    (adjustment,) = await _rows("adjustment")
    (sent,) = seen

    assert sent.amount == adjustment["amount"], "the value differs from what is stored"
    assert (sent.currency, sent.account_code, sent.period) == (
        adjustment["currency"],
        adjustment["account_code"],
        adjustment["period"],
    )
    assert sent.adjustment_id == adjustment_id

    # Compared against a *second ORM read of the same row*, not against the raw driver's value.
    # The first version of this assertion compared digit tuples across the two read paths and
    # failed for a reason that had nothing to do with mutation: the ORM's Money type quantises on
    # read to four places (2799.9700) while asyncpg returns the stored scale unchanged (2799.97).
    # Both are the same amount; comparing their spellings measured a canonicalisation policy rather
    # than detecting a change. What matters is that the dispatcher passed the persisted value
    # through untouched, and this is the comparison that says so.
    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
        ).scalar_one()
        assert sent.amount.as_tuple() == stored.amount.as_tuple(), "the amount was re-spelled"

    # And numerically equal to what the driver returns by the other read path, which is the
    # cross-path check the digit comparison above cannot make. The line that used to stand here —
    # `sent.amount is not stored.amount or sent.amount == stored.amount` — is true for every pair of
    # objects in Python and asserted nothing at all; a reviewer spotted it.
    assert sent.amount == adjustment["amount"], "the dispatcher altered the value"


@pytest.mark.asyncio
async def test_a_dispatch_with_no_intent_is_refused(engine: AsyncEngine) -> None:
    """Nothing dispatches that was not enqueued — and the refusal leaves no attempt record, because
    an attempt row is evidence of a send."""
    exception_id = await _seed_residual(marker="nointent")
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await record_operation(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    with pytest.raises(DispatchRefusedError, match="no dispatch intent"):
        await dispatch_once(
            engine, adjustment_id=record.adjustment_id, adapter=SimulatedLedger(), sent_at=EPOCH
        )

    assert await _rows("posting_attempt") == []


# ======================================================================================
# The write-ahead record and the crash window
# ======================================================================================


@pytest.mark.asyncio
async def test_a_crash_between_the_socket_write_and_the_response_leaves_an_in_flight_record(
    engine: AsyncEngine,
) -> None:
    """**The third exit criterion**, and the reason §12.1.1 exists at all.

    The adapter is made to fail *after* the send, which is the window the write-ahead record covers.
    Without a row committed before the call, this crash would be indistinguishable from a crash
    before it, and the system would hold no evidence a send occurred — which is precisely the state
    from which a recovery routine would wrongly conclude "not sent, safe to retry".

    What survives is an ``in_flight`` attempt with no outcome, which §12.1.1 defines as ``UNKNOWN``
    and never retryable. **Acting on it is 4.4's**; this asserts only that the evidence exists.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="crash")

    class CrashAfterSending:
        """Applies the posting, then loses the response — §19.1's shape, at the adapter."""

        name = "crash-after-sending"

        def __init__(self) -> None:
            self.ledger = SimulatedLedger()

        def capabilities(self) -> LedgerAdapterCapabilities:
            return self.ledger.capabilities()

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            await self.ledger.post(op, instruction)
            raise ConnectionResetError("the response never arrived")

    adapter = CrashAfterSending()
    with pytest.raises(ConnectionResetError):
        await dispatch_once(engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=EPOCH)

    (attempt,) = await _rows("posting_attempt")
    assert attempt["state"] == "in_flight"
    assert attempt["outcome"] is None
    assert attempt["resolved_at"] is None
    assert attempt["posting_ref"] is None
    assert attempt["sent_at"] == EPOCH
    assert attempt["operation_id"] == operation_id, "self-contained evidence, no join required"

    (intent,) = await _rows("outbox")
    assert intent["state"] == "pending", "an unresolved send never settles the intent"

    assert adapter.ledger.applied_count(operation_id) == 1, (
        "the ledger really did apply it — which is what makes the record's ambiguity real"
    )


@pytest.mark.asyncio
async def test_the_write_ahead_record_is_committed_before_the_adapter_is_called(
    engine: AsyncEngine,
) -> None:
    """**The ordering is the mechanism**, so it is observed from outside rather than inferred.

    A separate connection reads ``posting_attempt`` from inside the adapter call. Seeing the row
    there proves it was *committed* first — a row merely flushed in the dispatcher's transaction
    would be invisible to this connection.
    """
    adjustment_id, _ = await _enqueued(engine, marker="ordering")
    observed: list[list[asyncpg.Record]] = []

    def look(_operation_id: str, _instruction: PostingInstruction) -> None:
        return None

    class ObservingLedger(SimulatedLedger):
        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            observed.append(await _rows("posting_attempt"))
            return await super().post(op, instruction)

    await dispatch_once(
        engine,
        adjustment_id=adjustment_id,
        adapter=ObservingLedger(responder=look),
        sent_at=EPOCH,
    )

    (during,) = observed
    assert len(during) == 1, "the attempt row was not committed before the send"
    assert during[0]["state"] == "in_flight"
    assert during[0]["outcome"] is None


@pytest.mark.asyncio
async def test_the_identifier_is_identical_across_attempts_one_and_five(
    engine: AsyncEngine,
) -> None:
    """**The plan's fifth required test**, and 4.1's guarantee observed through 4.2's path.

    Five attempts, because the adapter keeps returning ``Unknown`` — the realistic case, and the
    only one that produces five attempts without a retry policy. **What drives them is this test**,
    called five times directly; 4.2 has no scheduler and 4.3 owns the one that will.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="fivetimes")
    ledger = SimulatedLedger(responder=lambda _op, _i: Unknown(detail="no response"))

    results = [
        await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
        for _ in range(5)
    ]

    assert [r.attempt_no for r in results] == [1, 2, 3, 4, 5]
    assert {r.operation_id for r in results} == {operation_id}

    attempts = sorted(await _rows("posting_attempt"), key=lambda row: row["attempt_no"])
    assert [row["attempt_no"] for row in attempts] == [1, 2, 3, 4, 5]
    assert {row["operation_id"] for row in attempts} == {operation_id}
    assert {row["outcome"] for row in attempts} == {"unknown"}

    (intent,) = await _rows("outbox")
    assert intent["state"] == "pending", "an UNKNOWN never settles an intent"
    assert intent["last_outcome"] == "unknown"

    assert ledger.applied_count(operation_id) == 0


# ======================================================================================
# Outcome semantics — recorded, never interpreted
# ======================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "outcome", "code", "settles"),
    [
        ("a declination", Rejected(reason="account closed"), "rejected", True),
        ("throttling", Throttled(retry_after=dt.timedelta(seconds=30)), "throttled", False),
        ("an ambiguous send", Unknown(detail="read timeout"), "unknown", False),
        (
            "a partial application",
            PartiallyApplied(applied_legs=1, posting_refs=("SIM-1",)),
            "partially_applied",
            False,
        ),
    ],
)
async def test_each_outcome_is_recorded_as_itself(
    engine: AsyncEngine, label: str, outcome: PostingOutcome, code: str, settles: bool
) -> None:
    """**Only `confirmed` and `rejected` may settle an intent**, and the database agrees.

    That constraint is what stops an ``UNKNOWN`` being quietly filed as done — and `Throttled` is
    not a declination, so it does not settle either. 4.2 records each as itself and stops; routing
    them is 4.3's and 4.4's.
    """
    adjustment_id, _ = await _enqueued(engine, marker=f"outcome{abs(hash(label)) % 100000}")

    result = await dispatch_once(
        engine,
        adjustment_id=adjustment_id,
        adapter=SimulatedLedger(responder=lambda _op, _i: outcome),
        sent_at=EPOCH,
    )

    assert result.outcome == outcome
    assert result.settled is settles

    (attempt,) = await _rows("posting_attempt")
    assert attempt["outcome"] == code
    assert attempt["state"] == "resolved"

    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] == code
    assert intent["state"] == ("settled" if settles else "pending")


@pytest.mark.asyncio
async def test_a_non_confirmed_outcome_records_no_posting_reference(engine: AsyncEngine) -> None:
    """A posting reference is meaningful only on an attempt the ledger actually applied."""
    adjustment_id, _ = await _enqueued(engine, marker="noref")

    await dispatch_once(
        engine,
        adjustment_id=adjustment_id,
        adapter=SimulatedLedger(responder=lambda _op, _i: Unknown(detail="?")),
        sent_at=EPOCH,
    )

    (attempt,) = await _rows("posting_attempt")
    (adjustment,) = await _rows("adjustment")
    assert attempt["posting_ref"] is None
    assert adjustment["posting_ref"] is None


# ======================================================================================
# Guarantee 3 — duplicate dispatch prevention, bounded by knowledge
# ======================================================================================


@pytest.mark.asyncio
async def test_a_second_send_is_refused_once_the_outcome_is_known_terminal(
    engine: AsyncEngine,
) -> None:
    """§13.3: *"The dispatcher will not *initiate* a second send for an `operation_id` already in a
    **known terminal state** (`CONFIRMED` or `REJECTED`)."*

    Refused before the write-ahead record, so a refusal leaves no evidence of a send that never
    happened — and the ledger's own count confirms nothing further reached it.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="terminal")
    ledger = SimulatedLedger()

    await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
    posts_after_first = ledger.posts_received

    with pytest.raises(DispatchRefusedError, match="already confirmed"):
        await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)

    assert ledger.posts_received == posts_after_first, "the second send never reached the ledger"
    assert ledger.applied_count(operation_id) == 1
    assert len(await _rows("posting_attempt")) == 1, "a refusal writes no attempt record"


@pytest.mark.asyncio
async def test_a_rejection_is_terminal_too(engine: AsyncEngine) -> None:
    """Both halves of "known terminal state". Retrying a declination is a defect, not a courtesy."""
    adjustment_id, _ = await _enqueued(engine, marker="rejterminal")
    ledger = SimulatedLedger(responder=lambda _op, _i: Rejected(reason="closed"))

    await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)

    with pytest.raises(DispatchRefusedError, match="already rejected"):
        await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)


@pytest.mark.asyncio
async def test_a_second_send_after_an_unknown_is_permitted_under_a_verified_enforces_key(
    engine: AsyncEngine,
) -> None:
    """**Bounded by what we know, and the boundary is asserted rather than described.**

    §13.3: *"When the outcome is `UNKNOWN` this guarantee is silent by construction: a system cannot
    suppress a duplicate of an operation whose first outcome it does not know."* So a second
    dispatch after an ``UNKNOWN`` is not refused — and what makes that safe is §13.5's capability
    branch, not this code: the reference adapter's ``ENFORCES_KEY`` has a conformance run behind it,
    so :data:`ResendDecision.PERMITTED` applies and the ledger's own suppression is what bounds the
    effect. The applied-count is read off the ledger to show it.

    **What this models, stated exactly.** The first send is ambiguous to *us*; the ledger did not
    apply it, because the injected responder answers before anything is written. The harder
    scenario — applied at the ledger and the response lost on the way back — needs an adapter that
    can commit and then fail, and §19 assigns that to 4.5's chaos suite, where it runs against three
    capability configurations. Saying so here is cheaper than a test that looks like it covers the
    harder case and does not.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="silent")

    sends = 0

    def lose_the_first_response(
        _op: str, _instruction: PostingInstruction
    ) -> PostingOutcome | None:
        nonlocal sends
        sends += 1
        return Unknown(detail="response lost") if sends == 1 else None

    ledger = SimulatedLedger(responder=lose_the_first_response)
    assert capabilities_for(ledger).suppresses_duplicates, "the reference adapter is verified"

    first = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
    assert isinstance(first.outcome, Unknown)

    second = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
    assert isinstance(second.outcome, Confirmed)
    assert second.attempt_no == 2

    assert ledger.applied_count(operation_id) == 1, (
        "at most one application, measured at the ledger"
    )
    assert len(await _rows("posting_attempt")) == 2, "both sends are evidenced"


@pytest.mark.asyncio
async def test_a_second_send_after_an_unknown_is_refused_when_the_capability_is_unproven(
    engine: AsyncEngine,
) -> None:
    """**The refusal half of §13.5 clause 3, and the reason "declaration is not evidence" bites.**

    This adapter *declares* ``ENFORCES_KEY`` — it returns the reference adapter's own capability
    record — and it is a different class, so no conformance run stands behind it and
    :func:`capabilities_for` downgrades both strong claims to ``NONE``. §13.5 then permits no
    automatic re-send: the correct action is manual recovery, and this increment's part of that is
    to stop.

    The first version of this test asserted the opposite, because the dispatcher had no capability
    gate at all and re-sent from an ambiguous state unconditionally — the defect §13.5 names in as
    many words: *"never blindly retry an irreversible financial write."*
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="unproven")
    inner = SimulatedLedger()

    class DeclaresWhatItCannotProve:
        name = "declares-what-it-cannot-prove"

        def __init__(self) -> None:
            self.sends = 0

        def capabilities(self) -> LedgerAdapterCapabilities:
            return inner.capabilities()

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            self.sends += 1
            await inner.post(op, instruction)
            return Unknown(detail="response lost")

    adapter = DeclaresWhatItCannotProve()

    first = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=EPOCH)
    assert isinstance(first.outcome, Unknown)
    assert adapter.capabilities().idempotency is IdempotencyMode.ENFORCES_KEY, "it does declare it"
    assert capabilities_for(adapter).idempotency is IdempotencyMode.NONE, "and it is not believed"

    with pytest.raises(DispatchRefusedError, match="manual recovery"):
        await dispatch_once(engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=EPOCH)

    assert adapter.sends == 1, "the refusal happened before the socket, not after it"
    assert inner.applied_count(operation_id) == 1
    assert len(await _rows("posting_attempt")) == 1, "a refusal writes no attempt record"


@pytest.mark.asyncio
async def test_an_unresolved_attempt_alone_is_enough_to_trigger_the_ambiguity_gate(
    engine: AsyncEngine,
) -> None:
    """**The gap the first gate had: it read only ``outbox.last_outcome``.**

    That column is written when a dispatch *completes*. A send that failed mid-flight — the crash
    §12.1.1 exists for — leaves it NULL, so the gate saw nothing and permitted a further send into
    exactly the state it was built to guard. Here the first dispatch raises before recording
    anything, leaving an ``in_flight`` attempt and a ``pending`` outbox row, and the gate must still
    fire.
    """
    adjustment_id, _ = await _enqueued(engine, marker="inflightgate")
    inner = SimulatedLedger()

    class CrashesAfterSending:
        name = "crashes-after-sending"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            await inner.post(op, instruction)
            raise ConnectionResetError("the response never arrived")

    with pytest.raises(ConnectionResetError):
        await dispatch_once(
            engine, adjustment_id=adjustment_id, adapter=CrashesAfterSending(), sent_at=EPOCH
        )

    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] is None, "nothing was recorded; the old gate saw exactly this"
    (attempt,) = await _rows("posting_attempt")
    assert attempt["state"] == "in_flight"

    with pytest.raises(DispatchRefusedError, match="undetermined state"):
        await dispatch_once(
            engine, adjustment_id=adjustment_id, adapter=CrashesAfterSending(), sent_at=EPOCH
        )

    assert len(await _rows("posting_attempt")) == 1


@pytest.mark.asyncio
async def test_a_structurally_queryable_adapter_is_still_routed_to_manual_recovery(
    engine: AsyncEngine,
) -> None:
    """**"Declaration is not evidence" is not a slogan; it changes which branch runs.**

    This adapter *declares* ``BY_OPERATION_ID`` and really does implement ``get_by_operation_id``.
    §13.5's middle branch would reconcile by querying it — and that branch does not run, because
    nothing has ever proven the query returns the posting it claims to. :func:`capabilities_for`
    downgrades the unproven claim to ``NONE``, and what is left is the branch for an adapter that
    can neither suppress nor detect a duplicate: stop, and route to manual recovery.

    The first version of this test expected the reconciliation message and asserted the opposite
    behaviour. Running it was what showed the expectation was wrong rather than the code — the
    middle branch is reachable only for an adapter whose conformance run proved the query and not
    the suppression, and this repository ships no such adapter. :func:`resend_decision` is a pure
    function of the capability record, so all three branches are covered where they can be covered
    honestly, in ``test_ledger_adapter.py``.
    """
    adjustment_id, _ = await _enqueued(engine, marker="reconcilefirst")

    class QueryableOnly:
        name = "queryable-only"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities(
                posting_identity_query=PostingQueryMode.BY_OPERATION_ID
            )

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            return Unknown(detail="no idea")

        async def get_by_operation_id(self, op: str) -> QueryOutcome:
            return Found(posting_ref="SIM-known")

    adapter = QueryableOnly()
    assert adapter.capabilities().queryable_by_operation_id is True, "it declares it"
    assert capabilities_for(adapter).queryable_by_operation_id is False, "and it is not believed"

    await dispatch_once(engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=EPOCH)

    with pytest.raises(DispatchRefusedError, match="manual recovery"):
        await dispatch_once(engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=EPOCH)


@pytest.mark.asyncio
async def test_a_throttled_outcome_is_not_treated_as_an_ambiguous_one(
    engine: AsyncEngine,
) -> None:
    """The control on the ambiguity set, and §10.1 is explicit about why it is one.

    ``Throttled`` means the request was turned away *before* it could be applied — a scheduling
    signal, not a declination and not an ambiguity. Folding it in with ``UNKNOWN`` would route every
    rate-limited posting to manual recovery, which is the false-positive direction that makes an
    operator stop reading the queue.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="throttled")

    calls = 0

    def throttle_once(_op: str, _instruction: PostingInstruction) -> PostingOutcome | None:
        nonlocal calls
        calls += 1
        return Throttled(retry_after=dt.timedelta(seconds=30)) if calls == 1 else None

    ledger = SimulatedLedger(responder=throttle_once)

    first = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
    assert isinstance(first.outcome, Throttled)

    second = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
    assert isinstance(second.outcome, Confirmed)
    assert ledger.applied_count(operation_id) == 1


# ======================================================================================
# Concurrent dispatch — the unique constraint, not the row lock
# ======================================================================================


@pytest.mark.asyncio
async def test_two_dispatchers_racing_one_operation_apply_it_at_most_once(
    engine: AsyncEngine,
) -> None:
    """**The invariant, measured at the ledger, whichever way the race falls.**

    Transaction 1 takes ``FOR UPDATE`` on the outbox row and then commits, releasing it before the
    send — §12.1.1 forces that, because the write-ahead record must be committed separately. So two
    dispatchers can both pass the gate, and a reviewer walked through exactly that. Two legal
    outcomes follow, depending on interleaving: the loser collides on
    ``uq_posting_attempt_adjustment_no`` and is refused, or it observes the in-flight attempt and is
    permitted to send under the verified ``ENFORCES_KEY``. Both are safe, and the reason they are
    safe is the same one: at most one application at the ledger.

    So this asserts the invariant rather than the interleaving. A test that demanded one particular
    branch would be asserting a scheduling accident, and would flake on a busier machine.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="race")
    ledger = SimulatedLedger()

    results = await asyncio.gather(
        *(
            dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)
            for _ in range(4)
        ),
        return_exceptions=True,
    )

    for result in results:
        assert not isinstance(result, BaseException) or isinstance(result, DispatchRefusedError), (
            f"an unexpected failure escaped the race: {result!r}"
        )

    assert ledger.applied_count(operation_id) == 1, "the ledger applied it more than once"

    attempts = await _rows("posting_attempt")
    numbers = [row["attempt_no"] for row in attempts]
    assert len(numbers) == len(set(numbers)), f"duplicate attempt numbers: {sorted(numbers)}"

    (intent,) = await _rows("outbox")
    assert intent["last_outcome"] == "confirmed"


@pytest.mark.asyncio
async def test_the_write_ahead_record_is_unique_per_attempt(engine: AsyncEngine) -> None:
    """The mechanism the race relies on, asserted directly rather than inferred from timing.

    The invariant test above cannot control which branch it takes. This one proves the constraint
    that makes the losing branch safe actually exists and actually rejects, which is what turns
    "the database prevents it" from a comment into a fact.
    """
    adjustment_id, operation_id = await _enqueued(engine, marker="uniqueattempt")

    async def insert() -> None:
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                PostingAttempt(
                    adjustment_id=adjustment_id,
                    operation_id=operation_id,
                    attempt_no=1,
                    sent_at=EPOCH,
                    state=AttemptState.IN_FLIGHT,
                )
            )

    await insert()
    with pytest.raises(IntegrityError, match="uq_posting_attempt_adjustment_no"):
        await insert()


# ======================================================================================
# The dispatcher branches on declared capability
# ======================================================================================


@pytest.mark.asyncio
async def test_the_dispatch_result_carries_the_effective_capabilities(
    engine: AsyncEngine,
) -> None:
    """**The second exit criterion.** The path is chosen from the record the adapter declares.

    ``capabilities`` on the result is the *effective* record — the declaration with every unproven
    claim already downgraded — so a caller cannot accidentally act on an unverified one.
    """
    adjustment_id, _ = await _enqueued(engine, marker="caps")
    ledger = SimulatedLedger()

    result = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)

    assert result.capabilities == capabilities_for(ledger)
    assert result.capabilities.suppresses_duplicates is True
    assert result.capabilities.queryable_by_operation_id is True
    assert result.capabilities.permits_effectively_once_claim is True


@pytest.mark.asyncio
async def test_a_weakly_declared_adapter_reports_no_reconciliation_route(
    engine: AsyncEngine,
) -> None:
    """The negative half of the branch, and the degradation §13.6 asks for.

    An adapter declaring ``NONE``/``NONE`` is dispatched to exactly the same way — the send is the
    send — but it offers no route to find out what happened, and the result says so. What a caller
    does about that is §13.5's manual-recovery branch and belongs to 4.4.
    """
    adjustment_id, _ = await _enqueued(engine, marker="weak")
    weak = SimulatedLedger(name="weak-ledger", capabilities=LedgerAdapterCapabilities())

    result = await dispatch_once(engine, adjustment_id=adjustment_id, adapter=weak, sent_at=EPOCH)

    assert result.capabilities.idempotency is IdempotencyMode.NONE
    assert result.capabilities.posting_identity_query is PostingQueryMode.NONE
    assert result.capabilities.permits_effectively_once_claim is False
    assert reconciliation_is_available(weak) is False


@pytest.mark.asyncio
async def test_an_unverified_strong_declaration_offers_no_reconciliation_route(
    engine: AsyncEngine,
) -> None:
    """**Declaration is not evidence, observed through the dispatcher.**

    This adapter declares both strong capabilities and has no conformance record. It is
    structurally queryable — the method is right there — and the answer is still ``False``, because
    the claim behind it was never proven.

    It is a distinct class rather than a renamed :class:`SimulatedLedger`, and that is not
    incidental. Verification is keyed on the implementation, so a rename no longer makes the
    reference adapter unverified — which is exactly the forgery the keying change closed, and it
    would have quietly turned this test green for the wrong reason.
    """
    adjustment_id, _ = await _enqueued(engine, marker="unverified")

    class NeverRanTheSuite:
        name = "never-ran-the-suite"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities(
                idempotency=IdempotencyMode.ENFORCES_KEY,
                posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
            )

        async def post(self, op: str, instruction: PostingInstruction) -> PostingOutcome:
            return Confirmed(posting_ref="SIM-unverified")

        async def get_by_operation_id(self, op: str) -> QueryOutcome:
            return Found(posting_ref="SIM-unverified")

    unverified = NeverRanTheSuite()

    result = await dispatch_once(
        engine, adjustment_id=adjustment_id, adapter=unverified, sent_at=EPOCH
    )

    assert unverified.capabilities().queryable_by_operation_id is True, "it declares the capability"
    assert result.capabilities.queryable_by_operation_id is False, "and it is not verified"
    assert reconciliation_is_available(unverified) is False
    assert result.capabilities.permits_effectively_once_claim is False


@pytest.mark.asyncio
async def test_the_verified_reference_adapter_offers_a_reconciliation_route(
    engine: AsyncEngine,
) -> None:
    """The control. A check that always said ``False`` would pass the two tests above."""
    ledger = SimulatedLedger()
    assert reconciliation_is_available(ledger) is True

    adjustment_id, operation_id = await _enqueued(engine, marker="route")
    await dispatch_once(engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=EPOCH)

    assert isinstance(await ledger.get_by_operation_id(operation_id), Found)


# ======================================================================================
# Scope — 4.2 touches nothing a later increment owns
# ======================================================================================


@pytest.mark.asyncio
async def test_dispatch_creates_no_row_in_any_later_increment_table(engine: AsyncEngine) -> None:
    """4.2 dispatches once and records the outcome. It does not retry, dead-letter, recover or
    audit."""
    adjustment_id, _ = await _enqueued(engine, marker="scope")
    await dispatch_once(
        engine, adjustment_id=adjustment_id, adapter=SimulatedLedger(), sent_at=EPOCH
    )

    connection = await asyncpg.connect(DSN)
    try:
        for table in ("dlq", "recovery_queue", "audit_event"):
            count = await connection.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"4.2 wrote to {table}, which belongs to a later increment"
        assert await connection.fetchval("SELECT count(*) FROM posting_attempt") == 1
        assert await connection.fetchval("SELECT count(*) FROM outbox") == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_dispatch_never_schedules_a_next_attempt(engine: AsyncEngine) -> None:
    """``next_attempt_at`` exists from M1.2 and stays NULL through 4.2.

    Writing one would be this increment quietly deciding a retry policy — backoff, jitter and the
    attempt ceiling are 4.3's, and a value here would be a schedule nobody specified.
    """
    adjustment_id, _ = await _enqueued(engine, marker="noschedule")
    await dispatch_once(
        engine,
        adjustment_id=adjustment_id,
        adapter=SimulatedLedger(responder=lambda _op, _i: Unknown(detail="?")),
        sent_at=EPOCH,
    )

    (intent,) = await _rows("outbox")
    assert intent["next_attempt_at"] is None
