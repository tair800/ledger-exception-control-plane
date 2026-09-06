"""Resolving an ambiguous outcome — §13.5's capability branch, bounded (increment 4.4).

4.2 built the **refusal** half of this branch: where capability could not suppress or detect a
duplicate, a further send after an ambiguous outcome was refused and the routes were named. 4.3 kept
`UNKNOWN` out of the retry path entirely. This is the half that acts, and the plan's goal for it is
exact:

    Make the conditional nature of the side-effect guarantee real in code, not just in prose.

**Query before re-send, always.** An adapter can be both `ENFORCES_KEY` and `BY_OPERATION_ID`, and
when it is, this module asks rather than sends. §13.5 clause 4 says *"reconcile against the
downstream system where possible"*, and the ordering is not a preference: a query is a read and a
re-send is an irreversible financial write, so the branch that can be wrong for free goes first.

**The bounds are the point.** §13.5 clause 3 does not say "re-send under `ENFORCES_KEY`". It says a
re-send *"is permitted only while ``now - first_send < idempotency_window`` **and** the target
endpoint matches the original ``idempotency_scope``"*, because:

    Real providers retain keys for a limited period (commonly hours to days) and often scope them
    per-endpoint or per-account; a re-send outside either bound is an ordinary duplicate write
    wearing an idempotency header.

A re-send carrying an idempotency header the provider has already forgotten is not idempotent — it
is a second posting with extra ceremony, and nothing downstream would tell us it had happened.

**`NotFound` is not "no".** §13.5's table is explicit that a negative answer becomes trustworthy
only after both declared windows have elapsed, across N consecutive queries:

    A `NotFound` means "not visible to this query yet", which is not the same as "will never be
    applied": an in-flight request the ledger has received but not yet committed, or a read that is
    not linearizable with respect to the posting write, both produce it. Acting on it is a
    double-post.

So `Found` resolves immediately — *"a positive hit is trustworthy"* — `NotFound` accumulates, and
`Indeterminate` never counts toward the total.

**The count is derived, never stored.** Every query is appended to ``reconciliation_query``, which a
trigger makes append-only, and "N consecutive" is read back off those rows. A column holding the
running total would be a number somebody can set, and that number is the entire safety argument for
declaring an ambiguous financial write un-applied.

**Monotonic, and what that means against this schema.** §13.5 clause 6: *"`UNKNOWN` is never
overwritten in place; resolution is an appended transition."* Three rules, each enforced by a
trigger rather than intended:

- the ``posting_attempt`` row that saw the ambiguity **keeps** its outcome forever, so the evidence
  of what that send observed is immutable — this module never writes to that table;
- ``outbox.last_outcome`` is the current-state pointer and may not move off a terminal value;
- the transition itself is *appended* — a query row and an audit event carrying the windows used.

**On exhaustion, manual recovery — never a guess.** *"Reconciliation is not total, and the design
says so rather than leaving the exhausted case to fall off the end."*
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import uuid
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.audit import correlation_for_adjustment, emit
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.control import (
    Adjustment,
    AttemptState,
    AuditOutcome,
    AuditTool,
    DispatchState,
    Outbox,
    PostingAttempt,
    QueryAnswer,
    ReconciliationQuery,
    RecoveryItem,
    RecoveryState,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
from ledger_exception_control_plane.ledger.conformance import capabilities_for
from ledger_exception_control_plane.ledger.port import (
    Eventual,
    Found,
    Indeterminate,
    LedgerAdapter,
    LedgerAdapterCapabilities,
    Linearizable,
    NotFound,
    QueryableLedgerAdapter,
    declared_endpoint,
)
from ledger_exception_control_plane.operations.dispatcher import (
    ResendBound,
    dispatch_once,
    resend_is_within_bounds,
)
from ledger_exception_control_plane.operations.recovery import RecoveryReason, open_item

#: ``ResendBound`` and ``resend_is_within_bounds`` appear here and are defined in
#: :mod:`~.dispatcher`, deliberately: §13.5's two bounds guard an irreversible write, so they are
#: evaluated in the gate in front of the socket rather than in whichever module decided a re-send
#: was worth attempting. They are re-exported because this is the module a reader looks in for the
#: capability branch, and a unit test pins both the placement and the re-export.
__all__ = [
    "AMBIGUOUS",
    "RECONCILE_SCOPE",
    "ReconciliationPolicy",
    "ReconciliationReport",
    "ResendBound",
    "Resolution",
    "reconcile_once",
    "resend_is_within_bounds",
    "visibility_bound_of",
]

#: §11's *"authorisation under which the action ran"* for a reconciliation query.
RECONCILE_SCOPE: Final = "ledger:reconcile"

#: Outcomes that leave the ledger's state undetermined and therefore need this module.
#:
#: The third declaration of this set in the codebase, and deliberately not shared: the dispatcher's
#: gates a second send inside one dispatch, the retry module's gates selection for retry, and this
#: one gates *resolution*. A test asserts all three agree, so the duplication cannot drift while
#: each stays free to change for its own reason.
AMBIGUOUS: Final[frozenset[OutcomeCode]] = frozenset(
    {OutcomeCode.UNKNOWN, OutcomeCode.PARTIALLY_APPLIED}
)


class Resolution(enum.StrEnum):
    """What one reconciliation pass concluded. Reported, never inferred by the caller."""

    #: The ledger confirmed the posting. §13.5: *"A positive hit is trustworthy."*
    CONFIRMED = "confirmed"

    #: Both windows elapsed and N consecutive queries said the posting is not there.
    REJECTED = "rejected"

    #: A re-send was permitted and made, under a verified ``ENFORCES_KEY`` inside its bounds.
    RESENT = "resent"

    #: Still ambiguous. More queries, or more time, are needed before anything may be concluded.
    UNRESOLVED = "unresolved"

    #: The bounds are spent, or capability offers no route. An operator decides from here.
    ROUTED_TO_RECOVERY = "routed_to_recovery"

    #: Already in the queue. A second item would split one operator's evidence across two entries.
    ALREADY_IN_RECOVERY = "already_in_recovery"

    #: Nothing to do: this operation is not ambiguous.
    NOT_AMBIGUOUS = "not_ambiguous"


@dataclasses.dataclass(frozen=True, slots=True)
class ReconciliationPolicy:
    """The two bounds that are ours. Everything else comes from the adapter's declarations.

    The visibility bound, the in-flight window, the idempotency window and the scope are all read
    from the *effective* capability record, because §13.5 makes those the provider's declarations
    rather than our policy. What is left for us is how much corroboration a negative answer needs
    and how long the automatic path may keep asking.

    Both defaults are **declared project decisions** in the sense ADR-042 and ADR-055 established:
    §13.5 says *"N consecutive queries"* and names no number, and requires reconciliation to be
    bounded without saying by how much. Recorded in ADR-057.
    """

    #: N. Three: one answer can be a stale read, two can be a replica that has not caught up, and
    #: three spread across the declared visibility window is the point at which "not visible yet"
    #: stops being a plausible description of a live write.
    consecutive_not_found: int = 3

    #: How many passes an operation gets before it routes to an operator. Bounded because §13.5
    #: requires reconciliation to be *"bounded and scheduled, never an unbounded retry loop"*, and
    #: an unbounded query loop against a ledger is a retry loop that has stopped calling itself one.
    max_queries: int = 10

    #: How long an operator has before a recovery item becomes an alertable condition (§13.5).
    sla: dt.timedelta = dt.timedelta(hours=24)

    def __post_init__(self) -> None:
        if self.consecutive_not_found < 1:
            raise ValueError("N must be at least 1; zero would resolve on no evidence at all")
        if self.max_queries < self.consecutive_not_found:
            raise ValueError(
                "max_queries must allow at least N queries, or the bound expires before the "
                "evidence it is waiting for could ever be gathered"
            )
        if self.sla <= dt.timedelta(0):
            raise ValueError("an SLA that has already elapsed makes every item stale at once")

    @classmethod
    def from_settings(cls, settings: Settings) -> ReconciliationPolicy:
        return cls(
            consecutive_not_found=settings.reconcile_consecutive_not_found,
            max_queries=settings.reconcile_max_queries,
            sla=dt.timedelta(hours=settings.recovery_sla_hours),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """What one pass did, and the evidence it did it on."""

    adjustment_id: uuid.UUID
    operation_id: str
    resolution: Resolution

    #: The answer this pass got, where it asked one.
    answer: QueryAnswer | None = None

    #: The re-send bound evaluated on this pass, where the branch reached it.
    resend_bound: ResendBound | None = None

    #: How many consecutive ``NotFound`` answers now stand on record, read back from the appended
    #: rows rather than counted in memory.
    consecutive_not_found: int = 0

    #: How many queries this operation has had, against the policy's ceiling.
    queries_made: int = 0

    #: Why it went to an operator, where it did.
    recovery_reason: RecoveryReason | None = None

    detail: str = ""


def visibility_bound_of(capabilities: LedgerAdapterCapabilities) -> dt.timedelta:
    """How long a read may lag a committed write, as a duration.

    ``LINEARIZABLE`` is zero — §13.5 permits resolving *"immediately, if
    ``query_consistency == LINEARIZABLE``"* — and ``EVENTUAL`` carries its own bound, which is why
    4.2 put the payload on the variant rather than leaving the mode a bare flag.
    """
    consistency = capabilities.query_consistency
    if isinstance(consistency, Linearizable):
        return dt.timedelta(0)
    if isinstance(consistency, Eventual):
        return consistency.visibility_bound
    raise TypeError(  # pragma: no cover - the union is closed and mypy proves it
        f"{consistency!r} is not a QueryConsistency"
    )


def negative_answer_is_trustworthy(
    *,
    capabilities: LedgerAdapterCapabilities,
    last_sent_at: dt.datetime,
    now: dt.datetime,
) -> bool:
    """Whether enough time has passed for a ``NotFound`` to mean anything.

    §13.5: resolve to ``REJECTED`` *"only after **both** the declared ``visibility_bound`` (or
    immediately, if ``query_consistency == LINEARIZABLE``) **and** ``max_inflight_window`` have
    elapsed since the last send"*.

    Both, not either. The visibility bound covers a read that has not caught up with the write; the
    in-flight window covers a request the ledger has received and not yet committed. They are
    different failure modes, and a system that waited for only one would still act on the other.
    """
    elapsed = now - last_sent_at
    return elapsed >= visibility_bound_of(capabilities) and elapsed >= (
        capabilities.max_inflight_window
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _State:
    """Everything one pass needs, read in a single transaction before any I/O."""

    operation_id: str
    correlation_id: str
    #: Deliberately **not** named ``last_outcome``. The column is text and this is the coerced
    #: enum, so identity comparison against it is correct here and would be a bug against the
    #: raw column — a distinction the stored-enum guard cannot see, and one a shared name would
    #: have hidden from every later reader as well.
    recorded_outcome: OutcomeCode | None
    unresolved_attempts: int
    first_sent_at: dt.datetime | None
    last_sent_at: dt.datetime | None
    original_endpoint: str | None
    queries_made: int
    open_recovery: bool

    @property
    def is_ambiguous(self) -> bool:
        """Ambiguous either because an outcome says so, or because a send never answered.

        The second half is the one the dispatcher's first version missed: ``last_outcome`` is
        written when a dispatch *completes*, so a crash mid-send leaves the column NULL and the
        operation ambiguous with nothing recorded to say so. The in-flight attempt row is the
        evidence, which is exactly why §12.1.1 requires it to be committed before the socket write.
        """
        return self.unresolved_attempts > 0 or (
            self.recorded_outcome is not None and self.recorded_outcome in AMBIGUOUS
        )

    @property
    def is_partially_applied(self) -> bool:
        """Whether the adapter reported that some legs committed and some did not.

        A property rather than a comparison at the call site, and the reason is the stored-enum
        guard rather than taste: ``state.recorded_outcome is OutcomeCode.X`` is the exact shape of
        the defect that guard exists to catch — an identity comparison against a value that came
        out of a text column and is therefore a plain ``str``. Here the value was coerced when the
        row was read, so the comparison is safe; written as ``self.``, it is safe *and* recognisably
        so, which is what keeps the guard worth having.
        """
        return self.recorded_outcome is OutcomeCode.PARTIALLY_APPLIED


async def _read_state(session: AsyncSession, adjustment_id: uuid.UUID) -> tuple[_State, str]:
    outbox, adjustment = (
        await session.execute(
            select(Outbox, Adjustment)
            .join(Adjustment, Outbox.adjustment_id == Adjustment.id)
            .where(Outbox.adjustment_id == adjustment_id)
        )
    ).one()

    unresolved, first_sent_at, last_sent_at = (
        await session.execute(
            select(
                func.count().filter(PostingAttempt.state == AttemptState.IN_FLIGHT),
                func.min(PostingAttempt.sent_at),
                func.max(PostingAttempt.sent_at),
            ).where(PostingAttempt.adjustment_id == adjustment_id)
        )
    ).one()

    # The endpoint of the *first* send, because that is the one the provider's key is held against.
    original_endpoint = (
        await session.execute(
            select(PostingAttempt.endpoint)
            .where(PostingAttempt.adjustment_id == adjustment_id)
            .order_by(PostingAttempt.attempt_no)
            .limit(1)
        )
    ).scalar_one_or_none()

    queries_made = int(
        (
            await session.execute(
                select(func.coalesce(func.max(ReconciliationQuery.query_no), 0)).where(
                    ReconciliationQuery.adjustment_id == adjustment_id
                )
            )
        ).scalar_one()
    )

    open_recovery = (
        await session.execute(
            select(func.count())
            .select_from(RecoveryItem)
            .where(
                RecoveryItem.adjustment_id == adjustment_id,
                RecoveryItem.state == RecoveryState.OPEN,
            )
        )
    ).scalar_one() > 0

    state = _State(
        operation_id=adjustment.operation_id,
        correlation_id=await correlation_for_adjustment(session, adjustment_id),
        recorded_outcome=OutcomeCode(outbox.last_outcome) if outbox.last_outcome else None,
        unresolved_attempts=int(unresolved),
        first_sent_at=first_sent_at,
        last_sent_at=last_sent_at,
        original_endpoint=original_endpoint,
        queries_made=queries_made,
        open_recovery=open_recovery,
    )
    return state, adjustment.operation_id


async def _consecutive_not_found(session: AsyncSession, adjustment_id: uuid.UUID) -> int:
    """How many ``NotFound`` answers stand at the end of the record, read back off the rows.

    Derived rather than counted in memory, and derived rather than stored: the number that decides
    whether an ambiguous financial write may be declared un-applied has to be reconstructible from
    append-only evidence, or it is just a number.

    Expressed as *"the highest query number, minus the highest query number that was not a
    ``NotFound``"* — which is the length of the trailing run, needs no window function, and returns
    zero when the most recent answer was something else.
    """
    highest = int(
        (
            await session.execute(
                select(func.coalesce(func.max(ReconciliationQuery.query_no), 0)).where(
                    ReconciliationQuery.adjustment_id == adjustment_id
                )
            )
        ).scalar_one()
    )
    last_other = int(
        (
            await session.execute(
                select(func.coalesce(func.max(ReconciliationQuery.query_no), 0)).where(
                    ReconciliationQuery.adjustment_id == adjustment_id,
                    ReconciliationQuery.answer != QueryAnswer.NOT_FOUND,
                )
            )
        ).scalar_one()
    )
    return highest - last_other


_ANSWER_AUDIT_OUTCOME: Final[dict[QueryAnswer, AuditOutcome]] = {
    QueryAnswer.FOUND: AuditOutcome.SUCCESS,
    # Not a failure: the question was answered, and the answer was "not visible to this query".
    # Filing it as a failure would put the coercion §13.5 forbids into the audit trail.
    QueryAnswer.NOT_FOUND: AuditOutcome.QUARANTINED,
    QueryAnswer.INDETERMINATE: AuditOutcome.QUARANTINED,
}


async def _route_to_recovery(
    engine: AsyncEngine,
    *,
    adjustment_id: uuid.UUID,
    operation_id: str,
    reason: RecoveryReason,
    now: dt.datetime,
    policy: ReconciliationPolicy,
    report: ReconciliationReport,
) -> ReconciliationReport:
    async with AsyncSession(engine) as session, session.begin():
        opened = await open_item(
            session,
            adjustment_id=adjustment_id,
            reason=reason,
            opened_at=now,
            sla=policy.sla,
        )
    resolution = (
        Resolution.ROUTED_TO_RECOVERY if opened is not None else Resolution.ALREADY_IN_RECOVERY
    )
    return dataclasses.replace(report, resolution=resolution, recovery_reason=reason)


async def reconcile_once(
    engine: AsyncEngine,
    *,
    adjustment_id: uuid.UUID,
    adapter: LedgerAdapter,
    now: dt.datetime,
    policy: ReconciliationPolicy | None = None,
    target_endpoint: str | None = None,
) -> ReconciliationReport:
    """One bounded pass over one ambiguous operation.

    Takes an **engine** rather than a session for the reason the dispatcher does: the adapter call
    sits between two transactions, and holding one open across network I/O would keep a lock on a
    financial row for as long as a provider takes to answer.

    ``now`` is a parameter rather than a clock reading, because both §13.5 windows are measured
    against it and a value invented inside this function would make the safety argument depend on
    when the code happened to run.

    ``target_endpoint`` is where a re-send *would* go. It defaults to the adapter's declaration; a
    caller passes it explicitly to reconcile against a deployment whose endpoint has moved, which
    is precisely the case ``PER_ENDPOINT`` retention makes unsafe.
    """
    policy = policy or ReconciliationPolicy()
    capabilities = capabilities_for(adapter)
    target = target_endpoint if target_endpoint is not None else declared_endpoint(adapter)

    async with AsyncSession(engine) as session:
        state, operation_id = await _read_state(session, adjustment_id)

    base = ReconciliationReport(
        adjustment_id=adjustment_id,
        operation_id=operation_id,
        resolution=Resolution.UNRESOLVED,
        queries_made=state.queries_made,
    )

    if not state.is_ambiguous:
        return dataclasses.replace(
            base,
            resolution=Resolution.NOT_AMBIGUOUS,
            detail="nothing to resolve: no unresolved attempt and no ambiguous recorded outcome",
        )

    if state.open_recovery:
        return dataclasses.replace(
            base,
            resolution=Resolution.ALREADY_IN_RECOVERY,
            detail="an operator already holds this operation; a second item would split the "
            "evidence across two queue entries",
        )

    # §14 sends a partial application straight to an operator, whatever else capability offers: a
    # query answers "is it there", and the answer for a posting whose legs disagree is "partly",
    # which is not a resolution. Checked before every capability branch so no adapter's strength
    # can route it into an automatic path.
    if state.is_partially_applied:
        return await _route_to_recovery(
            engine,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            reason=RecoveryReason.PARTIALLY_APPLIED,
            now=now,
            policy=policy,
            report=dataclasses.replace(
                base, detail="legs disagree; §14 routes this to an operator and never retries it"
            ),
        )

    if capabilities.queryable_by_operation_id and isinstance(adapter, QueryableLedgerAdapter):
        return await _reconcile_by_query(
            engine,
            adjustment_id=adjustment_id,
            adapter=adapter,
            capabilities=capabilities,
            state=state,
            now=now,
            policy=policy,
            report=base,
        )

    if capabilities.suppresses_duplicates:
        bound = resend_is_within_bounds(
            capabilities=capabilities,
            first_sent_at=state.first_sent_at,
            now=now,
            original_endpoint=state.original_endpoint,
            target_endpoint=target,
        )
        report = dataclasses.replace(base, resend_bound=bound)
        if bound is ResendBound.PERMITTED:
            # The dispatcher re-checks both the branch and the bounds — it owns them — so the
            # permission is evaluated twice by two modules rather than trusted across the call.
            #
            # **What bounds the number of re-sends is the window, not a counter**, and that is the
            # specification's answer rather than an omission here: §13.5 names exactly two bounds,
            # and once the window elapses this branch routes to an operator instead. Inside it, a
            # send that comes back ambiguous again can be sent again — which is safe for precisely
            # the reason the branch is permitted at all, since the capability that unlocks it is
            # the provider's contractual suppression of the repeated identifier. Adding an attempt
            # ceiling here would bound something the ledger already bounds, and would do it with a
            # number nobody specified.
            result = await dispatch_once(
                engine, adjustment_id=adjustment_id, adapter=adapter, sent_at=now
            )
            return dataclasses.replace(
                report,
                resolution=Resolution.RESENT,
                detail=f"re-sent inside the declared window and scope; outcome "
                f"{type(result.outcome).__name__}",
            )

        reason = (
            RecoveryReason.RESEND_WINDOW_EXPIRED
            if bound is ResendBound.WINDOW_EXPIRED
            else RecoveryReason.RESEND_SCOPE_UNPROVEN
        )
        return await _route_to_recovery(
            engine,
            adjustment_id=adjustment_id,
            operation_id=operation_id,
            reason=reason,
            now=now,
            policy=policy,
            report=report,
        )

    # Neither branch. §13.5: *"Otherwise route to manual recovery … the automatic path stops."*
    return await _route_to_recovery(
        engine,
        adjustment_id=adjustment_id,
        operation_id=operation_id,
        reason=RecoveryReason.NO_SUPPRESSION_OR_QUERY,
        now=now,
        policy=policy,
        report=dataclasses.replace(base, resend_bound=ResendBound.NOT_ENFORCED),
    )


async def _reconcile_by_query(
    engine: AsyncEngine,
    *,
    adjustment_id: uuid.UUID,
    adapter: QueryableLedgerAdapter,
    capabilities: LedgerAdapterCapabilities,
    state: _State,
    now: dt.datetime,
    policy: ReconciliationPolicy,
    report: ReconciliationReport,
) -> ReconciliationReport:
    """§13.5 clause 4's table, with the bound that stops it becoming a retry loop."""
    if state.queries_made >= policy.max_queries:
        return await _route_to_recovery(
            engine,
            adjustment_id=adjustment_id,
            operation_id=state.operation_id,
            reason=RecoveryReason.RECONCILIATION_EXHAUSTED,
            now=now,
            policy=policy,
            report=dataclasses.replace(
                report,
                detail=f"{state.queries_made} queries made and the answer is still not definite",
            ),
        )

    answer = await adapter.get_by_operation_id(state.operation_id)

    code: QueryAnswer
    posting_ref: str | None
    detail: str | None
    match answer:
        case Found():
            code, posting_ref, detail = QueryAnswer.FOUND, answer.posting_ref, None
        case NotFound():
            code, posting_ref, detail = QueryAnswer.NOT_FOUND, None, None
        case Indeterminate():
            code, posting_ref, detail = QueryAnswer.INDETERMINATE, None, answer.detail
        case _:
            raise TypeError(
                f"the adapter answered with {type(answer).__name__}, which is not a QueryOutcome; "
                "an unrecognised answer is not evidence and must not be recorded as any"
            )

    visibility = visibility_bound_of(capabilities)

    async with AsyncSession(engine) as session, session.begin():
        # Appended first, so the observation is on record before anything is concluded from it.
        session.add(
            ReconciliationQuery(
                adjustment_id=adjustment_id,
                operation_id=state.operation_id,
                query_no=state.queries_made + 1,
                queried_at=now,
                answer=code,
                posting_ref=posting_ref,
                detail=detail,
                visibility_bound=visibility,
                max_inflight_window=capabilities.max_inflight_window,
            )
        )
        await session.flush()

        await emit(
            session,
            tool=AuditTool.RECONCILE,
            outcome=_ANSWER_AUDIT_OUTCOME[code],
            correlation_id=state.correlation_id,
            occurred_at=now,
            scope_granted=RECONCILE_SCOPE,
        )

        consecutive = await _consecutive_not_found(session, adjustment_id)
        outcome = ReconciliationReport(
            adjustment_id=adjustment_id,
            operation_id=state.operation_id,
            resolution=Resolution.UNRESOLVED,
            answer=code,
            consecutive_not_found=consecutive,
            queries_made=state.queries_made + 1,
        )

        if code is QueryAnswer.FOUND:
            intent = (
                await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
            ).scalar_one()
            intent.last_outcome = OutcomeCode.CONFIRMED
            intent.state = DispatchState.SETTLED
            applied = (
                await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
            ).scalar_one()
            applied.posting_ref = posting_ref
            return dataclasses.replace(
                outcome,
                resolution=Resolution.CONFIRMED,
                detail="a positive hit is trustworthy (§13.5)",
            )

        trustworthy = state.last_sent_at is not None and negative_answer_is_trustworthy(
            capabilities=capabilities, last_sent_at=state.last_sent_at, now=now
        )
        if (
            code is QueryAnswer.NOT_FOUND
            and consecutive >= policy.consecutive_not_found
            and (trustworthy)
        ):
            intent = (
                await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment_id))
            ).scalar_one()
            intent.last_outcome = OutcomeCode.REJECTED
            intent.state = DispatchState.SETTLED
            return dataclasses.replace(
                outcome,
                resolution=Resolution.REJECTED,
                detail=f"{consecutive} consecutive NotFound answers, both declared windows "
                f"elapsed (visibility {visibility}, in-flight {capabilities.max_inflight_window})",
            )

        # Still ambiguous. Both remaining answers land here and neither returns early, because the
        # bound below applies to both — and an adapter that can only ever say `Indeterminate` is
        # precisely the case that never resolves on its own. An earlier version returned from the
        # `Indeterminate` arm directly, which made the query loop unbounded for exactly the answer
        # that can repeat forever; the exhaustion test found it.
        if code is QueryAnswer.INDETERMINATE:
            detail_text = (
                "the query did not answer; this never counts toward the consecutive NotFound "
                "requirement, and it breaks the run"
            )
        elif not trustworthy:
            detail_text = (
                f"{consecutive} consecutive NotFound answers, but the declared windows have not "
                "elapsed since the last send"
            )
        else:
            detail_text = (
                f"{consecutive} of {policy.consecutive_not_found} consecutive NotFound answers; "
                "NotFound alone is not sufficient — it means 'not visible to this query yet', "
                "not 'will never be applied'"
            )
        unresolved = dataclasses.replace(outcome, detail=detail_text)

    if unresolved.queries_made >= policy.max_queries:
        return await _route_to_recovery(
            engine,
            adjustment_id=adjustment_id,
            operation_id=state.operation_id,
            reason=RecoveryReason.RECONCILIATION_EXHAUSTED,
            now=now,
            policy=policy,
            report=unresolved,
        )
    return unresolved
