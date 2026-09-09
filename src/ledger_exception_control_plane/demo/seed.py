"""Drive the real pipeline once and leave a database a reviewer can read (M7 support).

**What this is.** One function that calls the shipped service entry points in order and leaves
behind an exception in every state the console renders: proposed, approved, priced, dispatched,
ambiguous, dead-lettered, and open in recovery. It exists so `make demo` produces something to
look at, and so the console's fifteen elements have real rows behind them rather than fixtures.

**What this is not.** It is **not the orchestration**. Nothing in `src/` calls these stages in
sequence as a running system — that is M7's wiring and it does not exist. This module is a
composition written once, for a demonstration, and it says so rather than letting a reader mistake
it for a pipeline. The distinction is the same one `tests/test_audit_contract_postgres.py` draws
about its own walk, and for the same reason: a demo that looked like a running system would be
claiming functionality the repository does not have.

**Every stage is the production entry point.** ``ingest``, ``run_matching``,
``run_classification``, ``record_decision``, ``enqueue_posting``, ``dispatch_once`` and
``reconcile_once`` are the functions the system ships. Nothing here writes a domain row directly
except the opening ledger snapshot, which is the *counterparty's* data and has no ingestion path in
this system — a ledger the system reconciles against is not something it creates.

**No model call, and no credential.** The proposal is produced by a **stand-in proposer**
declared in this module, and it says so in the two fields a reader sees: its ``model_id`` is
``stand-in`` and its rationale begins by stating it was not produced by a model. So the console
renders a real proposal, through the real port and the real persistence path, and a visitor is told
what produced it.

The committed cassettes were the obvious alternative and cannot serve here: they are keyed by
request fingerprint against the *canonical corpus*, and this demonstration ingests a different
payload deliberately — one constructed to reach the two priceable classifications. Replaying them
would raise a cassette miss. A demo that required a paid key would not be a demo anybody could
run, and one that silently presented a stand-in as a model's judgement would be the overclaim this
project exists to avoid.

**Nothing here is reachable from the running application.** It is invoked by a command, against a
database whose name the fixture loader's disposability check has already approved.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import re
import uuid
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.classification.service import run_classification
from ledger_exception_control_plane.db.control import (
    ApprovalDecision,
    ConfidenceBand,
    ExceptionRecord,
    TreatmentCode,
)
from ledger_exception_control_plane.db.models import LedgerEntry
from ledger_exception_control_plane.ingest.parser import SETTLEMENT_COLUMNS
from ledger_exception_control_plane.ingest.service import ingest
from ledger_exception_control_plane.ledger import (
    Fault,
    FaultInjectingLedger,
    LedgerUnreachableError,
    NonIdempotentLedger,
    SimulatedLedger,
)
from ledger_exception_control_plane.llm.port import ProviderId
from ledger_exception_control_plane.llm.schema import (
    EvidenceRef,
    ProposalPrompt,
    TreatmentProposal,
)
from ledger_exception_control_plane.llm.service import propose_for_exception
from ledger_exception_control_plane.matching.service import run_matching
from ledger_exception_control_plane.money import (
    DEMO_LEDGER_CONTEXT,
    AdjustmentInstruction,
    ExceptionFacts,
    compute_adjustment,
)
from ledger_exception_control_plane.operations.approval import record_decision
from ledger_exception_control_plane.operations.dispatcher import dispatch_once
from ledger_exception_control_plane.operations.reconcile import reconcile_once
from ledger_exception_control_plane.operations.retry import DeadLetterReason, dead_letter
from ledger_exception_control_plane.operations.service import enqueue_posting
from ledger_exception_control_plane.security import Principal, Role

__all__ = [
    "DemoSummary",
    "bootstrap_demo",
    "reset_demo",
    "run_bootstrap",
    "run_seed",
    "seed_demo",
]

#: Matches an evidence identifier as it appears in the prompt the system builds. The pack's
#: identifiers are UUIDs and they are in the user message so that a model can cite them.
_EVIDENCE_ID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: The instant the whole demonstration is dated at.
#:
#: Fixed rather than "now", for the reason the reliability layer is built on: a demonstration whose
#: rows move every time it is seeded cannot be described in a README, and a reviewer comparing what
#: they see with what the documentation says would find a different database each run.
EPOCH: Final = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

#: The principals the demonstration acts as. They must exist in the configured registry for the
#: console to authenticate as them; `docs/` records the tokens' *names*, never their values.
#: A real :class:`Principal`, not a name, because ``record_decision`` takes the authenticated
#: actor and checks its authority — ``principal.may_authorise()`` is among the first refusals
#: in that function. Passing a bare string would have bypassed the role check at the type
#: level, which is precisely the gate 5.1 exists to enforce; mypy refused it, correctly.
CONTROLLER: Final = Principal(id="controller-a", role=Role.CONTROLLER)

#: What is recorded as the model call's processing region. §11 makes it part of the audit
#: contract; no call is made here, so the honest value states that rather than naming a region.
STAND_IN_JURISDICTION: Final = "none: no model call was made"

#: The settlement file the demonstration ingests.
#:
#: **Constructed against the classifier's actual rules, not against a guess.** The first version of
#: this payload produced four exceptions and priced none of them, because every one classified
#: ``unclassified`` — and only ``chargeback_reversal`` and ``cross_period_refund`` have a configured
#: account (ADR-047). A demo that raised exceptions nobody could act on would have shown a console
#: with an empty approval path, which is the opposite of the point.
#:
#: So the rows come in **pairs**, because both priceable rules require a *reconciled counterpart*
#: on the same order:
#:
#: * ``reversal_of_booked_chargeback`` fires when this row is a declared ``chargeback_reversal``
#:   with a positive amount and exactly one movement on the same order is a declared ``chargeback``
#:   the ledger already booked. So each reversal is preceded by a chargeback that matches the
#:   opening snapshot exactly and therefore reconciles.
#: * ``refund_of_booked_capture_across_periods`` fires when this row is a declared ``refund`` with a
#:   negative amount, exactly one booked ``capture`` on the same order is its exact negation, and
#:   the two settle in different calendar months. Hence a May capture and a June refund.
#:
#: The last three rows are deliberately *not* priceable — a fee split reported across two rows, and
#: a capture with no merchant reference. They classify to ``fee_split`` and ``unclassified``, for
#: which the account policy configures nothing, so the correct outcome is escalation. **A demo
#: showing only the happy path would misrepresent the honest majority case**, which is that most
#: residuals are referred to a human rather than posted.
PAYLOAD: Final = (
    "\n".join(
        (
            ",".join(SETTLEMENT_COLUMNS),
            # Reconciled counterparts: these match the opening snapshot and clear deterministically.
            "PSP-DEMO-CB1A,ORD-CB1,chargeback,-780.50,EUR,2026-05-20,,,,chargeback raised",
            "PSP-DEMO-CB2A,ORD-CB2,chargeback,-455.00,EUR,2026-05-22,,,,chargeback raised",
            "PSP-DEMO-CB3A,ORD-CB3,chargeback,-612.00,EUR,2026-05-25,,,,chargeback raised",
            "PSP-DEMO-RF1A,ORD-RF1,capture,450.00,EUR,2026-05-15,,,,capture settled",
            # The residual the console is built to work: two reversals and a cross-period refund,
            # each of which the calculator can price.
            "PSP-DEMO-CB1B,ORD-CB1,chargeback_reversal,780.50,EUR,2026-06-03,,,,reversal received",
            "PSP-DEMO-CB2B,ORD-CB2,chargeback_reversal,455.00,EUR,2026-06-04,,,,reversal received",
            # Approved and priced but deliberately NOT dispatched: this is what the console's
            # "inject a crash" control acts on. Without a pending posting there is nothing for a
            # visitor to fault, and `dispatch_once` correctly refuses an operation already
            # finished — so the demonstration needs one held in reserve.
            "PSP-DEMO-CB3B,ORD-CB3,chargeback_reversal,612.00,EUR,2026-06-07,,,,awaiting dispatch",
            "PSP-DEMO-RF1B,ORD-RF1,refund,-450.00,EUR,2026-06-02,,,,refund of a may capture",
            # Residual the system must refuse to price, and escalate instead.
            "PSP-DEMO-FEE1,ORD-FEE,capture,1310.25,EUR,2026-06-05,,,,capture net of fees",
            "PSP-DEMO-FEE2,ORD-FEE,fee,-18.75,EUR,2026-06-05,,,,psp fee",
            "PSP-DEMO-NOREF,,capture,95.40,EUR,2026-06-06,,,,no merchant reference",
        )
    )
    + "\n"
)

#: The counterparty's opening position: exactly the three movements that must reconcile.
#:
#: Written directly because it is the *ledger's* data — this system reconciles against a general
#: ledger it does not own and has no path to create. Amounts, currency and dates mirror the three
#: counterpart rows above, because a counterpart that did not match would leave the reversal with
#: no booked chargeback to point at and the rule would correctly decline to fire.
OPENING_ENTRIES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("GL-DEMO-CB1", "4900", "-780.50", "2026-05-20"),
    ("GL-DEMO-CB2", "4900", "-455.00", "2026-05-22"),
    ("GL-DEMO-CB3", "4900", "-612.00", "2026-05-25"),
    ("GL-DEMO-RF1", "4100", "450.00", "2026-05-15"),
)


@dataclasses.dataclass(frozen=True, slots=True)
class DemoSummary:
    """What one seeding produced. Counts only, so a caller can report without re-querying."""

    batches: int
    lines: int
    matched: int
    exceptions: int
    proposed: int
    evidence: int
    approved: int
    priced: int
    dispatched: int
    ambiguous: int
    dead_lettered: int
    in_recovery: int
    awaiting_dispatch: int
    audit_events: int

    def as_lines(self) -> tuple[str, ...]:
        return (
            f"batches ingested             {self.batches}",
            f"settlement lines             {self.lines}",
            f"cleared deterministically    {self.matched}",
            f"exceptions raised            {self.exceptions}",
            f"treatments proposed          {self.proposed}  (stand-in, not a model)",
            f"evidence records             {self.evidence}",
            f"approved by a human          {self.approved}",
            f"priced deterministically     {self.priced}",
            f"posted to the ledger         {self.dispatched}",
            f"left ambiguous (UNKNOWN)     {self.ambiguous}",
            f"dead-lettered                {self.dead_lettered}",
            f"open in manual recovery      {self.in_recovery}",
            f"awaiting dispatch            {self.awaiting_dispatch}  (the fault-injection target)",
            f"audit events written         {self.audit_events}",
        )


class _StandInProposer:
    """A proposer that answers without a model, and says so in the answer.

    Satisfies :class:`~..llm.port.TreatmentProposer` structurally, so the demonstration exercises
    the **real** flow — evidence assembly, prompt construction, citation validation, persistence and
    the audit event — with only the provider call replaced. That is the part a demo can honestly
    stand in for; the rest is the system.

    **Its honesty is in the two fields the console displays.** ``model_id`` is ``stand-in``, not a
    vendor identifier, so the persisted proposal cannot later be mistaken for a measurement of a
    model. And the rationale opens by stating what produced it, because the console renders that
    text verbatim as model provenance — the same discipline ``tests/cassette_builder.py`` applies to
    its synthesised answers, and for the same reason.

    It proposes ``REBOOK`` with ``MEDIUM`` confidence and cites **one** evidence identifier, read
    out of the prompt's own text. Reading it from the prompt is not a shortcut: the identifiers are
    in the user message precisely so a model can cite them, so this is the same channel a real
    adapter's answer travels down, and it keeps the citation inside the subset rule 3.3 enforces.
    It is not a judgement about the case.
    """

    provider: Final = ProviderId.ANTHROPIC
    model_id: Final = "stand-in"
    model_version: Final = "stand-in"

    async def propose(self, prompt: ProposalPrompt) -> TreatmentProposal:
        found = _EVIDENCE_ID.search(prompt.user)
        return TreatmentProposal(
            treatment=TreatmentCode.REBOOK,
            confidence=ConfidenceBand.MEDIUM,
            rationale=(
                "Not produced by a model. This demonstration runs offline with no provider "
                "credential, so a stand-in proposer supplies a valid proposal in order to exercise "
                "evidence assembly, citation validation, persistence and the audit trail. The "
                "treatment shown here is fixed, not inferred."
            ),
            evidence_refs=((EvidenceRef(evidence_id=found.group(0)),) if found is not None else ()),
            abstained=False,
        )


#: Every table the demonstration writes, deepest first, so a re-seed starts from a clean slate.
#:
#: **A demo must be repeatable, and `alembic downgrade base` cannot make it so.** The 4.4 migration
#: deliberately *refuses* to downgrade while `reconcile` or `recover` audit events exist, because
#: `audit_event` is append-only and the alternative is a migration destroying history on an
#: operator's behalf. This seeder writes both verbs, so a second `make demo` would meet that
#: refusal. Clearing the rows here is the honest way round it: the schema stays, the history of a
#: *demonstration* is discarded on purpose, and the refusal keeps protecting a real deployment.
_RESET_ORDER: Final[tuple[str, ...]] = (
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

#: Append-only by trigger, so clearing them means suspending a control this system enforces.
_APPEND_ONLY: Final[tuple[tuple[str, str], ...]] = (
    ("audit_event", "audit_event_append_only_row"),
    ("reconciliation_query", "reconciliation_query_append_only_row"),
)


async def reset_demo(engine: AsyncEngine) -> None:
    """Empty every table the demonstration writes. Caller has already proved the target disposable.

    Each trigger suspension is its own transaction. ``ALTER TABLE … DISABLE TRIGGER`` outside one
    commits immediately, so a ``DELETE`` that raised between the disable and the enable would leave
    the append-only control switched **off** — and a control that is off while tests and the console
    still assert it would be worse than the rows it was protecting.
    """
    async with AsyncSession(engine) as session:
        for table, trigger in _APPEND_ONLY:
            async with session.begin():
                await session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
                await session.execute(text(f"DELETE FROM {table}"))
                await session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
        async with session.begin():
            for table in _RESET_ORDER:
                await session.execute(text(f"DELETE FROM {table}"))


async def _opening_ledger(engine: AsyncEngine) -> None:
    """The counterparty's position, before anything is reconciled against it."""
    async with AsyncSession(engine) as session, session.begin():
        for external_ref, account_code, amount, booked_on in OPENING_ENTRIES:
            session.add(
                LedgerEntry(
                    id=uuid.uuid4(),
                    external_ref=external_ref,
                    account_code=account_code,
                    amount=decimal.Decimal(amount),
                    currency="EUR",
                    booked_at=dt.datetime.fromisoformat(booked_on).replace(tzinfo=dt.UTC),
                    description="opening position",
                )
            )


