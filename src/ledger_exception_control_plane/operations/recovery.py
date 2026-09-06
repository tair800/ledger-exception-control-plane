"""The manual-recovery queue — where the automatic path stops (increment 4.4).

`PROJECT_SPEC.md` §13.5 clause 5 is unusually prescriptive about this queue, and the reason is
stated in the same paragraph:

    A queue is not a control on its own, so the design specifies what the operator actually does.

So a recovery item is not a to-do. It carries the **evidence procedure** — which downstream artefact
must be inspected and what counts as sufficient evidence for each permitted resolution — an SLA that
makes a stale item alertable, and a segregation-of-duties rule that the database enforces rather
than this module.

**Three resolutions, and the third is the honest one.** ``CONFIRMED_BY_EVIDENCE`` and
``REJECTED_BY_EVIDENCE`` say what the evidence showed. ``RESOLVED_UNVERIFIED`` records that *no
evidence was obtainable and a judgement was made anyway* — §13.5 requires it to be reportable *"so
an unverifiable resolution is visible to an auditor rather than indistinguishable from a verified
one"*. It therefore does **not** settle the dispatch: settling would write ``confirmed`` or
``rejected`` into the outbox as though the answer were known, and the constraint
``settled_requires_terminal_outcome`` offers no third option. The operation stays ambiguous, closed
to every automatic path, with a human's name against the judgement.

**What this module does not do.** It never re-sends. Reaching this queue *is* the automatic path
stopping, and a recovery item that could trigger a posting would be the ambiguity gate with an
operator-shaped hole in it. The only write it makes to the money path is recording the posting
reference an operator found — evidence of a posting that already happened, never a new one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.audit import correlation_for_adjustment, emit
from ledger_exception_control_plane.db.control import (
    Adjustment,
    AuditOutcome,
    AuditTool,
    DispatchState,
    Outbox,
    RecoveryItem,
    RecoveryResolution,
    RecoveryState,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
from ledger_exception_control_plane.ledger.port import MAX_POSTING_REF
from ledger_exception_control_plane.security import OPERATIONS_ROLES, Principal

__all__ = [
    "RECOVERY_SCOPE",
    "EvidenceProcedure",
    "RecoveryReason",
    "RecoveryRefusal",
    "RecoveryRefusedError",
    "RecoveryView",
    "evidence_procedure_for",
    "open_item",
    "open_items",
    "resolve_item",
    "stale_items",
]

#: §11's *"authorisation under which the action ran"* for an operator's recovery decision.
RECOVERY_SCOPE: Final = "operations:recover"


class RecoveryReason(enum.StrEnum):
    """Why the automatic path stopped. Closed, because each value implies a different procedure.

    Every member is a branch §13.5 names. There is deliberately no ``OTHER``: a reason with no
    procedure attached would be a queue entry an operator cannot act on, which is the thing clause 5
    exists to prevent.
    """

    #: Neither ``ENFORCES_KEY`` nor ``BY_OPERATION_ID``. §13.5: *"Otherwise route to manual
    #: recovery."* Nothing can be asked and nothing may be re-sent.
    NO_SUPPRESSION_OR_QUERY = "no_suppression_or_query"

    #: ``now - first_send >= idempotency_window``. The provider may no longer hold the key, so a
    #: re-send would be *"an ordinary duplicate write wearing an idempotency header"*.
    RESEND_WINDOW_EXPIRED = "resend_window_expired"

    #: The endpoint a re-send would use cannot be shown to match the one the original send used,
    #: under a scope that is narrower than global. Includes the case where the original endpoint was
    #: never recorded — unproven is not proven.
    RESEND_SCOPE_UNPROVEN = "resend_scope_unproven"

    #: The bounded reconciliation ran out of queries without a definite answer. §13.5: *"On bound
    #: exhaustion … the operation routes to manual recovery. Reconciliation is not total."*
    RECONCILIATION_EXHAUSTED = "reconciliation_exhausted"

    #: Some legs committed and some did not, which is possible only under ``NON_ATOMIC``. §14 sends
    #: this straight here: it is never automatically retried.
    PARTIALLY_APPLIED = "partially_applied"


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceProcedure:
    """What an operator must do before recording a resolution.

    Three fields rather than one paragraph, because §13.5 asks for two different things — *"which
    downstream artefact must be inspected"* and *"what constitutes sufficient evidence for each
    permitted resolution"* — and a single blob makes the second easy to omit.
    """

    #: The artefact to inspect. §13.5 names the plausible ones: the next ledger snapshot, a
    #: statement export, or vendor support confirmation.
    artefact: str

    #: What would justify ``CONFIRMED_BY_EVIDENCE``.
    sufficient_for_confirmed: str

    #: What would justify ``REJECTED_BY_EVIDENCE``.
    sufficient_for_rejected: str

    def render(self, *, operation_id: str) -> str:
        """The text stored on the row, with the identifier the operator will search for."""
        return (
            f"Inspect: {self.artefact}\n"
            f"Search by operation identifier {operation_id}.\n"
            f"Sufficient for confirmed_by_evidence: {self.sufficient_for_confirmed}\n"
            f"Sufficient for rejected_by_evidence: {self.sufficient_for_rejected}\n"
            "If neither can be established, record resolved_unverified. It is a reportable "
            "outcome and is not a failure to follow the procedure."
        )


#: One procedure per reason. Exhaustive over :class:`RecoveryReason`, and a test proves it.
_PROCEDURES: Final[dict[RecoveryReason, EvidenceProcedure]] = {
    RecoveryReason.NO_SUPPRESSION_OR_QUERY: EvidenceProcedure(
        artefact=(
            "the next ledger snapshot for the adjustment's account and period, or a statement "
            "export covering the send time — this adapter cannot be queried by operation "
            "identifier, so the answer has to come from a downstream artefact"
        ),
        sufficient_for_confirmed=(
            "a posting in the snapshot matching the adjustment's account, period, currency and "
            "amount, whose reference is recorded with this resolution"
        ),
        sufficient_for_rejected=(
            "a snapshot taken after the provider's stated settlement cut-off that contains no such "
            "posting, plus vendor confirmation that no request was received"
        ),
    ),
    RecoveryReason.RESEND_WINDOW_EXPIRED: EvidenceProcedure(
        artefact=(
            "the ledger snapshot covering the original send time — do not re-send: the provider's "
            "idempotency window has elapsed and a repeat carries no suppression"
        ),
        sufficient_for_confirmed="a posting bearing this operation identifier, with its reference",
        sufficient_for_rejected=(
            "vendor confirmation that the original request was never applied, dated after the "
            "in-flight window"
        ),
    ),
    RecoveryReason.RESEND_SCOPE_UNPROVEN: EvidenceProcedure(
        artefact=(
            "the deployment record of the endpoint the original send used, and the ledger snapshot "
            "for that endpoint — a key enforced per endpoint or per account says nothing about a "
            "different one"
        ),
        sufficient_for_confirmed="a posting at the original endpoint bearing this identifier",
        sufficient_for_rejected=(
            "confirmation from the original endpoint's operator that the identifier was never seen"
        ),
    ),
    RecoveryReason.RECONCILIATION_EXHAUSTED: EvidenceProcedure(
        artefact=(
            "the recorded reconciliation queries for this operation, then the ledger snapshot — "
            "the queries are on record and say what was already asked and answered"
        ),
        sufficient_for_confirmed="a posting found by any means, with its reference",
        sufficient_for_rejected=(
            "vendor confirmation, or a snapshot taken after both declared windows containing no "
            "such posting; the recorded queries alone were not sufficient, which is why this "
            "item exists"
        ),
    ),
    RecoveryReason.PARTIALLY_APPLIED: EvidenceProcedure(
        artefact=(
            "every leg of the posting in the ledger snapshot, one by one — the adapter reported "
            "that some committed and some did not"
        ),
        sufficient_for_confirmed=(
            "every leg present and balanced, with the reference of the completing posting"
        ),
        sufficient_for_rejected=(
            "every leg absent or reversed, evidenced per leg; a partial state is neither and must "
            "be corrected by a new approved resolution rather than resolved here"
        ),
    ),
}


def evidence_procedure_for(reason: RecoveryReason) -> EvidenceProcedure:
    """The procedure for a reason. Total over the enum, never defaulted.

    A default would produce a queue entry saying "look into it", which is the failure mode §13.5's
    clause 5 is written against.
    """
    return _PROCEDURES[reason]


class RecoveryRefusal(enum.StrEnum):
    """Why a recovery decision was refused. Reported to the caller, recorded in the trail."""

    #: Only the operator role works this queue. §16's separation, from the approval side.
    ROLE_MAY_NOT_RECOVER = "role_may_not_recover"

    #: §13.5: *"the principal resolving an `UNKNOWN` may not be the principal who approved the
    #: original adjustment"*. Also refused by a check constraint; this is the courteous half.
    APPROVER_MAY_NOT_RESOLVE = "approver_may_not_resolve"

    #: The item is already resolved. Resolutions are not revisable — a second judgement would
    #: overwrite the first and the trail would show only the survivor.
    ALREADY_RESOLVED = "already_resolved"

    #: ``CONFIRMED_BY_EVIDENCE`` with no posting reference. The reference *is* the evidence, and a
    #: confirmation without one is an assertion.
    CONFIRMED_WITHOUT_REFERENCE = "confirmed_without_reference"

    #: A posting reference supplied for a resolution that is not a confirmation.
    REFERENCE_WITHOUT_CONFIRMATION = "reference_without_confirmation"

    UNKNOWN_ITEM = "unknown_item"


class RecoveryRefusedError(Exception):
    """A recovery decision was refused. Nothing was written."""

    def __init__(self, refusal: RecoveryRefusal, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal


@dataclasses.dataclass(frozen=True, slots=True)
class RecoveryView:
    """One queue entry, as an operator sees it."""

    id: uuid.UUID
    adjustment_id: uuid.UUID
    operation_id: str
    reason: str
    evidence_procedure: str
    opened_at: dt.datetime
    sla_due_at: dt.datetime
    approving_principal: str
    #: §13.5: *"Recovery items carry an SLA and age; a stale `UNKNOWN` is an alertable condition."*
    overdue: bool


async def open_item(
    session: AsyncSession,
    *,
    adjustment_id: uuid.UUID,
    reason: RecoveryReason,
    opened_at: dt.datetime,
    sla: dt.timedelta,
) -> uuid.UUID | None:
    """Route an operation to manual recovery. Returns ``None`` if it is already queued.

    Written into the **caller's** transaction, unlike the dispatcher's three: the routing decision
    and the item must commit together, because an operation recorded as routed with nothing in the
    queue is an ambiguity nobody will ever look at.

    Idempotent by database rather than by check. ``uq_recovery_queue_open_adjustment`` is a partial
    unique index on open items, so two concurrent reconciliation passes cannot both queue the same
    operation — the second loses the insert and gets ``None``. A read-then-write check would race.

    **The losing insert is contained in a savepoint**, and that was a correction rather than a
    flourish. Rolling the *caller's* transaction back would have been the obvious way to recover a
    failed flush and the wrong one: this function is handed somebody else's transaction, so a
    rollback here discards whatever they had in flight — for the caller that matters, a
    reconciliation pass, that is the resolution it had just decided. A savepoint undoes only the
    insert.
    """
    adjustment = (
        await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
    ).scalar_one()

    procedure = evidence_procedure_for(reason)
    item = RecoveryItem(
        adjustment_id=adjustment_id,
        state=RecoveryState.OPEN,
        reason=reason.value,
        evidence_procedure=procedure.render(operation_id=adjustment.operation_id),
        opened_at=opened_at,
        sla_due_at=opened_at + sla,
        # Not a free copy: the composite foreign key ties it to `adjustment.approving_principal`,
        # which is itself tied to `approval.principal`. That chain is what makes the
        # segregation-of-duties check compare against the real approver rather than against
        # whatever this module supplied.
        approving_principal=adjustment.approving_principal,
    )
    try:
        async with session.begin_nested():
            session.add(item)
            await session.flush()
    except IntegrityError:
        return None

    await emit(
        session,
        tool=AuditTool.RECOVER,
        outcome=AuditOutcome.QUARANTINED,
        correlation_id=await correlation_for_adjustment(session, adjustment_id),
        occurred_at=opened_at,
        scope_granted=RECOVERY_SCOPE,
    )
    return item.id


def _view(item: RecoveryItem, operation_id: str, *, now: dt.datetime) -> RecoveryView:
    return RecoveryView(
        id=item.id,
        adjustment_id=item.adjustment_id,
        operation_id=operation_id,
        reason=str(item.reason),
        evidence_procedure=item.evidence_procedure,
        opened_at=item.opened_at,
        sla_due_at=item.sla_due_at,
        approving_principal=item.approving_principal,
        overdue=now >= item.sla_due_at,
    )


async def open_items(
    session: AsyncSession, *, now: dt.datetime, limit: int = 100
) -> Sequence[RecoveryView]:
    """The open queue, most urgent first. Ordered by SLA, which is how an operator works it."""
    rows = (
        await session.execute(
            select(RecoveryItem, Adjustment.operation_id)
            .join(Adjustment, Adjustment.id == RecoveryItem.adjustment_id)
            .where(RecoveryItem.state == RecoveryState.OPEN)
            .order_by(RecoveryItem.sla_due_at)
            .limit(limit)
        )
    ).all()
    return [_view(item, operation_id, now=now) for item, operation_id in rows]


async def stale_items(
    session: AsyncSession, *, now: dt.datetime, limit: int = 100
) -> Sequence[RecoveryView]:
    """Open items past their SLA — §13.5's *"alertable condition"*, as a query rather than a hope.

    Separate from :func:`open_items` filtered in Python, because this is what an alert reads and an
    alert that scans the whole queue to find nothing is an alert that gets turned off.
    """
    rows = (
        await session.execute(
            select(RecoveryItem, Adjustment.operation_id)
            .join(Adjustment, Adjustment.id == RecoveryItem.adjustment_id)
            .where(RecoveryItem.state == RecoveryState.OPEN, RecoveryItem.sla_due_at <= now)
            .order_by(RecoveryItem.sla_due_at)
            .limit(limit)
        )
    ).all()
    return [_view(item, operation_id, now=now) for item, operation_id in rows]


def _check_reference(resolution: RecoveryResolution, posting_ref: str | None) -> None:
    if resolution is RecoveryResolution.CONFIRMED_BY_EVIDENCE:
        if not posting_ref or not posting_ref.strip():
            raise RecoveryRefusedError(
                RecoveryRefusal.CONFIRMED_WITHOUT_REFERENCE,
                "confirmed_by_evidence requires the posting reference that was found; the "
                "reference is the evidence, and a confirmation without one is an assertion",
            )
        if len(posting_ref) > MAX_POSTING_REF:
            raise RecoveryRefusedError(
                RecoveryRefusal.CONFIRMED_WITHOUT_REFERENCE,
                f"posting reference is {len(posting_ref)} characters and the column holds "
                f"{MAX_POSTING_REF}",
            )
    elif posting_ref is not None:
        raise RecoveryRefusedError(
            RecoveryRefusal.REFERENCE_WITHOUT_CONFIRMATION,
            f"{resolution.value} does not record a posting reference; supplying one would file "
            "evidence of a posting under a resolution that says there was none",
        )


#: How a resolution reads in the audit trail.
#:
#: ``RESOLVED_UNVERIFIED`` is ``ABSTAINED`` and that is the whole point of having four audit
#: outcomes: the operator did not establish what happened, and recording it as a success or a
#: failure would make an unverified judgement indistinguishable from a verified one — which is the
#: single property §13.5 asks this resolution to preserve.
_RESOLUTION_AUDIT_OUTCOME: Final[dict[RecoveryResolution, AuditOutcome]] = {
    RecoveryResolution.CONFIRMED_BY_EVIDENCE: AuditOutcome.SUCCESS,
    RecoveryResolution.REJECTED_BY_EVIDENCE: AuditOutcome.FAILURE,
    RecoveryResolution.RESOLVED_UNVERIFIED: AuditOutcome.ABSTAINED,
}


async def resolve_item(
    engine: AsyncEngine,
    *,
    recovery_id: uuid.UUID,
    principal: Principal,
    resolution: RecoveryResolution,
    now: dt.datetime,
    posting_ref: str | None = None,
) -> RecoveryView:
    """Record an operator's judgement on one ambiguous operation.

    Every refusal below is *also* enforced by the database, and the split is deliberate: the
    constraint is the control and this is the courtesy that gives it a usable message. Two tests
    make the claim checkable — one drives this function, one writes the violation straight at the
    table and watches PostgreSQL refuse it.

    **What settles and what does not.** A verified resolution moves the outbox to its terminal
    outcome; ``RESOLVED_UNVERIFIED`` does not, because ``settled_requires_terminal_outcome`` admits
    only ``confirmed`` and ``rejected`` and the operator has just recorded that neither was
    established. The operation stays ambiguous and permanently closed to the automatic path, with a
    name and a timestamp against the judgement — which is what "reportable" has to mean.
    """
    if principal.role not in OPERATIONS_ROLES:
        raise RecoveryRefusedError(
            RecoveryRefusal.ROLE_MAY_NOT_RECOVER,
            f"role {principal.role.value} may not work the recovery queue; a principal who can "
            "both authorise a posting and adjudicate what happened to it is not a separation "
            "of duties",
        )
    _check_reference(resolution, posting_ref)

    async with AsyncSession(engine) as session, session.begin():
        row = (
            await session.execute(
                select(RecoveryItem, Adjustment.operation_id)
                .join(Adjustment, Adjustment.id == RecoveryItem.adjustment_id)
                .where(RecoveryItem.id == recovery_id)
                .with_for_update(of=RecoveryItem)
            )
        ).one_or_none()
        if row is None:
            raise RecoveryRefusedError(
                RecoveryRefusal.UNKNOWN_ITEM, f"no recovery item {recovery_id}"
            )
        item, operation_id = row

        if RecoveryState(item.state) is not RecoveryState.OPEN:
            raise RecoveryRefusedError(
                RecoveryRefusal.ALREADY_RESOLVED,
                f"recovery item {recovery_id} is already resolved as {item.resolution}; a second "
                "judgement would overwrite the first and the trail would show only the survivor",
            )
        if principal.id == item.approving_principal:
            raise RecoveryRefusedError(
                RecoveryRefusal.APPROVER_MAY_NOT_RESOLVE,
                "the principal who approved this adjustment may not judge what happened to it "
                "(PROJECT_SPEC 13.5)",
            )

        item.state = RecoveryState.RESOLVED
        item.resolution = resolution
        item.resolved_by = principal.id
        item.resolved_at = now

        if resolution is not RecoveryResolution.RESOLVED_UNVERIFIED:
            intent = (
                await session.execute(
                    select(Outbox).where(Outbox.adjustment_id == item.adjustment_id)
                )
            ).scalar_one()
            settled = (
                OutcomeCode.CONFIRMED
                if resolution is RecoveryResolution.CONFIRMED_BY_EVIDENCE
                else OutcomeCode.REJECTED
            )
            intent.last_outcome = settled
            intent.state = DispatchState.SETTLED

            if posting_ref is not None:
                applied = (
                    await session.execute(
                        select(Adjustment).where(Adjustment.id == item.adjustment_id)
                    )
                ).scalar_one()
                # Evidence of a posting that already happened. This module never causes one.
                applied.posting_ref = posting_ref

        await emit(
            session,
            tool=AuditTool.RECOVER,
            outcome=_RESOLUTION_AUDIT_OUTCOME[resolution],
            correlation_id=await correlation_for_adjustment(session, item.adjustment_id),
            occurred_at=now,
            scope_granted=RECOVERY_SCOPE,
            principal=principal.id,
        )
        view = _view(item, operation_id, now=now)

    return view
