"""Bounded retry, scheduling and the dead-letter queue (increment 4.3).

`IMPLEMENTATION_PLAN.md` §4.3: *"Failure has a floor and a way back."* The floor is here; the way
back is the replay CLI in :mod:`~.__main__`.

**This module retries a transport failure. It never retries a financial ambiguity.** §15 draws the
line and this module is the enforcement:

    A timeout or connection reset after the request was sent is **NOT** a transient retry case. …
    Treating an ambiguous financial write as an ordinary transient retry is precisely the defect
    this design exists to prevent.

So the due-work query does not *decline* to retry an ambiguous operation after selecting it — it
cannot see one. An outbox row whose last outcome is ``unknown`` or ``partially_applied``, or which
has an unresolved ``in_flight`` attempt from a crash mid-send, is excluded by the predicate itself.
That is deliberate: a filter applied after selection is one ``if`` away from being wrong, and this
is the ``if`` that would double-post.

**Why the schedule is written here and not in the dispatcher.** ``dispatch_once`` is a unit: it
sends one operation and records what came back. A guard test asserts it contains no loop, imports
nothing that schedules, and has no parameter that could express a batch — and two more assert it
writes no dead letter and sets no ``next_attempt_at``. All three stay true. Everything in this
module sits *above* that call, which is what keeps "does one send" and "decides whether to send
again" separable, and separable is what let 4.2 be reviewed on its own terms.

**Both bounds, because either alone is unbounded in the other dimension.** §15 requires *"Bounded
maximum attempts"* and, separately, *"Total attempt budget is bounded in time as well as count, so
an entry cannot retry indefinitely."* A count-only bound with a long backoff can still run for
days; a time-only bound with a short backoff can still hammer. Both are checked before every
schedule.

**The parameters are configuration, and the specification says so rather than giving values.** §15:
*"base delay, multiplier and cap are configuration."* It names no number, and neither did anything
else in this repository before this increment — not the spec, not an ADR, not ``.env.example``. The
defaults on :class:`RetryPolicy` are therefore **declared project decisions, not empirical
findings**; ADR-055 §4 records them as such, ``.env.example`` documents the knobs, and every one is
overridable through ``Settings``. They are stated in one place so a reviewer can disagree with a
number without hunting for it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import random
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.audit import (
    correlation_for_adjustment,
    emit,
    posting_audit_outcome,
)
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.control import (
    Adjustment,
    AttemptState,
    AuditTool,
    DeadLetter,
    DispatchState,
    Outbox,
    PostingAttempt,
    ReplayState,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
from ledger_exception_control_plane.ledger.port import (
    LedgerAdapter,
    PostingOutcome,
    Throttled,
)
from ledger_exception_control_plane.ledger.transport import (
    AdapterCallError,
    AttributedAdapter,
    TransportVerdict,
    classify_transport_failure,
)
from ledger_exception_control_plane.operations.dispatcher import (
    POST_SCOPE,
    DispatchRefusedError,
    dispatch_once,
    outcome_code,
)

__all__ = [
    "AMBIGUOUS_OUTCOMES",
    "DeadLetterReason",
    "ReplayOutcome",
    "ReplayReport",
    "RetryPolicy",
    "RetryReport",
    "RetryVerdict",
    "attempt_one",
    "backoff_ceiling",
    "backoff_delay",
    "dead_letter",
    "due_adjustments",
    "pending_dead_letters",
    "replay_dead_letter",
    "run_due_once",
]


#: Outcomes that put an operation beyond the ordinary retry path, permanently as far as 4.3 is
#: concerned.
#:
#: Identical in membership to the dispatcher's ``_AMBIGUOUS`` and deliberately declared again rather
#: than imported: that one gates a *second send* inside one dispatch, this one gates *selection for
#: retry*, and a future increment could legitimately change one without the other. A reviewer asked
#: why they were not shared; the answer is that sharing them would couple two decisions that only
#: happen to agree today. A test asserts they agree, so the duplication cannot drift silently.
AMBIGUOUS_OUTCOMES: frozenset[OutcomeCode] = frozenset(
    {OutcomeCode.UNKNOWN, OutcomeCode.PARTIALLY_APPLIED}
)


class DeadLetterReason(enum.StrEnum):
    """Why an envelope stopped being retried. A closed vocabulary, not free text.

    The precedent is the quarantine reason established at M2.1: a stored failure reason is a closed
    set, bounded in length and carrying none of the offending input, so it can be counted, alerted
    on and rendered without becoming a channel for whatever the failure contained.
    """

    #: The attempt ceiling was reached. §15: *"Bounded maximum attempts; on exhaustion the envelope
    #: moves to the DLQ with reason and attempt count."*
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"

    #: The wall-clock budget was reached first. §15's second, independent bound. Kept distinct from
    #: the count because the operator response differs: a count exhaustion usually means the
    #: endpoint is wrong, a budget exhaustion that it was down for a long time.
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"

    #: The ledger declined. §15: *"A 4xx other than 429 is `Rejected` and goes straight to DLQ —
    #: retrying a validation error is a defect."* No attempt is wasted first.
    TERMINAL_REJECTION = "terminal_rejection"


class RetryVerdict(enum.StrEnum):
    """What this module decided to do with one operation, reported rather than inferred."""

    #: Rescheduled. ``next_attempt_at`` was written.
    SCHEDULED = "scheduled"
    #: Moved to the dead-letter queue.
    DEAD_LETTERED = "dead_lettered"
    #: The ledger gave a terminal answer and the dispatcher settled it. Nothing further to do.
    SETTLED = "settled"
    #: Ambiguous, or refused by a dispatcher gate. Left exactly as it was for 4.4.
    HELD = "held"


@dataclasses.dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The two bounds and the backoff curve, as one frozen value passed as an argument.

    A frozen dataclass rather than ambient configuration, following the money policy's precedent:
    the value a decision was made under is visible in the call, and a test can pass a different one
    without touching the environment. :meth:`from_settings` is the bridge for the deployed path.
    """

    base_delay: dt.timedelta
    multiplier: float
    cap: dt.timedelta
    max_attempts: int
    time_budget: dt.timedelta

    def __post_init__(self) -> None:
        if self.base_delay <= dt.timedelta(0):
            raise ValueError("base_delay must be positive; a zero base makes backoff a no-op")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be at least 1.0; below it the backoff shrinks")
        if self.cap < self.base_delay:
            raise ValueError(
                "cap must be at least base_delay, or the first delay is already capped"
            )
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1; zero would forbid the first send")
        if self.time_budget <= dt.timedelta(0):
            raise ValueError("time_budget must be positive")

    @classmethod
    def from_settings(cls, settings: Settings) -> RetryPolicy:
        return cls(
            base_delay=dt.timedelta(seconds=settings.retry_base_delay_seconds),
            multiplier=settings.retry_multiplier,
            cap=dt.timedelta(seconds=settings.retry_cap_seconds),
            max_attempts=settings.retry_max_attempts,
            time_budget=dt.timedelta(seconds=settings.retry_time_budget_seconds),
        )