async def _count(engine: AsyncEngine, table: str, where: str = "TRUE") -> int:
    async with AsyncSession(engine) as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))
        return int(result.scalar_one())


async def bootstrap_demo(engine: AsyncEngine) -> DemoSummary | None:
    """Seed the demonstration **only if it is not already there**. ``None`` when it already is.

    The difference from :func:`seed_demo` is the whole reason this exists, and it is a deployment
    concern rather than a local one. `seed_demo` resets before it seeds, which is right on a
    developer's disposable database and wrong on a deployed demonstration: a free-tier container
    scales to zero and cold-starts often, so a reset-on-boot would silently discard whatever a
    visitor had just approved, every time the service woke up.

    So the deployed path asks whether the demonstration exists and leaves it alone if it does.
    Repeated bootstrap is then genuinely idempotent — not "resets to the same state", which
    destroys work, but "makes no change at all".

    Emptiness is judged on `exception` rather than on a settlement batch: a batch could exist from a
    partial run, whereas an exception is the first row the console actually renders, and a database
    holding none has nothing to show regardless of what else is in it.
    """
    if await _count(engine, "exception") > 0:
        return None
    return await seed_demo(engine)


async def seed_demo(engine: AsyncEngine) -> DemoSummary:
    """Ingest, match, classify, approve, price, dispatch — and leave two failures behind.

    Returns counts rather than rows. The console reads the database; this function's job is to
    make the database worth reading, and its return value exists so a command can report what it
    did without a second round of queries.

    **The two deliberate failures are the point of the demonstration.** One posting is dispatched
    through a ledger that commits and then loses the response, so the console shows an operation
    recorded ``UNKNOWN`` that the system refuses to retry — §19.1, reachable in a browser. Another
    is dispatched through a ledger that cannot be reached at all, so bounded retry exhausts and the
    entry dead-letters with the envelope an operator replays it from. A demonstration in which
    everything succeeds would hide the half of the system that matters.
    """
    await reset_demo(engine)
    await _opening_ledger(engine)

    receipt = await ingest(engine, PAYLOAD.encode("utf-8"), source="demo-seed", received_at=EPOCH)
    if not receipt.accepted:
        raise RuntimeError(
            f"the demonstration payload was quarantined: {receipt.quarantine_reason}"
        )

    await run_matching(engine, matched_at=EPOCH)
    await run_classification(engine)

    async with AsyncSession(engine) as session:
        exceptions = list(
            (await session.execute(select(ExceptionRecord).order_by(ExceptionRecord.created_at)))
            .scalars()
            .all()
        )
    if not exceptions:
        raise RuntimeError("no exception was raised, so there is nothing to demonstrate")

    # A proposal for every exception, through the real flow. This is what puts rows in `evidence`
    # and `treatment_proposal_evidence`, and without it the console's evidence panel is empty and
    # its citation column has nothing to say — which is the half of the system the project is about.
    #
    # `region_jurisdiction` is required with no default by design (§11 makes the model call's
    # processing region part of the audit contract and this layer cannot know it). A stand-in
    # answers here, so the honest value is a statement that no call was made.
    proposer = _StandInProposer()
    for exception_row in exceptions:
        await propose_for_exception(
            engine,
            proposer,
            exception_row.id,
            region_jurisdiction=STAND_IN_JURISDICTION,
            proposed_at=EPOCH,
        )

    approved = priced = 0
    ambiguous_ids: list[uuid.UUID] = []
    unreachable_ids: list[uuid.UUID] = []

    # Only the exceptions the calculator can actually price are approved, and they are approved
    # one at a time through the real gate. An exception whose class has no configured account is
    # left open on purpose: the console must show a residual a human has to escalate, because that
    # is the honest majority case and a demo of only the happy path would misrepresent it.
    for exception_row in exceptions:
        line_amount, line_currency, line_date = await _line_facts(engine, exception_row.id)
        instruction_or_refusal = compute_adjustment(
            ExceptionFacts(
                exception_id=exception_row.id,
                classification=exception_row.classification,
                amount=line_amount,
                currency=line_currency,
                value_date=line_date,
                originating_period=None,
            ),
            TreatmentCode.REBOOK,
            DEMO_LEDGER_CONTEXT,
        )
        if not isinstance(instruction_or_refusal, AdjustmentInstruction):
            continue

        async with AsyncSession(engine) as session, session.begin():
            decision = await record_decision(
                session,
                exception_id=exception_row.id,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=uuid.uuid4().hex,
                now=EPOCH,
                treatment=TreatmentCode.REBOOK,
            )
        approved += 1

        async with AsyncSession(engine) as session, session.begin():
            operation = await enqueue_posting(
                session, approval_id=decision.approval_id, instruction=instruction_or_refusal
            )
        priced += 1

        # The first priced operation is dispatched cleanly; the second is faulted so the console
        # has an UNKNOWN to show; the third is made unreachable so it can be dead-lettered.
        # Assigned by position rather than at random so the demonstration is the same every time.
        #
        # **Counted over priced operations, not over exceptions.** The first version branched on
        # the loop index over all exceptions, and since the unpriceable ones are skipped above, the
        # clean-dispatch branch was simply never reached — the seeder reported three priced
        # operations and zero dispatched. A demonstration missing its own happy path.
        position = priced - 1
        if position == 0:
            await dispatch_once(
                engine,
                adjustment_id=operation.adjustment_id,
                adapter=SimulatedLedger(),
                sent_at=EPOCH,
            )
        elif position == 1:
            await dispatch_once(
                engine,
                adjustment_id=operation.adjustment_id,
                adapter=FaultInjectingLedger(
                    SimulatedLedger(), fault=Fault.COMMIT_THEN_LOSE_RESPONSE
                ),
                sent_at=EPOCH,
            )
            ambiguous_ids.append(operation.adjustment_id)
        elif position == 2:
            try:
                await dispatch_once(
                    engine,
                    adjustment_id=operation.adjustment_id,
                    adapter=FaultInjectingLedger(
                        SimulatedLedger(),
                        fault=Fault.UNREACHABLE_BEFORE_FIRST_BYTE,
                        fires=10,
                    ),
                    sent_at=EPOCH,
                )
            except LedgerUnreachableError:
                # Classified NOT_SENT by 4.3's classifier: nothing was applied, so the operation
                # stays retryable rather than settled. It is dead-lettered below through the real
                # dead-letter path, so the console's queue holds an entry with a real envelope.
                unreachable_ids.append(operation.adjustment_id)
        # position 3 and beyond: approved, priced, and left pending on purpose — the target the
        # console's fault-injection control needs.

    # The two failures are driven to the states the console renders, through the real services.
    #
    # Dead-lettering is `dead_letter`, not an INSERT: the envelope's shape, the amount-free check
    # constraint and the state transition are all that function's, and a seeder writing the row
    # itself would produce a queue entry the replay path might not accept.
    for adjustment_id in unreachable_ids:
        async with AsyncSession(engine) as session, session.begin():
            await dead_letter(
                session,
                adjustment_id=adjustment_id,
                reason=DeadLetterReason.ATTEMPTS_EXHAUSTED,
                envelope={
                    "operation_id": await _operation_id(engine, adjustment_id),
                    "adapter": SimulatedLedger().name,
                    "endpoint": "sim://demo/postings",
                    "attempts": 1,
                },
                attempts=1,
                mark_state=True,
                dead_lettered_at=EPOCH,
            )

    # And the ambiguous one is reconciled under a capability that can neither suppress a duplicate
    # nor answer a query, which is §13.5 clause 5: the automatic path stops and an operator takes
    # it. That is what puts a row in `recovery_queue` for the console to work.
    for adjustment_id in ambiguous_ids:
        await reconcile_once(
            engine,
            adjustment_id=adjustment_id,
            adapter=NonIdempotentLedger(),
            now=EPOCH + dt.timedelta(days=1),
        )

    # **Every count is read back from the database, not accumulated in the loop.** An earlier
    # version reported `dead_lettered` from a counter incremented when a dispatch raised, which
    # was a count of unreachable *sends* and not of queue entries — the summary said one and the
    # `dlq` table held none. The system's own discipline applies to its demonstration: measure the
    # state, do not infer it.
    return DemoSummary(
        batches=await _count(engine, "settlement_batch"),
        lines=await _count(engine, "settlement_line"),
        matched=await _count(engine, "match_result"),
        exceptions=len(exceptions),
        proposed=await _count(engine, "treatment_proposal"),
        evidence=await _count(engine, "evidence"),
        approved=approved,
        priced=priced,
        dispatched=await _count(engine, "adjustment", "posting_ref IS NOT NULL"),
        ambiguous=await _count(engine, "outbox", "last_outcome = 'unknown'"),
        dead_lettered=await _count(engine, "dlq"),
        in_recovery=await _count(engine, "recovery_queue"),
        awaiting_dispatch=await _count(engine, "outbox", "state = 'pending' AND attempt_count = 0"),
        audit_events=await _count(engine, "audit_event"),
    )


