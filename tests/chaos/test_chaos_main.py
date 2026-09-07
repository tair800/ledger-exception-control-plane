"""§19's scenarios against `main` — the GREEN half of the gate (increment 4.5).

`PROJECT_SPEC.md` §19: *"`main` — the real implementation. **It must not** [double-post]."*

**Every assertion here reads the ledger, not our own records.** §19.1 is explicit about why:

    The test inspects the simulated ledger's applied-count for X directly — it does not infer the
    outcome from application state, since inferring from our own records is exactly what fails here.

An assertion built on ``adjustment.posting_ref`` or on an ``outbox`` row would be asking the system
whether it thinks it double-posted. The number that matters is how many postings the books hold, and
only the ledger knows that.

Each scenario is parametrised over §19's three capability configurations and asserts the expectation
declared in ``scenarios.py`` **before** the run. Where that expectation is zero the work did not
complete automatically, and that is the specified outcome rather than a shortfall: under a
capability that can neither suppress a duplicate nor answer a query, §13.5 requires the automatic
path to stop.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.db.control import ApprovalDecision, TreatmentCode
from ledger_exception_control_plane.ingest.service import ingest
from ledger_exception_control_plane.ledger import (
    Fault,
    FaultInjectingLedger,
    SimulatedLedger,
    Unknown,
)
from ledger_exception_control_plane.operations import (
    ReconciliationPolicy,
    Resolution,
    claim_residuals,
    dispatch_once,
    enqueue_posting,
    reconcile_once,
)
from ledger_exception_control_plane.operations.approval import (
    ApprovalRefusedError,
    RefusalReason,
    record_decision,
)
from ledger_exception_control_plane.security import Principal, Role
from tests.chaos.conftest import (
    DSN,
    REBOOK_ACCOUNT,
    enqueued,
    instruction,
    one_day_later,
    rows,
    seed_approval,
    seed_residual,
)
from tests.chaos.results import observe
from tests.chaos.scenarios import (
    AMOUNT,
    CONFIGURATIONS,
    INFLIGHT,
    Capability,
    Scenario,
    adapter_for,
    expectation,
)

pytestmark = pytest.mark.integration

CONTROLLER = Principal("controller-a", Role.CONTROLLER)

#: The header the real parser accepts, for the two scenarios that drive real ingestion.
from ledger_exception_control_plane.ingest.parser import SETTLEMENT_COLUMNS  # noqa: E402

PAYLOAD = (
    "\n".join(
        (
            ",".join(SETTLEMENT_COLUMNS),
            "PSP-CHAOS-1,ORD-CHAOS-1,capture,2799.97,EUR,2026-06-01,,,,capture",
        )
    )
    + "\n"
).encode("utf-8")


def _expect(scenario: Scenario, capability: Capability) -> int:
    return expectation(scenario=scenario, capability=capability, branch="main").applied


async def _resolve(
    engine: AsyncEngine, adapter: object, adjustment_id: uuid.UUID, capability: Capability
) -> Resolution:
    """Drive `main`'s recovery for one ambiguous operation, at a time past **both** windows.

    One pass under ``ENFORCES_KEY`` and ``NONE``/``NONE``: the first resolves by re-sending, the
    second routes to an operator and stops. Under ``BY_OPERATION_ID`` a positive hit also resolves
    on the first pass, and a negative one deliberately does not — §13.5 requires N consecutive
    negatives — so the loop below runs until the pass stops being ``UNRESOLVED``, bounded by the
    policy's own ceiling rather than by a number chosen here.

    **``INFLIGHT`` is added and that is not padding.** §13.5 trusts a negative answer only after N
    consecutive negatives *and* both declared windows have elapsed, and every adapter here declares
    a 30-second in-flight window. A first version of this helper stepped ``now`` forward by one
    second per pass from the instant of the send, so the window never elapsed, no negative could
    ever become trustworthy, and the query bound exhausted into manual recovery instead — correct
    behaviour, reached for a reason that had nothing to do with the scenario. The docstring said
    "past both windows" while the arithmetic did not deliver it, which is the kind of harness defect
    that reads as a finding about the system.
    """
    policy = ReconciliationPolicy()
    last = Resolution.UNRESOLVED
    for index in range(policy.max_queries + 1):
        report = await reconcile_once(
            engine,
            adjustment_id=adjustment_id,
            adapter=adapter,  # type: ignore[arg-type]
            now=one_day_later() + INFLIGHT + dt.timedelta(seconds=index + 1),
            policy=policy,
        )
        last = report.resolution
        if last is not Resolution.UNRESOLVED:
            return last
    return last


# ======================================================================================
# §19.1 — the required scenario
# ======================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_lost_response_after_a_committed_ledger_write(
    engine: AsyncEngine, capability: Capability
) -> None:
    """**The scenario this design exists for.** §19.1, step by step.

    1. the dispatcher sends a posting for `operation_id` X;
    2. the ledger **commits it**;
    3. the response is lost;
    4. the dispatcher records `UNKNOWN` — not success, not failure;
    5. recovery follows the adapter's declared capability;
    6. **the ledger has applied X exactly once.**

    The fault fires on the first send only, because the failure §19.1 describes is transient: what
    is being tested is what the system does *afterwards*. A permanently faulted ledger would leave
    every branch looking identically safe, since an implementation that never posts never
    double-posts.
    """
    inner = adapter_for(capability)
    adapter = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    adjustment_id, operation_id = await enqueued(engine, marker=f"lost{capability.value}")

    # 1-3. The send. The books move; the client is told nothing.
    result = await dispatch_once(
        engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=one_day_later()
    )

    assert adapter.injections == 1, "the fault must actually have fired"
    assert adapter.applied_count(operation_id) == 1, "the ledger committed it"

    # 4. Recorded as ambiguous. Never as success, never as failure.
    assert isinstance(result.outcome, Unknown)
    intent = (await rows("outbox", adjustment_id=adjustment_id))[0]
    assert intent["last_outcome"] == "unknown"
    assert intent["state"] == "pending", "an unknown outcome is not a finished dispatch"
    assert (await rows("adjustment", id=adjustment_id))[0]["posting_ref"] is None

    # 5. Recovery, per capability.
    resolution = await _resolve(engine, adapter, adjustment_id, capability)
    if capability is Capability.ENFORCES_KEY:
        assert resolution is Resolution.RESENT, "a bounded re-send is permitted and is the answer"
    elif capability is Capability.BY_OPERATION_ID:
        assert resolution is Resolution.CONFIRMED, "the query found it; a positive hit is trusted"
        assert adapter.posts_received == 1, "it asked, and it did not send"
    else:
        assert resolution is Resolution.ROUTED_TO_RECOVERY
        assert adapter.posts_received == 1, "no automatic re-send occurs at all"
        assert len(await rows("recovery_queue", adjustment_id=adjustment_id)) == 1

    # 6. The assertion that carries the claim, read off the books.
    assert adapter.applied_count(operation_id) == 1
    observe(
        scenario=Scenario.LOST_RESPONSE_AFTER_COMMIT,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.LOST_RESPONSE_AFTER_COMMIT, capability)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_the_audit_trail_is_complete_in_all_three_branches(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§19.1's closing requirement: *"attempt, `UNKNOWN`, reconciliation query and result where
    applicable, and the final resolution."*"""
    inner = adapter_for(capability)
    adapter = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    adjustment_id, _ = await enqueued(engine, marker=f"trail{capability.value}")

    await dispatch_once(
        engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=one_day_later()
    )
    await _resolve(engine, adapter, adjustment_id, capability)

    events = [(row["tool"], row["outcome"]) for row in await rows("audit_event")]

    assert ("compute_amount", "success") in events, "the amount becoming durable"
    assert events.count(("post", "quarantined")) >= 2, "the send recorded, then the ambiguity"

    if capability is Capability.ENFORCES_KEY:
        assert ("post", "success") in events, "the re-send and its answer"
    elif capability is Capability.BY_OPERATION_ID:
        assert ("reconcile", "success") in events, "the query answer and the resolution"
        assert events.count(("reconcile", "success")) == 2
    else:
        assert ("recover", "quarantined") in events, "routed to an operator"