def backoff_ceiling(policy: RetryPolicy, attempt_no: int) -> dt.timedelta:
    """The upper bound of the delay after ``attempt_no`` attempts, before jitter.

    ``base * multiplier ** (attempt_no - 1)``, capped. Separated from :func:`backoff_delay` so the
    bound can be asserted without a random source at all — a "backoff bounds" test that had to draw
    a sample to learn the bound would be testing the sample.

    Computed in float seconds and converted once. ``timedelta`` multiplication by a float rounds to
    the microsecond at every step, so repeated multiplication would accumulate a drift that makes
    the ceiling depend on how it was reached.
    """
    if attempt_no < 1:
        raise ValueError("attempt_no counts sends and starts at 1")
    ceiling = policy.base_delay.total_seconds() * policy.multiplier ** (attempt_no - 1)
    return dt.timedelta(seconds=min(ceiling, policy.cap.total_seconds()))


def backoff_delay(policy: RetryPolicy, attempt_no: int, rng: random.Random) -> dt.timedelta:
    """Full jitter: a uniform draw from ``[0, ceiling]``.

    **Full jitter rather than equal or decorrelated**, and the choice is declared rather than
    assumed — §15 says only *"with jitter"*. Full jitter is the variant that actually spreads a
    thundering herd: equal jitter keeps half of each delay fixed, so a fleet that failed together
    retries in a tight band around the same instant, which is the failure mode jitter exists to
    prevent. Its cost is that a delay may be very short; the ceiling still grows exponentially, so
    the *expected* delay still doubles.

    ``rng`` is a parameter for the same reason ``sent_at`` is one on ``dispatch_once``: a module
    that reached for a global random source would make its own bounds untestable, and the retry
    fence forbids exactly that reach.
    """
    ceiling = backoff_ceiling(policy, attempt_no)
    return dt.timedelta(seconds=rng.uniform(0.0, ceiling.total_seconds()))


@dataclasses.dataclass(frozen=True, slots=True)
class RetryReport:
    """What one pass did, per operation. Returned rather than logged, so a test can assert on it."""

    adjustment_id: uuid.UUID
    operation_id: str
    verdict: RetryVerdict
    attempt_no: int
    #: Present only when the verdict is :attr:`RetryVerdict.SCHEDULED`.
    next_attempt_at: dt.datetime | None = None
    #: Present only when the verdict is :attr:`RetryVerdict.DEAD_LETTERED`.
    reason: DeadLetterReason | None = None
    #: The transport classifier's answer, when the adapter raised rather than answered.
    transport: TransportVerdict | None = None