async def _operation_id(engine: AsyncEngine, adjustment_id: uuid.UUID) -> str:
    """The identifier 4.1 persisted for this adjustment. Read, never re-derived."""
    async with AsyncSession(engine) as session:
        return str(
            (
                await session.execute(
                    text("SELECT operation_id FROM adjustment WHERE id = :i"),
                    {"i": adjustment_id},
                )
            ).scalar_one()
        )


async def _line_facts(
    engine: AsyncEngine, exception_id: uuid.UUID
) -> tuple[decimal.Decimal, str, dt.date]:
    """The settlement facts behind one exception, read from the row the system persisted."""
    async with AsyncSession(engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT l.amount, l.currency, l.value_date FROM settlement_line l "
                    "JOIN exception e ON e.settlement_line_id = l.id WHERE e.id = :exception_id"
                ),
                {"exception_id": exception_id},
            )
        ).one()
    return decimal.Decimal(str(row[0])), str(row[1]), row[2]


def run_bootstrap() -> int:
    """Seed the configured database only if the demonstration is not already present.

    The deployed entrypoint's command. Reports what it found either way, because "already seeded"
    and "seeded 7 exceptions" are different facts about a deploy and a log line saying neither is
    the one you want at three in the morning.

    Keeps :func:`~.fixtures.loader.assert_target_is_disposable`. A deployed demonstration is exactly
    where that guard earns its place: the database name has to match `lecp_(test|demo|fixtures)`, so
    pointing this at anything else refuses rather than writing invented transactions into it.
    """
    import asyncio

    from ledger_exception_control_plane.config import Settings
    from ledger_exception_control_plane.db.engine import create_engine
    from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable

    settings = Settings()
    assert_target_is_disposable(settings)

    async def run() -> DemoSummary | None:
        engine = create_engine(settings)
        try:
            return await bootstrap_demo(engine)
        finally:
            await engine.dispose()

    summary = asyncio.run(run())
    if summary is None:
        print("the demonstration is already seeded; nothing was written")
        return 0
    print("seeded the demonstration database:")
    for line in summary.as_lines():
        print(f"  {line}")
    return 0