# ======================================================================================
# The other six scenarios
# ======================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_an_ambiguous_5xx_is_never_read_as_not_applied(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§14: *"A 5xx is never classified as 'not applied'."*

    Indistinguishable from §19.1 at the client — the same ``Unknown``, the same recorded state —
    and different at the books, where nothing was applied. The system cannot tell them apart and
    does not try; it follows the same capability branch, which is why the expected counts differ by
    configuration rather than by scenario.
    """
    inner = adapter_for(capability)
    adapter = FaultInjectingLedger(inner, fault=Fault.AMBIGUOUS_5XX)
    adjustment_id, operation_id = await enqueued(engine, marker=f"fivexx{capability.value}")

    result = await dispatch_once(
        engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=one_day_later()
    )
    assert isinstance(result.outcome, Unknown)
    assert adapter.applied_count(operation_id) == 0, "nothing was applied"
    assert (await rows("outbox", adjustment_id=adjustment_id))[0]["last_outcome"] == "unknown", (
        "and it is emphatically not recorded as a rejection"
    )

    resolution = await _resolve(engine, adapter, adjustment_id, capability)

    if capability is Capability.ENFORCES_KEY:
        assert resolution is Resolution.RESENT
        assert adapter.applied_count(operation_id) == 1, "the re-send completed the work"
    elif capability is Capability.BY_OPERATION_ID:
        assert resolution is Resolution.REJECTED, "N negatives and both windows elapsed"
    else:
        assert resolution is Resolution.ROUTED_TO_RECOVERY

    observe(
        scenario=Scenario.AMBIGUOUS_5XX,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.AMBIGUOUS_5XX, capability)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_crash_before_commit_leaves_no_effect_and_re_runs_once(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§14: *"No outbox row, no effect; work re-claimable."*

    The crash is a rollback of the transaction that would have written the adjustment and its
    dispatch intent — which is what a crash before commit *is*, and the reason `main` writes both in
    one transaction. There is no window for a crash to fall into.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    exception_id = await seed_residual(marker=f"crash{capability.value}")
    approval_id = await seed_approval(exception_id)

    async with AsyncSession(engine) as session, session.begin():
        await enqueue_posting(
            session, approval_id=approval_id, instruction=instruction(exception_id)
        )
        await session.rollback()

    assert await rows("adjustment") == [], "no adjustment"
    assert await rows("outbox") == [], "and no dispatch intent: neither, or both"
    assert adapter.total_applied == 0, "nothing was sent, so nothing was applied"

    # The work is re-claimable, and completing it posts exactly once.
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=instruction(exception_id)
        )
    await dispatch_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=one_day_later()
    )

    observe(
        scenario=Scenario.CRASH_BEFORE_COMMIT,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.CRASH_BEFORE_COMMIT, capability)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_duplicate_webhook_delivery_is_a_no_op(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§14: *"Content-hash unique constraint makes it a no-op."*

    Driven through the *real* ingestion path rather than a seeded row, because the control being
    tested is a unique constraint on a value ingestion computes.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))

    first = await ingest(engine, PAYLOAD, source="chaos", received_at=one_day_later())
    second = await ingest(engine, PAYLOAD, source="chaos", received_at=one_day_later())

    assert first.accepted
    assert second.duplicate, "the redelivery is recognised as the payload it is"
    assert len(await rows("settlement_line")) == 1, "one payload, one unit of work"

    line = (await rows("settlement_line"))[0]
    opened = await asyncpg.connect(DSN)
    try:
        exception_id = uuid.uuid4()
        await opened.execute(
            "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
            " status, rule_id, classifier_version, correlation_id, created_at)"
            " VALUES ($1, $2, 'unmatched', 'fee_split', 'open', 'fees_deducted_from_a_capture',"
            " 'residual-r2', 'lecp:dup', $3)",
            exception_id,
            line["id"],
            one_day_later(),
        )
    finally:
        await opened.close()

    approval_id = await seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=instruction(exception_id)
        )
    await dispatch_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=one_day_later()
    )

    observe(
        scenario=Scenario.DUPLICATE_WEBHOOK,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.DUPLICATE_WEBHOOK, capability)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_worker_killed_mid_batch_leaves_the_work_re_claimable(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§14: *"Unclaimed work re-claimable; claimed work times out and returns."*

    The kill is the claiming transaction ending without committing, which is what a killed process
    leaves behind. `main`'s claim is **transaction-scoped** — ``SELECT … FOR UPDATE SKIP LOCKED``
    holds the row only while the transaction lives — so the work returns by itself rather than
    needing a timeout to expire.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    exception_id = await seed_residual(marker=f"killed{capability.value}")

    async with AsyncSession(engine) as session, session.begin():
        claim = await claim_residuals(session, limit=10)
        assert [residual.exception_id for residual in claim.residuals] == [exception_id]
        # The worker dies here: the transaction ends without committing anything.
        await session.rollback()

    # A second worker finds the work waiting.
    async with AsyncSession(engine) as session, session.begin():
        again = await claim_residuals(session, limit=10)
        assert [residual.exception_id for residual in again.residuals] == [exception_id], (
            "the claim was transaction-scoped, so the work came back"
        )

    approval_id = await seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=instruction(exception_id)
        )
    await dispatch_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=one_day_later()
    )

    observe(
        scenario=Scenario.WORKER_KILLED_MID_BATCH,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.WORKER_KILLED_MID_BATCH, capability)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_two_workers_cannot_both_claim_one_residual(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§14: *"`SKIP LOCKED` prevents it; asserted under concurrency."*

    **Forced, not hoped for.** Two coroutines gathered and hoped to overlap is worse than weak with
    ``SKIP LOCKED``: run one after the other, both claim the row and the test fails for a reason
    that has nothing to do with the lock. The first worker holds its transaction open on an event
    until the second has finished trying.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    exception_id = await seed_residual(marker=f"race{capability.value}")

    first_has_claimed = asyncio.Event()
    second_has_tried = asyncio.Event()

    async def worker_one() -> list[uuid.UUID]:
        async with AsyncSession(engine) as session, session.begin():
            claim = await claim_residuals(session, limit=10)
            first_has_claimed.set()
            await asyncio.wait_for(second_has_tried.wait(), timeout=20)
            return [residual.exception_id for residual in claim.residuals]

    async def worker_two() -> list[uuid.UUID]:
        await asyncio.wait_for(first_has_claimed.wait(), timeout=20)
        try:
            async with AsyncSession(engine) as session, session.begin():
                claim = await claim_residuals(session, limit=10)
                return [residual.exception_id for residual in claim.residuals]
        finally:
            second_has_tried.set()

    one, two = await asyncio.gather(worker_one(), worker_two())

    assert one == [exception_id]
    assert two == [], "the second worker skipped the locked row rather than waiting for it"

    approval_id = await seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=instruction(exception_id)
        )
    await dispatch_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=one_day_later()
    )

    observe(
        scenario=Scenario.TWO_WORKERS_ONE_RESIDUAL,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.TWO_WORKERS_ONE_RESIDUAL, capability)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_replayed_approval_token_is_refused(
    engine: AsyncEngine, capability: Capability
) -> None:
    """§14: *"Rejected; audit event recorded."*

    The token is unique in the database, so the replay loses to a constraint rather than to an
    application check a refactor could skip — and one approval means one authorised posting.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    first_exception = await seed_residual(marker=f"tok1{capability.value}")
    second_exception = await seed_residual(marker=f"tok2{capability.value}")
    token = uuid.uuid4().hex

    async with AsyncSession(engine) as session, session.begin():
        record = await record_decision(
            session,
            exception_id=first_exception,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=token,
            now=one_day_later(),
            treatment=TreatmentCode.REBOOK,
        )

    async with AsyncSession(engine) as session:
        with pytest.raises(ApprovalRefusedError) as refused:
            await record_decision(
                session,
                exception_id=second_exception,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=token,
                now=one_day_later(),
                treatment=TreatmentCode.REBOOK,
            )
    assert refused.value.reason is RefusalReason.TOKEN_ALREADY_USED

    assert len(await rows("approval")) == 1, "one token, one approval"

    async with AsyncSession(engine) as session, session.begin():
        operation = await enqueue_posting(
            session, approval_id=record.approval_id, instruction=instruction(first_exception)
        )
    await dispatch_once(
        engine, adjustment_id=operation.adjustment_id, adapter=adapter, sent_at=one_day_later()
    )

    observe(
        scenario=Scenario.REPLAYED_APPROVAL_TOKEN,
        capability=capability,
        branch="main",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.REPLAYED_APPROVAL_TOKEN, capability)