async def due_adjustments(
    session: AsyncSession, *, now: dt.datetime, limit: int
) -> Sequence[uuid.UUID]:
    """Outbox rows eligible for a send, claimed with ``FOR UPDATE SKIP LOCKED``.

    **Four conditions, and the last two are the ones that matter.**

    1. ``state = 'pending'`` — settled and dead-lettered rows are finished. This also matches the
       partial index the schema has carried since M1.2.
    2. ``next_attempt_at IS NULL OR next_attempt_at <= now`` — NULL means *due now*, which is the
       reading a freshly enqueued row requires: ``enqueue_posting`` writes no schedule, and a
       predicate that treated NULL as "not yet" would silently never dispatch anything.
    3. The last outcome is not ambiguous. §15 again: an ``UNKNOWN`` is not a transient failure and
       never enters this path.
    4. No unresolved ``in_flight`` attempt exists. This is the crash-mid-send case §12.1.1 defines
       as ``UNKNOWN`` by construction, and acceptance criterion 8g requires that recovery *"never
       retries it"*. It is a separate condition from (3) because a crash leaves no recorded outcome
       at all — ``last_outcome`` is still NULL — which is precisely how it would slip past a filter
       that only read the column.

    ``SKIP LOCKED`` rather than blocking, following the residual claim at 4.1: two runners want
    *different* work, so the loser should move on rather than wait. The lock is released when the
    caller's transaction commits, which is before any send — holding it across ``dispatch_once``
    would deadlock against that function's own ``FOR UPDATE`` on the same row.
    """
    unresolved = (
        select(func.count())
        .select_from(PostingAttempt)
        .where(
            PostingAttempt.adjustment_id == Outbox.adjustment_id,
            PostingAttempt.state == AttemptState.IN_FLIGHT,
        )
        .scalar_subquery()
    )

    rows = await session.execute(
        select(Outbox.adjustment_id)
        .where(
            Outbox.state == DispatchState.PENDING,
            (Outbox.next_attempt_at.is_(None)) | (Outbox.next_attempt_at <= now),
            (Outbox.last_outcome.is_(None))
            | (Outbox.last_outcome.not_in([code.value for code in AMBIGUOUS_OUTCOMES])),
            unresolved == 0,
        )
        .order_by(Outbox.next_attempt_at.nulls_first(), Outbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Outbox)
    )
    return list(rows.scalars().all())


async def _first_sent_at(session: AsyncSession, adjustment_id: uuid.UUID) -> dt.datetime | None:
    """When this operation was first sent, which is where the time budget starts.

    **The first attempt, not the outbox row's creation.** The budget bounds *retrying*; an entry
    that sat unsent because no runner ran has not been retrying, and starting the clock at creation
    would dead-letter it without a single failure having occurred.
    """
    return (
        await session.execute(
            select(func.min(PostingAttempt.sent_at)).where(
                PostingAttempt.adjustment_id == adjustment_id
            )
        )
    ).scalar_one_or_none()


def _envelope(
    *,
    operation_id: str,
    adjustment_id: uuid.UUID,
    adapter_name: str,
    attempt_no: int,
    last_outcome: OutcomeCode,
    transport: TransportVerdict | None,
    first_sent_at: dt.datetime | None,
) -> dict[str, object]:
    """What a dead letter carries, and deliberately what it does not.

    **No amount, no currency, no account.** ``dlq.envelope`` is guarded by a check constraint that
    rejects fourteen monetary key names outright, and the constraint is right: the envelope exists
    so an operator can find and replay an operation, and replay re-reads the persisted
    ``adjustment`` row. Copying the instruction in would create a second copy of a financial value
    that could drift from the first, which is the failure mode the single-owner rule for amounts
    exists to prevent.

    **No exception message.** ``transport.detail`` is the exception's *class name*. A message can
    carry a URL, a hostname, an authorization header or a token, and this column is read by
    operators and rendered in tooling.
    """
    return {
        "operation_id": operation_id,
        "adjustment_id": str(adjustment_id),
        "adapter": adapter_name,
        "attempt_no": attempt_no,
        "last_outcome": last_outcome.value,
        "transport_class": transport.classification.value if transport else None,
        "transport_cause": transport.cause.value if transport and transport.cause else None,
        "transport_detail": transport.detail if transport else None,
        "first_sent_at": first_sent_at.isoformat() if first_sent_at else None,
    }


async def dead_letter(
    session: AsyncSession,
    *,
    adjustment_id: uuid.UUID,
    reason: DeadLetterReason,
    envelope: dict[str, object],
    attempts: int,
    mark_state: bool,
) -> None:
    """Write the dead letter and, where the outcome left the row unfinished, mark it.

    ``mark_state`` is the difference between the two ways an envelope arrives here, and it is not a
    convenience flag:

    - **Exhaustion.** The last outcome is ``not_sent`` or ``throttled``, which
      ``settled_requires_terminal_outcome`` forbids on a settled row. The row is unfinished, and
      ``DEAD_LETTERED`` — a state the schema has carried since M1.2 with nothing to write it — is
      what it becomes.
    - **Terminal rejection.** ``dispatch_once`` has already settled the row with a terminal outcome,
      which is the truth: the ledger answered. Overwriting ``settled`` with ``dead_lettered`` would
      erase the fact that an answer was received, so the state is left alone and only the dead
      letter is written.

    ``uq_dlq_outbox_id`` means an outbox row dead-letters at most once; a second call for the same
    row raises rather than quietly writing a second envelope.
    """
    intent = (
        await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
    ).scalar_one()

    session.add(
        DeadLetter(
            outbox_id=intent.id,
            envelope=envelope,
            reason=reason.value,
            attempts=attempts,
        )
    )
    if mark_state:
        intent.state = DispatchState.DEAD_LETTERED
    await session.flush()


