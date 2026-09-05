"""Dispatching one posting: write-ahead, send, record (increment 4.2).

The plan asks 4.2 for a *dispatcher*, and for three things to be true of it: dispatch works end to
end, it branches on declared capability, and a crash between the socket write and the response write
leaves a recoverable ``IN_FLIGHT`` record.

**It is a dispatch of one operation, not a loop.** Nothing here polls, sleeps, schedules or decides
when to try again. Every mechanism that would make a loop necessary — exponential backoff, jitter, a
bounded attempt count, a time budget, ``next_attempt_at`` as a policy — is verbatim 4.3's, and the
plan's sequencing note forbids absorbing a later increment's work for convenience. What 4.2 owes is
a unit that can be driven once, and driven again, with the identifier unchanged; what drives it is
4.3's decision.

**The write-ahead record is committed in its own transaction, strictly before the send** (§12.1.1).
That ordering is the whole mechanism: without it a crash between the socket write and the response
write is indistinguishable from a crash *before* the write, and the system would hold no evidence a
send occurred at all. The row is therefore committed and not merely flushed — a flush inside the
caller's transaction would vanish on the same rollback that loses everything else, which is exactly
the case it exists to survive.

**Capability is read, never inferred** (§13.4). The branch below is a function of the capabilities
record, obtained through :func:`~...ledger.conformance.capabilities_for` so that an unverified claim
has already been downgraded to ``NONE``. Nothing here looks at the adapter's class, its name, or
which exception it raised.

**Nothing here retries, reconciles or recovers, and "does not retry" is not the same as
"cannot".** An ``Unknown`` is recorded as ``Unknown`` and the dispatch stops. The first version
stopped there and left the door open: called again, it would have sent again, from an ambiguous
state, with nothing consulted. That is §13.5 clause 3's defect — *"Do not blindly retry an
irreversible financial write"* — reachable through a function this increment shipped, and three
reviewers walked through it.

So the gate below is the **refusal half** of §13.5's capability branch, and only the refusal half.
Where capability cannot suppress or detect a duplicate, a further send after an ambiguous outcome
is refused outright. Where it can, the send is permitted and this module does nothing else: the
*bounds* on that permission — ``idempotency_window``, ``idempotency_scope``, the scheduled
reconciliation, the manual-recovery queue — are 4.4's, and none of them is built here. Refusing
without them is not implementing them; it is declining to act without them, which is what
*"otherwise the automatic path stops"* asks for.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.db.control import (
    Adjustment,
    AttemptState,
    DispatchState,
    Outbox,
    PostingAttempt,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
from ledger_exception_control_plane.ledger.conformance import capabilities_for
from ledger_exception_control_plane.ledger.port import (
    Confirmed,
    LedgerAdapter,
    LedgerAdapterCapabilities,
    PartiallyApplied,
    PostingInstruction,
    PostingOutcome,
    QueryableLedgerAdapter,
    Rejected,
    Throttled,
    Unknown,
)

__all__ = [
    "DispatchRefusedError",
    "DispatchResult",
    "ResendDecision",
    "dispatch_once",
    "outcome_code",
    "reconciliation_is_available",
    "resend_decision",
]

#: Outcomes that end a dispatch. §13.3: the dispatcher will not initiate a second send for an
#: operation already in a **known terminal state**, and these two are that state — the only two the
#: ``outbox`` table will accept on a settled row.
_TERMINAL: frozenset[OutcomeCode] = frozenset({OutcomeCode.CONFIRMED, OutcomeCode.REJECTED})

#: Recorded outcomes that leave the ledger's state genuinely undetermined.
#:
#: ``Throttled`` is deliberately **not** here: §10.1 splits it out precisely because it is a
#: scheduling signal rather than a declination, and §15 places a 429 arriving *after* send in
#: ``UNKNOWN`` instead — so a recorded ``throttled`` means the request was turned away before it
#: could be applied, and a further send is not a re-send of an irreversible write.
_AMBIGUOUS: frozenset[OutcomeCode] = frozenset({OutcomeCode.UNKNOWN, OutcomeCode.PARTIALLY_APPLIED})


class ResendDecision(enum.StrEnum):
    """What §13.5's capability branch permits for an operation whose outcome is undetermined.

    Three values, one per branch of §13.5 item 3 and item 4, and the mapping is the specification's
    rather than this module's:

    - ``PERMITTED`` — ``idempotency == ENFORCES_KEY``. *"Automatic retry from `UNKNOWN` is permitted
      only where capability allows the duplicate to be suppressed or detected."* The bounds on that
      permission are 4.4's.
    - ``RECONCILE_FIRST`` — ``posting_identity_query == BY_OPERATION_ID`` and no enforcement.
      §13.5's table answers by *querying*, not by sending: *"no re-send; reconcile by querying X"*.
      4.2 refuses the send and names the route; 4.4 walks it.
    - ``MANUAL_RECOVERY`` — neither. *"Otherwise route to manual recovery … the automatic path
      stops."*
    """

    PERMITTED = "permitted"
    RECONCILE_FIRST = "reconcile_first"
    MANUAL_RECOVERY = "manual_recovery"


def resend_decision(capabilities: LedgerAdapterCapabilities) -> ResendDecision:
    """The branch, as a pure function of the **effective** capability record.

    A pure function of declared data, which is §13.4's requirement in as many words: *"Capability is
    data the system reads and branches on, never an assumption baked into the dispatcher."* Nothing
    here looks at an adapter's class, its name, or which exception it raised.

    ``ENFORCES_KEY`` is checked, never "has an idempotency key" — ``ACCEPTS_KEY`` is a provider that
    echoes a header and may ignore it, which §13.4 says is indistinguishable from having none.
    """
    if capabilities.suppresses_duplicates:
        return ResendDecision.PERMITTED
    if capabilities.queryable_by_operation_id:
        return ResendDecision.RECONCILE_FIRST
    return ResendDecision.MANUAL_RECOVERY


class DispatchRefusedError(Exception):
    """The dispatch was not attempted, and no socket write occurred."""


@dataclasses.dataclass(frozen=True, slots=True)
class DispatchResult:
    """What one dispatch did.

    ``outcome`` is the adapter's own value, carried whole rather than reduced to the persisted enum,
    so a caller keeps the payload — ``Throttled``'s ``retry_after``, ``Unknown``'s ``detail`` — that
    4.3 and 4.4 will need. The schema stores the variant *name*; the payloads have no columns and
    inventing some would be those increments' design decisions taken early.
    """

    adjustment_id: uuid.UUID
    operation_id: str
    attempt_no: int
    outcome: PostingOutcome
    settled: bool
    capabilities: LedgerAdapterCapabilities

    #: What a further send would be permitted to do if this outcome turns out to be ambiguous.
    #: Reported so a caller — and 4.3's retry, when it exists — reads the branch rather than
    #: re-deriving it.
    resend: ResendDecision


def outcome_code(outcome: PostingOutcome) -> OutcomeCode:
    """Map an adapter outcome onto the persisted vocabulary.

    Every arm of the closed union is named, and the wildcard is not decoration: without it the
    function *returned ``None``* for anything else, so a buggy or hostile adapter answering with a
    string, ``None`` or an unknown class would have had that ``None`` written into an outcome
    column — a dispatch recorded as finished with no record of what the ledger said. A reviewer
    reproduced it. The type checker still catches a new variant at compile time; this catches
    everything that reaches the function at runtime regardless of what the annotations promised.
    """
    match outcome:
        case Confirmed():
            return OutcomeCode.CONFIRMED
        case Rejected():
            return OutcomeCode.REJECTED
        case Throttled():
            return OutcomeCode.THROTTLED
        case Unknown():
            return OutcomeCode.UNKNOWN
        case PartiallyApplied():
            return OutcomeCode.PARTIALLY_APPLIED
        case _:
            raise TypeError(
                f"the adapter returned {type(outcome).__name__}, which is not a PostingOutcome; "
                "an unrecognised answer is not an outcome and must not be recorded as one"
            )


def reconciliation_is_available(adapter: LedgerAdapter) -> bool:
    """Whether this adapter can be asked what happened to an operation.

    **The capability branch 4.2 owes**, and the reason it is a function rather than an
    ``isinstance`` at the call site: §13.4 requires the path to be chosen from the *declared*
    record,
    and the structural check alone would accept an adapter that has a query method but no verified
    claim to answer honestly. Both must hold — the effective capability says ``BY_OPERATION_ID``
    *and* the adapter satisfies the queryable protocol, which is the typed absence §10.1 asks for.

    4.2 reports it; 4.4 acts on it. What a caller does with a `False` here — manual recovery, no
    automatic re-send — is §13.5's branch and is not decided in this module.
    """
    return capabilities_for(adapter).queryable_by_operation_id and isinstance(
        adapter, QueryableLedgerAdapter
    )


async def dispatch_once(
    engine: AsyncEngine, *, adjustment_id: uuid.UUID, adapter: LedgerAdapter, sent_at: dt.datetime
) -> DispatchResult:
    """Attempt one posting for one adjustment.

    Takes an **engine** rather than a session, deliberately: §12.1.1 requires the write-ahead record
    to be committed in a transaction of its own, before the send, and a function handed someone
    else's session cannot commit without also committing whatever that caller had in flight. The
    three transactions here are separate because the specification says they must be.

    ``sent_at`` is a parameter rather than a clock reading, for the reason the whole reliability
    layer is built on: a value this module invented would make the evidence depend on when the code
    ran. The caller supplies it, and a test can supply a fixed one.

    **Concurrency is settled by the write-ahead insert, not by the lock in transaction 1.** That
    lock is released at that transaction's commit, because §12.1.1 requires the attempt record to
    be committed separately and before the send. See transaction 2.
    """
    capabilities = capabilities_for(adapter)

    # -- Transaction 1: read the intent, refuse if it is already finished --------------------
    async with AsyncSession(engine) as session, session.begin():
        row = (
            await session.execute(
                select(Outbox, Adjustment)
                .join(Adjustment, Outbox.adjustment_id == Adjustment.id)
                .where(Outbox.adjustment_id == adjustment_id)
                .with_for_update(of=Outbox)
            )
        ).one_or_none()
        if row is None:
            raise DispatchRefusedError(
                f"no dispatch intent for adjustment {adjustment_id}; nothing was enqueued"
            )
        outbox, adjustment = row
        short = adjustment.operation_id[:12]

        # §13.3, Guarantee 3. Checked *before* the write-ahead record, because a refusal must leave
        # no evidence of a send that never happened.
        if outbox.last_outcome is not None and OutcomeCode(outbox.last_outcome) in _TERMINAL:
            raise DispatchRefusedError(
                f"operation {short}… is already {OutcomeCode(outbox.last_outcome).value}; "
                "a second send is refused (§13.3)"
            )

        # §13.5 clause 3, the refusal half. An operation is ambiguous either because a recorded
        # outcome says so, or because an attempt was sent and never resolved — and the second is the
        # case the first version missed entirely: `last_outcome` is written only when a dispatch
        # completes, so a send that failed mid-flight left the column NULL and the gate blind to the
        # very state §12.1.1 calls `UNKNOWN` by definition.
        unresolved = (
            await session.execute(
                select(func.count())
                .select_from(PostingAttempt)
                .where(
                    PostingAttempt.adjustment_id == adjustment_id,
                    PostingAttempt.state == AttemptState.IN_FLIGHT,
                )
            )
        ).scalar_one()
        recorded_ambiguous = (
            outbox.last_outcome is not None and OutcomeCode(outbox.last_outcome) in _AMBIGUOUS
        )

        if unresolved or recorded_ambiguous:
            decision = resend_decision(capabilities)
            if decision is not ResendDecision.PERMITTED:
                why = (
                    "the adapter can be queried by operation identifier, so the answer is a "
                    "reconciliation rather than another send (4.4)"
                    if decision is ResendDecision.RECONCILE_FIRST
                    else "the adapter can neither suppress nor detect a duplicate, so this routes "
                    "to manual recovery (4.4)"
                )
                raise DispatchRefusedError(
                    f"operation {short}… is in an undetermined state and a further send is "
                    f"refused: {why}. §13.5 permits an automatic re-send only under ENFORCES_KEY."
                )

        operation_id = adjustment.operation_id
        instruction = PostingInstruction(
            adjustment_id=adjustment.id,
            amount=adjustment.amount,
            currency=adjustment.currency,
            account_code=adjustment.account_code,
            period=adjustment.period,
        )
        attempt_no = (
            int(
                (
                    await session.execute(
                        select(func.coalesce(func.max(PostingAttempt.attempt_no), 0)).where(
                            PostingAttempt.adjustment_id == adjustment_id
                        )
                    )
                ).scalar_one()
            )
            + 1
        )

    # -- Transaction 2: the write-ahead attempt record, committed before any send ------------
    #
    # **This insert, and not the row lock above, is what makes concurrent dispatch safe.** The
    # `FOR UPDATE` in transaction 1 is released when transaction 1 commits, which §12.1.1 forces:
    # the write-ahead record must be committed in a transaction of its own, so there is no way to
    # hold a lock from the gate across the send. Two dispatchers can therefore both pass the gate,
    # and a reviewer walked through exactly that. What they cannot both do is insert attempt N —
    # `uq_posting_attempt_adjustment_no` is unique on `(adjustment_id, attempt_no)`, so the second
    # loses at the database and never reaches the socket.
    #
    # Refused rather than retried with N+1: a caller that quietly took the next attempt number
    # would be sending a second time precisely because someone else was already sending, which is
    # the duplicate this whole increment exists to prevent.
    try:
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                PostingAttempt(
                    adjustment_id=adjustment_id,
                    operation_id=operation_id,
                    attempt_no=attempt_no,
                    sent_at=sent_at,
                    state=AttemptState.IN_FLIGHT,
                )
            )
    except IntegrityError as exc:
        raise DispatchRefusedError(
            f"attempt {attempt_no} for operation {operation_id[:12]}… already exists; another "
            "dispatcher is sending it and this one stops rather than sending a second time"
        ) from exc
    # Committed. From here until the outcome is recorded, a crash leaves an `in_flight` row with no
    # outcome — which §12.1.1 defines as `UNKNOWN`, and which is the evidence 4.4 recovers from.

    outcome = await adapter.post(operation_id, instruction)
    code = outcome_code(outcome)

    # -- Transaction 3: record what happened -------------------------------------------------
    async with AsyncSession(engine) as session, session.begin():
        attempt = (
            await session.execute(
                select(PostingAttempt).where(
                    PostingAttempt.adjustment_id == adjustment_id,
                    PostingAttempt.attempt_no == attempt_no,
                )
            )
        ).scalar_one()
        attempt.state = AttemptState.RESOLVED
        attempt.outcome = code
        attempt.resolved_at = sent_at
        if isinstance(outcome, Confirmed):
            attempt.posting_ref = outcome.posting_ref

        intent = (
            await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
        ).scalar_one()
        intent.last_outcome = code
        intent.attempt_count = attempt_no
        settled = code in _TERMINAL
        if settled:
            # Only `confirmed` and `rejected` may settle a row — the database refuses the rest, and
            # that refusal is what stops an `UNKNOWN` being quietly filed as done.
            intent.state = DispatchState.SETTLED

        if isinstance(outcome, Confirmed):
            # "posting reference recorded on Confirmed". Both columns: the attempt row is the
            # per-send evidence and carries a check constraint tying the reference to an applied
            # outcome, while `adjustment.posting_ref` is the durable answer to "was this operation
            # applied, and where" without walking the attempt history.
            applied = (
                await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
            ).scalar_one()
            applied.posting_ref = outcome.posting_ref

    return DispatchResult(
        adjustment_id=adjustment_id,
        operation_id=operation_id,
        attempt_no=attempt_no,
        outcome=outcome,
        settled=code in _TERMINAL,
        capabilities=capabilities,
        resend=resend_decision(capabilities),
    )