# ======================================================================================
# The invariant, stated once over the whole matrix
# ======================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_main_never_applies_a_financial_effect_more_than_once(
    engine: AsyncEngine, capability: Capability
) -> None:
    """**The claim, asserted as one sentence rather than as seven numbers.**

    Every scenario above asserts its own expected count, and this asserts the property those counts
    are evidence *for*: across the whole matrix, `main` never commits the same financial effect
    twice. A suite of exact counts can be satisfied by a table somebody adjusted; an invariant
    cannot.

    Driven over the two ambiguous faults, which are the only ones from which a duplicate could
    arise — the other five leave nothing on the books to duplicate.
    """
    for fault in (Fault.COMMIT_THEN_LOSE_RESPONSE, Fault.AMBIGUOUS_5XX):
        adapter = FaultInjectingLedger(adapter_for(capability), fault=fault)
        adjustment_id, operation_id = await enqueued(
            engine, marker=f"inv{fault.value[:6]}{capability.value}"
        )
        await dispatch_once(
            engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=one_day_later()
        )
        await _resolve(engine, adapter, adjustment_id, capability)

        assert adapter.applied_count(operation_id) <= 1, (
            f"{fault.value} under {capability.value} applied "
            f"{adapter.applied_count(operation_id)} times"
        )
        assert adapter.total_applied <= 1
        await _wipe_between()


async def _wipe_between() -> None:
    from tests.chaos.conftest import wipe

    await wipe()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_every_posting_main_commits_carries_the_one_amount(engine: AsyncEngine) -> None:
    """The same property, measured at the ledger instead of inferred from the source.

    Drives a real dispatch and reads what the ledger *committed* — amount, currency, account and
    period — rather than what the harness intended to send. Together with the scan above this is
    what makes "adjustments posted" a count of one repeated economic event: the source cannot build
    a different instruction, and the ledger confirms the one it received.
    """
    ledger = SimulatedLedger()
    adjustment_id, operation_id = await enqueued(engine, marker="oneamount")
    await dispatch_once(
        engine, adjustment_id=adjustment_id, adapter=ledger, sent_at=one_day_later()
    )

    committed = ledger.applied(operation_id)
    assert committed is not None, "nothing was committed, so there is nothing to check"
    assert committed.amount == AMOUNT
    assert (committed.currency, committed.account_code, committed.period) == (
        "EUR",
        REBOOK_ACCOUNT,
        "2026-06",
    )