async def _schedule(session: AsyncSession, *, adjustment_id: uuid.UUID, when: dt.datetime) -> None:
    intent = (
        await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
    ).scalar_one()
    intent.next_attempt_at = when
    await session.flush()


def _budget_exhausted(
    *,
    first_sent_at: dt.datetime | None,
    now: dt.datetime,
    policy: RetryPolicy,
    delay: dt.timedelta = dt.timedelta(0),
) -> bool:
    """Whether the wall-clock budget has run out, or would before the next attempt could run.

    Two conditions, and the second was missing. Checking only the *elapsed* interval left a schedule
    free to be written past the deadline — a 429 carrying a ``retry_after`` of an hour against a
    ten-minute budget parked the row until an hour had passed, at which point the elapsed check
    finally fired and dead-lettered it. The operator saw a pending row with a future date for the
    whole of that hour, and the bound §15 describes as *"so an entry cannot retry indefinitely"* was
    enforced late rather than at the point of decision.

    So the delay is included: if the next attempt could only run after the budget expired, the
    budget is already spent and the envelope goes to the queue now. Reviewers found this twice, once
    through the provider-controlled ``retry_after`` path where it is worst.
    """
    if first_sent_at is None:
        return False
    return now + delay - first_sent_at >= policy.time_budget


async def _retry_or_dead_letter(
    engine: AsyncEngine,
    *,
    adjustment_id: uuid.UUID,
    operation_id: str,
    adapter_name: str,
    attempt_no: int,
    last_outcome: OutcomeCode,
    delay: dt.timedelta,
    transport: TransportVerdict | None,
    policy: RetryPolicy,
    now: dt.datetime,
) -> RetryReport:
    """The bounded half: schedule if both budgets allow, dead-letter otherwise.

    Both bounds are checked here rather than at selection time, because both are properties of what
    *just happened* — the attempt that failed is the one that consumes the budget.
    """
    async with AsyncSession(engine) as session, session.begin():
        first_sent_at = await _first_sent_at(session, adjustment_id)

        if attempt_no >= policy.max_attempts:
            reason = DeadLetterReason.ATTEMPTS_EXHAUSTED
        elif _budget_exhausted(first_sent_at=first_sent_at, now=now, policy=policy, delay=delay):
            reason = DeadLetterReason.TIME_BUDGET_EXHAUSTED
        else:
            when = now + delay
            await _schedule(session, adjustment_id=adjustment_id, when=when)
            return RetryReport(
                adjustment_id=adjustment_id,
                operation_id=operation_id,
                verdict=RetryVerdict.SCHEDULED,
                attempt_no=attempt_no,
                next_attempt_at=when,
                transport=transport,
            )

        await dead_letter(
            session,
            adjustment_id=adjustment_id,
            reason=reason,
            envelope=_envelope(
                operation_id=operation_id,
                adjustment_id=adjustment_id,
                adapter_name=adapter_name,
                attempt_no=attempt_no,
                last_outcome=last_outcome,
                transport=transport,
                first_sent_at=first_sent_at,
            ),
            attempts=attempt_no,
            mark_state=True,
        )

    return RetryReport(
        adjustment_id=adjustment_id,
        operation_id=operation_id,
        verdict=RetryVerdict.DEAD_LETTERED,
        attempt_no=attempt_no,
        reason=reason,
        transport=transport,
    )


