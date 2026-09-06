"""Emitting audit-event contract v1 (increments 4.4 and 5.2).

`PROJECT_SPEC.md` §11 fixes the field set; M1.2 built the table and the trigger that makes it
append-only. This module is the one place that writes to it.

**Why an emitter rather than an ORM call at each site.** Contract v1 is a *portfolio* contract —
seven later repositories copy it — so what matters is that every event has the same shape and that
the shape is decided once. Eleven fields, four of which are only meaningful when a model was
involved, is exactly the sort of structure that drifts when each call site fills it in by hand.

**Increment ownership, stated because it is unusual.** 5.2 owns contract v1 and the requirement that
*every* state transition emits an event. 4.4 owns the events for its own transitions — the plan
lists *"audit events for every attempt, `UNKNOWN`, query result and operator decision"* among 4.4's
deliverables by name — so the emitter is built here and 5.2 extends its coverage rather than
inventing a second one. ADR-056 records the ordering and why.

**What never reaches an audit row.** No amount, no evidence text, no provider message, no token, no
DSN. §11's field set is deliberately about *who did what under which authority with what outcome* —
the financial facts live in `adjustment` and the reasoning in `treatment_proposal`, both of which
the correlation id already joins to. An audit trail that duplicated the amount would be a second
copy of a number with exactly one owner.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    AuditApprovalDecision,
    AuditEvent,
    AuditOutcome,
    AuditTool,
    ExceptionRecord,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode

__all__ = [
    "SYSTEM_PRINCIPAL",
    "UNRECORDED_CORRELATION_ID",
    "correlation_for_adjustment",
    "emit",
    "posting_audit_outcome",
]

#: What an event records when the correlation chain cannot be walked.
#:
#: §11 makes ``correlation_id`` NOT NULL, so there is no "leave it out" option, and inventing a
#: fresh identifier would be worse than admitting the gap: it would look like a trace that simply
#: has no other members. A fixed, obviously-synthetic value is greppable and says what happened.
#: In practice this is unreachable for a dispatched adjustment — the chain adjustment → approval →
#: exception is all NOT NULL foreign keys — and a test asserts the real path is taken.
UNRECORDED_CORRELATION_ID = "lecp-correlation-unrecorded"

#: The principal recorded for a deterministic step nobody authorised individually.
#:
#: §11: *"Authenticated human, or `system`"*. Writing the literal in one place stops it drifting
#: into "SYSTEM", "internal" and "svc" across call sites, which would make the trail unfilterable by
#: exactly the distinction it exists to draw.
SYSTEM_PRINCIPAL = "system"


async def emit(
    session: AsyncSession,
    *,
    tool: AuditTool,
    outcome: AuditOutcome,
    correlation_id: str,
    occurred_at: dt.datetime,
    scope_granted: str,
    principal: str = SYSTEM_PRINCIPAL,
    approval_decision: AuditApprovalDecision = AuditApprovalDecision.NOT_APPLICABLE,
    approver: str | None = None,
    agent_identity: str | None = None,
    model: str | None = None,
    region_jurisdiction: str | None = None,
) -> None:
    """Append one event. Written into the caller's transaction, deliberately.

    The event and the state change it describes commit together or not at all. The alternative — a
    separate transaction — produces exactly two failure modes, both bad: an event for a transition
    that rolled back, or a transition with no event. §11 requires *"every ledger-affecting action
    has at least one event"*, and the only way to mean that is to make the event part of the action.

    ``occurred_at`` is a parameter for the same reason ``sent_at`` is one on the dispatcher: a clock
    reading taken inside this function would make the trail depend on when the code ran rather than
    on when the thing happened.

    ``scope_granted`` is §11's *"authorisation under which the action ran"* — the role for a human
    action, or the named internal capability for a deterministic one. It is required rather than
    defaulted, because an event that cannot say under what authority it happened is not an audit
    record, and a default would let a call site skip the one field that carries the authorisation
    question.
    """
    session.add(
        AuditEvent(
            occurred_at=occurred_at,
            principal=principal,
            agent_identity=agent_identity,
            tool=tool,
            scope_granted=scope_granted,
            approval_decision=approval_decision,
            approver=approver,
            model=model,
            region_jurisdiction=region_jurisdiction,
            outcome=outcome,
            correlation_id=correlation_id,
        )
    )


#: How a persisted posting outcome reads in the audit trail.
#:
#: Four audit outcomes and six posting outcomes, so the mapping is lossy by construction and the
#: trail is not where the detail lives — ``posting_attempt`` holds that, and the correlation id
#: joins the two. What this mapping must get right is the **three-way** distinction §13.5 rests on,
#: and it is the reason ``QUARANTINED`` is used rather than folding ambiguity into failure:
#:
#: - applied → ``SUCCESS``;
#: - definitely not applied → ``FAILURE``, which covers a declination, a throttle that turned the
#:   request away before it could be considered, and a transport failure with no byte written;
#: - **undetermined → ``QUARANTINED``**, meaning held aside for a decision rather than decided.
#:
#: Recording an ``UNKNOWN`` as ``FAILURE`` would put the exact coercion this project exists to
#: prevent into the one record an auditor reads to check it did not happen.
_POSTING_AUDIT_OUTCOME: dict[OutcomeCode, AuditOutcome] = {
    OutcomeCode.CONFIRMED: AuditOutcome.SUCCESS,
    OutcomeCode.REJECTED: AuditOutcome.FAILURE,
    OutcomeCode.THROTTLED: AuditOutcome.FAILURE,
    OutcomeCode.NOT_SENT: AuditOutcome.FAILURE,
    OutcomeCode.UNKNOWN: AuditOutcome.QUARANTINED,
    OutcomeCode.PARTIALLY_APPLIED: AuditOutcome.QUARANTINED,
}


def posting_audit_outcome(code: OutcomeCode) -> AuditOutcome:
    """The audit reading of a posting outcome. Total over the enum, and a test proves it.

    Total rather than defaulted: a new posting outcome must be classified deliberately, and a
    ``dict.get(code, FAILURE)`` would classify the next ambiguous variant as a failure by
    accident — silently, and in the direction that invites a re-send.
    """
    try:
        return _POSTING_AUDIT_OUTCOME[code]
    except KeyError:  # pragma: no cover - the test above makes this unreachable
        raise ValueError(
            f"{code!r} has no audit reading; classify it rather than defaulting, because the "
            "default direction for an unclassified outcome is the unsafe one"
        ) from None


async def correlation_for_adjustment(session: AsyncSession, adjustment_id: uuid.UUID) -> str:
    """The correlation id spanning ingestion to posting, for one adjustment.

    §11: *"Every record carries a correlation id that survives the full path from ingestion to
    ledger posting."* The chain is ``adjustment → approval → exception``, and every link is a NOT
    NULL foreign key — so this is a lookup rather than a search, and a miss means the adjustment
    itself does not exist.
    """
    found = (
        await session.execute(
            select(ExceptionRecord.correlation_id)
            .join(Approval, Approval.exception_id == ExceptionRecord.id)
            .join(Adjustment, Adjustment.approval_id == Approval.id)
            .where(Adjustment.id == adjustment_id)
        )
    ).scalar_one_or_none()
    return found or UNRECORDED_CORRELATION_ID