def run_seed() -> int:
    """Seed the configured database and report what was left behind. The CLI's whole body.

    Lives here rather than in ``demo/__main__.py`` so the snapshot's import guards stay narrow: that
    module's claim is that the M2 report runs from a generated corpus with no engine and no session,
    and holding the seeding wiring there would have meant widening the guard for the one module the
    guard is about. The CLI calls this and imports nothing else.

    Refuses anything but a disposable database. ``assert_target_is_disposable`` is the fixture
    loader's own check, reused rather than reimplemented: a seeder that dropped its guard would
    write demonstration rows into whatever DSN happened to be exported, and inventing transactions
    in a real ledger is the one thing a finance-ops tool must never do.
    """
    import asyncio

    from ledger_exception_control_plane.config import Settings
    from ledger_exception_control_plane.db.engine import create_engine
    from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable

    settings = Settings()
    assert_target_is_disposable(settings)

    async def run() -> DemoSummary:
        engine = create_engine(settings)
        try:
            return await seed_demo(engine)
        finally:
            await engine.dispose()

    summary = asyncio.run(run())
    print("seeded the demonstration database:")
    for line in summary.as_lines():
        print(f"  {line}")
    print()
    print("start the API with `make up`, then open the console; authenticate as controller-a")
    print("to approve and as operator-a to work the dead-letter and recovery queues.")
    return 0