async def _resolve_not_sent(engine: AsyncEngine, *, adjustment_id: uuid.UUID) -> int | None:
    """Close the write-ahead record for a request that provably never left the client.

    ``dispatch_once`` never reaches its third transaction when the adapter raises, so the attempt
    row it committed is still ``in_flight``. For an allowlisted transport failure that state is
    wrong and dangerously so: ``in_flight`` is what a crash *mid-send* leaves, and the ambiguity
    gate refuses to retry it. Recording ``not_sent`` is what distinguishes "nothing was written"
    from "we do not know", which is the entire purpose of the enumerated classifier.

    **The row is found by being in flight, not by an attempt number the caller worked out.** The
    first version read ``max(attempt_no) + 1`` before the send and resolved that row afterwards,
    while ``dispatch_once`` computed its own number inside its own transaction. Nothing holds a lock
    between the two — the claim is released before the send, by design — so a second runner landing
    an attempt in the gap made the numbers disagree, and the failing send then rewrote *another
    attempt's* record, including one that had recorded ``unknown``. Four reviewers found it.

    Returns the attempt number it resolved, or ``None`` when it declines to resolve anything.

    **It declines when more than one attempt is in flight**, and that is the honest answer rather
    than a limitation: with two unresolved sends outstanding, this process cannot tell which is its
    own, and picking either would be guessing about an irreversible write. Both rows stay in flight,
    which makes the operation invisible to the retry path — exactly the state §12.1.1 calls
    ``UNKNOWN``, and exactly what 4.4's recovery is specified to resolve. Safe, visible, and stuck
    rather than wrong.

    Only ever called for a :attr:`~...transport.TransportClass.NOT_SENT` verdict. An ``UNKNOWN``
    leaves the row exactly as it is — resolving that one is 4.4's, and guessing at it here would be
    the double-post this project exists to prevent.
    """
    async with AsyncSession(engine) as session, session.begin():
        in_flight = list(
            (
                await session.execute(
                    select(PostingAttempt)
                    .where(
                        PostingAttempt.adjustment_id == adjustment_id,
                        PostingAttempt.state == AttemptState.IN_FLIGHT,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if len(in_flight) != 1:
            return None

        attempt = in_flight[0]
        attempt.state = AttemptState.RESOLVED
        attempt.outcome = OutcomeCode.NOT_SENT
        attempt.resolved_at = attempt.sent_at

        intent = (
            await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
        ).scalar_one()
        intent.last_outcome = OutcomeCode.NOT_SENT
        intent.attempt_count = max(intent.attempt_count, attempt.attempt_no)

        # The dispatcher appended an event when this attempt was recorded and never reached its
        # third transaction, so without this the trail would show a send with no ending. 4.4
        # requires an event for *every* attempt, and an attempt that failed in transport is one.
        await emit(
            session,
            tool=AuditTool.POST,
            outcome=posting_audit_outcome(OutcomeCode.NOT_SENT),
            correlation_id=await correlation_for_adjustment(session, adjustment_id),
            occurred_at=attempt.sent_at,
            scope_granted=POST_SCOPE,
        )
        return int(attempt.attempt_no)


async def _snapshot(engine: AsyncEngine, adjustment_id: uuid.UUID) -> tuple[str, int, bool]:
    """The persisted identifier, the attempt number this send would take, and whether to send.

    Read together and read *before* the send, because all three are facts about the operation rather
    than about the attempt: the identifier was fixed at 4.1 and this module never derives one, and
    the attempt number has to be known even when the send raises and ``dispatch_once`` returns
    nothing.

    **The dispatch state is checked here because nothing else checks it.** ``due_adjustments``
    filters on ``state = 'pending'``, so the ordinary pass never offers a dead-lettered row — but
    ``attempt_one`` is a public entry point and ``dispatch_once``'s own gates read the recorded
    *outcome*, not the dispatch state. A caller holding a claim taken before the envelope was given
    up on would therefore send it again, and the second dead letter would hit
    ``uq_dlq_outbox_id`` and abort the pass. Two reviewers found the hole; an assertion added while
    fixing something else found it a third time.
    """
    async with AsyncSession(engine) as session, session.begin():
        adjustment = (
            await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
        ).scalar_one()
        intent = (
            await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
        ).scalar_one()
        highest = (
            await session.execute(
                select(func.coalesce(func.max(PostingAttempt.attempt_no), 0)).where(
                    PostingAttempt.adjustment_id == adjustment_id
                )
            )
        ).scalar_one()
        dispatchable = DispatchState(intent.state) is DispatchState.PENDING
        return adjustment.operation_id, int(highest) + 1, dispatchable


def _throttled_delay(
    outcome: PostingOutcome, policy: RetryPolicy, attempt_no: int, rng: random.Random
) -> dt.timedelta:
    """The provider's own schedule, honoured in full rather than clamped.

    §15 calls a 429 *"a scheduling signal, not a declination"*. A signal then overruled by a shorter
    computed delay is not a signal, so ``retry_after`` is a **floor** and is not capped: the cap
    exists to stop *our* exponential curve running away, not to overrule a counterparty saying when
    it will be ready.

    The jittered backoff is still computed and the larger wins, so a provider sending a token
    ``retry_after`` of zero still gets exponential spacing rather than an immediate re-send.
    """
    jittered = backoff_delay(policy, attempt_no, rng)
    if isinstance(outcome, Throttled):
        return max(outcome.retry_after, jittered)
    return jittered


async def attempt_one(
    engine: AsyncEngine,
    *,
    adjustment_id: uuid.UUID,
    adapter: LedgerAdapter,
    policy: RetryPolicy,
    now: dt.datetime,
    rng: random.Random,
) -> RetryReport:
    """Send one operation and decide what happens next. The whole retry decision, in one place.

    Six paths, and each is the specification's rather than this module's:

    - **Refused by a dispatcher gate** — already terminal, or ambiguous and not permitted. Held.
      4.2's gates are load-bearing here and are not second-guessed.
    - **Raised, classified NOT_SENT** — the allowlist. Resolve the attempt, then bounded retry.
    - **Raised, classified UNKNOWN** — everything else, by default. Held, untouched, evidence
      intact. §13.5's capability branch and the recovery queue are 4.4's.
    - **Confirmed** — the ledger applied it; ``dispatch_once`` settled the row.
    - **Rejected** — the ledger declined; the row is settled and the envelope goes *straight* to the
      dead-letter queue, with no attempt wasted first.
    - **Throttled** — its own path, on the provider's schedule.
    - **Unknown or PartiallyApplied returned** — held, for the same reason as a raised UNKNOWN.
    """
    operation_id, attempt_no, dispatchable = await _snapshot(engine, adjustment_id)

    if not dispatchable:
        # Settled or dead-lettered. Either way this operation's dispatch is over, and the only way
        # back is a deliberate replay — which is the point of the dead-letter queue.
        return RetryReport(
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            verdict=RetryVerdict.HELD,
            attempt_no=attempt_no,
        )

    try:
        result = await dispatch_once(
            engine,
            adjustment_id=adjustment_id,
            adapter=AttributedAdapter(adapter),
            sent_at=now,
        )
    except DispatchRefusedError:
        return RetryReport(
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            verdict=RetryVerdict.HELD,
            attempt_no=attempt_no,
        )
    except AdapterCallError as raised:
        # **Only what the adapter raised reaches the classifier.** Anything else — a database
        # failure, a bug in our own code — is not evidence about the ledger and is re-raised
        # untouched, leaving the write-ahead record in flight, which is precisely what §12.1.1 says
        # that state means. See :class:`AdapterCallError`.
        verdict = classify_transport_failure(raised.error)
        if not verdict.retryable:
            # UNKNOWN. The attempt row stays in_flight, which is what §12.1.1 calls this state and
            # what the ambiguity gate reads. Nothing is written and nothing is scheduled: 4.4 owns
            # every transition out of here, and inventing one would be the double-post.
            return RetryReport(
                adjustment_id=adjustment_id,
                operation_id=operation_id,
                verdict=RetryVerdict.HELD,
                attempt_no=attempt_no,
                transport=verdict,
            )

        resolved = await _resolve_not_sent(engine, adjustment_id=adjustment_id)
        if resolved is None:
            # Two sends outstanding and no way to tell which was ours. Nothing is written.
            return RetryReport(
                adjustment_id=adjustment_id,
                operation_id=operation_id,
                verdict=RetryVerdict.HELD,
                attempt_no=attempt_no,
                transport=verdict,
            )

        return await _retry_or_dead_letter(
            engine,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            adapter_name=adapter.name,
            attempt_no=resolved,
            last_outcome=OutcomeCode.NOT_SENT,
            delay=backoff_delay(policy, resolved, rng),
            transport=verdict,
            policy=policy,
            now=now,
        )

    code = outcome_code(result.outcome)

    if code is OutcomeCode.CONFIRMED:
        return RetryReport(
            adjustment_id=adjustment_id,
            operation_id=result.operation_id,
            verdict=RetryVerdict.SETTLED,
            attempt_no=result.attempt_no,
        )

    if code is OutcomeCode.REJECTED:
        async with AsyncSession(engine) as session, session.begin():
            await dead_letter(
                session,
                adjustment_id=adjustment_id,
                reason=DeadLetterReason.TERMINAL_REJECTION,
                envelope=_envelope(
                    operation_id=result.operation_id,
                    adjustment_id=adjustment_id,
                    adapter_name=adapter.name,
                    attempt_no=result.attempt_no,
                    last_outcome=code,
                    transport=None,
                    first_sent_at=await _first_sent_at(session, adjustment_id),
                ),
                attempts=result.attempt_no,
                mark_state=False,
            )
        return RetryReport(
            adjustment_id=adjustment_id,
            operation_id=result.operation_id,
            verdict=RetryVerdict.DEAD_LETTERED,
            attempt_no=result.attempt_no,
            reason=DeadLetterReason.TERMINAL_REJECTION,
        )

    if code is OutcomeCode.THROTTLED:
        return await _retry_or_dead_letter(
            engine,
            adjustment_id=adjustment_id,
            operation_id=result.operation_id,
            adapter_name=adapter.name,
            attempt_no=result.attempt_no,
            last_outcome=code,
            delay=_throttled_delay(result.outcome, policy, result.attempt_no, rng),
            transport=None,
            policy=policy,
            now=now,
        )

    return RetryReport(
        adjustment_id=adjustment_id,
        operation_id=result.operation_id,
        verdict=RetryVerdict.HELD,
        attempt_no=result.attempt_no,
    )


async def run_due_once(
    engine: AsyncEngine,
    *,
    adapter: LedgerAdapter,
    policy: RetryPolicy,
    now: dt.datetime,
    rng: random.Random,
    limit: int = 32,
) -> list[RetryReport]:
    """One bounded pass over the due queue. **Not a daemon, and not a loop that sleeps.**

    The plan gives 4.3 backoff, bounds, a classifier, a dead-letter queue and a replay CLI. It does
    not give it a background process, so there is none: this function does a finite amount of work
    and returns. What drives it — a test, the CLI, a scheduler somebody deploys — is a decision this
    increment does not need to take, and taking it would be inventing an operational model nobody
    specified.

    The claim is taken and released before any send. Holding it across ``dispatch_once`` would
    deadlock against that function's own ``FOR UPDATE`` on the same row, and holding it across a
    *network* call would be the long-running claim transaction the 4.1 record warned about. The
    consequence is that two runners can select the same row between the release and the send, and
    the interlock for that is the one 4.2 established: the loser of the write-ahead insert on
    ``(adjustment_id, attempt_no)`` is refused, never promoted to the next attempt number.
    """
    async with AsyncSession(engine) as session, session.begin():
        due = await due_adjustments(session, now=now, limit=limit)

    return [
        await attempt_one(
            engine,
            adjustment_id=adjustment_id,
            adapter=adapter,
            policy=policy,
            now=now,
            rng=rng,
        )
        for adjustment_id in due
    ]


# ======================================================================================
# The way back: replay
#
# FR-11: "A CLI replays DLQ entries. Replay must not create a second ledger effect for an
# adjustment already `CONFIRMED`." Acceptance criterion 8 is the measured form of the same
# sentence, and it is measured **at the ledger**: "produces exactly one applied posting for the
# operation — verified by the simulated ledger's applied-count, not by our own records".
# ======================================================================================


class ReplayOutcome(enum.StrEnum):
    """What a replay actually did. Six values, because "it worked" hides four different things."""

    #: The ledger applied it and the operation is now confirmed.
    APPLIED = "applied"

    #: The adjustment already carried a posting reference. **Nothing was sent.** This is acceptance
    #: criterion 8's second half, and it is checked before any dispatch rather than relying on the
    #: dispatcher's refusal, so the CLI can say plainly that it made no call at all.
    ALREADY_CONFIRMED = "already_confirmed"

    #: The ledger declined — now, or on the attempt that produced the dead letter. Terminal.
    #:
    #: A rejection cannot be fixed by sending it again, and §13.3 refuses a second send for an
    #: operation in a known terminal state, so the entry is finished rather than pending. The first
    #: version reported this case as ``REFUSED`` and left the row pending forever, which starved
    #: ``replay --all`` the moment one rejection existed; four reviewers found it.
    REJECTED = "rejected"

    #: A dispatcher gate refused the send — the operation is in a known terminal state, or it is
    #: ambiguous and capability does not permit a re-send. Nothing was sent and nothing changed.
    REFUSED = "refused"

    #: An allowlisted transport failure again. Nothing reached the ledger.
    NOT_SENT = "not_sent"

    #: Ambiguous. The attempt record stays in flight and 4.4 owns every transition out of it.
    HELD = "held"


#: Replay outcomes after which the dead letter is finished.
#:
#: Deliberately excludes ``REFUSED``, ``NOT_SENT`` and ``HELD``: each of those leaves the operation
#: exactly where it was, and marking the entry replayed would tell an operator the queue had been
#: worked when it had not. A dead letter that stays pending is a dead letter somebody still has to
#: look at, which is the honest reading of all three.
_RESOLVES_THE_ENTRY: frozenset[ReplayOutcome] = frozenset(
    {ReplayOutcome.APPLIED, ReplayOutcome.ALREADY_CONFIRMED, ReplayOutcome.REJECTED}
)


@dataclasses.dataclass(frozen=True, slots=True)
class ReplayReport:
    """What one replay did, returned so the CLI reports rather than infers."""

    dlq_id: uuid.UUID
    adjustment_id: uuid.UUID
    operation_id: str
    outcome: ReplayOutcome
    #: The reference the ledger gave, whether now or on the attempt that was interrupted.
    posting_ref: str | None = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.outcome in _RESOLVES_THE_ENTRY


async def pending_dead_letters(session: AsyncSession, *, limit: int = 100) -> Sequence[uuid.UUID]:
    """The operator's queue: dead letters nobody has replayed, oldest first.

    Matches ``ix_dlq_pending_created_at``, the partial index the schema has carried since M1.2.
    """
    rows = await session.execute(
        select(DeadLetter.id)
        .where(DeadLetter.replay_state == ReplayState.PENDING)
        .order_by(DeadLetter.created_at)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def replay_dead_letter(
    engine: AsyncEngine,
    *,
    dlq_id: uuid.UUID,
    adapter: LedgerAdapter,
    now: dt.datetime,
) -> ReplayReport:
    """Replay one dead letter. **The identifier is read, never re-derived.**

    Every safety property here is inherited rather than re-implemented, and that is the design:

    - The **operation identifier** is the one persisted at 4.1. This function never calls the
      derivation, and a guard test asserts the module cannot: replaying under a fresh identifier
      would present the ledger with a second, unrelated operation carrying the same money.
    - The **financial instruction** is rebuilt by ``dispatch_once`` from the persisted
      ``adjustment`` row. Nothing is copied out of the envelope — which deliberately carries no
      amount — so a replay cannot post a different figure from the one that was approved.
    - **No new adjustment** is created. This module writes none, and the creation fence names the
      one module that may.
    - The **terminal and ambiguity gates** are the dispatcher's. A replay is an ordinary send with a
      human deciding when, not a privileged one, and it gets exactly the same refusals.

    The already-confirmed check is made here as well, before any dispatch, because acceptance
    criterion 8 asks for a demonstration that replay *applies nothing further* — and a refusal
    raised from inside the dispatcher, while equally safe, is a weaker demonstration than never
    making the call.
    """
    async with AsyncSession(engine) as session, session.begin():
        entry = (
            await session.execute(select(DeadLetter).where(DeadLetter.id == dlq_id))
        ).scalar_one_or_none()
        if entry is None:
            raise LookupError(f"no dead letter {dlq_id}")
        # Coerced, not compared. `replay_state` is a `String(16)` column with an enum annotation,
        # so SQLAlchemy hands back a plain `str` — and `"pending" is not ReplayState.PENDING` is
        # True, because identity is not equality. The first version of this check therefore refused
        # every entry with the message "dead letter … is pending; only a pending entry may be
        # replayed", which is the failure telling you the answer if you read it. The same trap has
        # bitten this repository before; the dispatcher coerces for the same reason.
        if ReplayState(entry.replay_state) is not ReplayState.PENDING:
            raise ValueError(
                f"dead letter {dlq_id} is {entry.replay_state}; "
                "only a pending entry may be replayed"
            )

        intent = (
            await session.execute(select(Outbox).where(Outbox.id == entry.outbox_id))
        ).scalar_one()
        adjustment = (
            await session.execute(select(Adjustment).where(Adjustment.id == intent.adjustment_id))
        ).scalar_one()
        adjustment_id = adjustment.id
        operation_id = adjustment.operation_id
        already = adjustment.posting_ref
        settled_outcome = (
            OutcomeCode(intent.last_outcome) if intent.last_outcome is not None else None
        )

    if already is not None:
        report = ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=ReplayOutcome.ALREADY_CONFIRMED,
            posting_ref=already,
            detail="the adjustment already carries a posting reference; nothing was sent",
        )
        await _close_entry(engine, dlq_id=dlq_id, now=now)
        return report

    if settled_outcome is OutcomeCode.REJECTED:
        # Checked here rather than left to the dispatcher's refusal, for the same reason the
        # already-confirmed case is: a rejection is *finished*, not *blocked*, and reporting it as a
        # refusal left the entry pending with no route out of the queue. Nothing is sent.
        report = ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=ReplayOutcome.REJECTED,
            detail="the ledger declined this operation; a re-send cannot change that",
        )
        await _close_entry(engine, dlq_id=dlq_id, now=now)
        return report

    try:
        result = await dispatch_once(
            engine,
            adjustment_id=adjustment_id,
            adapter=AttributedAdapter(adapter),
            sent_at=now,
        )
    except DispatchRefusedError as refusal:
        return ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=ReplayOutcome.REFUSED,
            detail=str(refusal),
        )
    except AdapterCallError as raised:
        # As in `attempt_one`: only the adapter's own failure is classified, and anything else is
        # re-raised rather than being turned into a claim about the ledger.
        verdict = classify_transport_failure(raised.error)
        if verdict.retryable:
            await _resolve_not_sent(engine, adjustment_id=adjustment_id)
            outcome = ReplayOutcome.NOT_SENT
        else:
            outcome = ReplayOutcome.HELD
        return ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=outcome,
            detail=verdict.detail,
        )

    code = outcome_code(result.outcome)
    if code is OutcomeCode.CONFIRMED:
        report = ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=ReplayOutcome.APPLIED,
            posting_ref=getattr(result.outcome, "posting_ref", None),
        )
    elif code is OutcomeCode.REJECTED:
        report = ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=ReplayOutcome.REJECTED,
            detail=getattr(result.outcome, "reason", ""),
        )
    else:
        report = ReplayReport(
            dlq_id=dlq_id,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            outcome=ReplayOutcome.HELD,
            detail=code.value,
        )

    if report.resolved:
        await _close_entry(engine, dlq_id=dlq_id, now=now)
    return report


async def _close_entry(engine: AsyncEngine, *, dlq_id: uuid.UUID, now: dt.datetime) -> None:
    """Mark a dead letter replayed.

    ``replayed_at`` is set in the same statement because ``replayed_at_iff_replayed`` is an
    equality: the database refuses a replayed entry with no timestamp, which is the constraint
    doing exactly what it was written for.

    Nothing here ever sets ``abandoned``. That state belongs to an operator deciding an entry will
    never be replayed, and 4.3 ships no verb for that decision — inventing one would be inventing
    an operator workflow the plan does not describe.
    """
    async with AsyncSession(engine) as session, session.begin():
        entry = (
            await session.execute(select(DeadLetter).where(DeadLetter.id == dlq_id))
        ).scalar_one()
        entry.replay_state = ReplayState.REPLAYED
        entry.replayed_at = now
