"""The human gate: approve, edit, reject — with role separation (increment 5.1).

`IMPLEMENTATION_PLAN.md` §5.1: *"No ledger write without a recorded human decision."* The exit
criterion is that **the gate blocks the write, proven by test**, and the mechanism that makes that
true is not in this module at all — it is the composite foreign key M1.2 put on ``adjustment``:

    FOREIGN KEY (approval_id, approved_treatment, approving_principal)
        REFERENCES approval (id, approved_treatment, principal)

A rejection carries ``approved_treatment IS NULL`` and the referencing side is ``NOT NULL``, so a
rejected decision *cannot* be referenced by an adjustment. No trigger, no application check, no
ordering assumption: the database refuses. This module's job is to put the right row in
``approval`` — and to refuse the decisions §16 says a given principal may not take.

**What this module deliberately does not do.** It computes no amount, derives no operation
identifier and writes no ``adjustment``. M2.4 owns every posted amount and ``operations/service.py``
is the only module permitted to write an adjustment row; a guard test enforces both, and this module
is inside the fence rather than exempt from it. An approval authorises a *treatment code*; the money
is computed afterwards, deterministically, from the approved code and ledger data.

**The three refusals, and why each is a real control rather than a policy string.**

1. **Role.** An operator may not approve at all. The operator's job is unsticking a stalled
   dispatch, and a role that can both unstick a posting and authorise its amount is not a separation
   of duties.
2. **Edit countersignature.** §16: *"The approver cannot be the same principal as the requester
   where an edit changed the treatment."* An edit is the case where a human overrides the model's
   proposal, which is exactly where a second pair of eyes is worth the friction. Only a controller
   may authorise an edit, and never one they requested themselves.
3. **Single use.** §14 lists *"Replay of a consumed approval token"* as a named failure with the
   expected behaviour *"Rejected; audit event recorded"*. The token is unique in the database, so a
   replay loses to a constraint rather than to a check somebody might refactor away.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.db.control import (
    Approval,
    ApprovalDecision,
    ExceptionRecord,
    TreatmentCode,
    TreatmentProposal,
)
from ledger_exception_control_plane.security import Principal, Role

__all__ = [
    "ApprovalRecord",
    "ApprovalRefusedError",
    "RefusalReason",
    "record_decision",
]


class RefusalReason(enum.StrEnum):
    """Why a decision was refused. A closed vocabulary, so refusals can be counted and audited.

    Free text would make the audit trail unqueryable and would tempt a caller into rendering a
    reason the client supplied. Each member below is a control §16 or §14 names.
    """

    #: The principal's role may not record an approval decision at all.
    ROLE_MAY_NOT_APPROVE = "role_may_not_approve"

    #: The principal's role may not authorise a treatment different from the one proposed.
    ROLE_MAY_NOT_EDIT = "role_may_not_edit"

    #: §16's countersignature rule: the approver of an edit may not be the requester.
    SELF_COUNTERSIGNED_EDIT = "self_countersigned_edit"

    #: The approval token has already been used. §14's "replay of a consumed approval token".
    TOKEN_ALREADY_USED = "token_already_used"

    #: A decision already exists for this exception at this resolution version.
    ALREADY_DECIDED = "already_decided"

    #: An approval must name the treatment it authorises; a rejection must not.
    TREATMENT_INCONSISTENT_WITH_DECISION = "treatment_inconsistent_with_decision"

    #: The exception does not exist, or the proposal does not belong to it.
    UNKNOWN_SUBJECT = "unknown_subject"


class ApprovalRefusedError(Exception):
    """A decision the gate declined to record, carrying a closed reason."""

    def __init__(self, reason: RefusalReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """What the gate recorded. Returned rather than re-read, so a caller cannot mistake it."""

    approval_id: uuid.UUID
    exception_id: uuid.UUID
    resolution_version: int
    decision: ApprovalDecision
    approved_treatment: TreatmentCode | None
    principal: str
    approval_token: str
    #: Present only on an edited decision: the principal who asked for the different treatment.
    requested_by: str | None = None


def _requires_treatment(decision: ApprovalDecision) -> bool:
    """Whether this decision must name a treatment.

    ``APPROVED`` and ``EDITED`` authorise money to move and must say what was authorised;
    ``REJECTED`` authorises nothing and must not, because a rejection carrying a treatment is
    exactly the row the ``adjustment`` foreign key is designed to make unreferenceable.
    """
    return decision in (ApprovalDecision.APPROVED, ApprovalDecision.EDITED)


async def record_decision(
    session: AsyncSession,
    *,
    exception_id: uuid.UUID,
    resolution_version: int,
    principal: Principal,
    decision: ApprovalDecision,
    approval_token: str,
    now: dt.datetime,
    treatment: TreatmentCode | None = None,
    treatment_proposal_id: uuid.UUID | None = None,
    requested_by: str | None = None,
) -> ApprovalRecord:
    """Record one human decision, or refuse it.

    ``now`` is a parameter for the same reason ``sent_at`` is one on the dispatcher: a decision
    timestamp this module invented would make the audit trail depend on when the code ran.

    The order of the checks is deliberate — cheap local refusals first, then the ones needing the
    database, then the write whose constraint is the real control. A caller gets the most specific
    reason available, and the constraint stays the backstop rather than the error message.
    """
    if not principal.may_approve():
        raise ApprovalRefusedError(
            RefusalReason.ROLE_MAY_NOT_APPROVE,
            f"role {principal.role.value} may not record an approval decision",
        )

    if _requires_treatment(decision) and treatment is None:
        raise ApprovalRefusedError(
            RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION,
            f"a {decision.value} decision must name the treatment it authorises",
        )
    if not _requires_treatment(decision) and treatment is not None:
        raise ApprovalRefusedError(
            RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION,
            "a rejection authorises nothing and must not name a treatment",
        )

    if decision is ApprovalDecision.EDITED and not principal.may_edit_treatment():
        raise ApprovalRefusedError(
            RefusalReason.ROLE_MAY_NOT_EDIT,
            f"role {principal.role.value} may request an edit but may not authorise one",
        )

    exception_row = (
        await session.execute(select(ExceptionRecord).where(ExceptionRecord.id == exception_id))
    ).scalar_one_or_none()
    if exception_row is None:
        raise ApprovalRefusedError(RefusalReason.UNKNOWN_SUBJECT, "no such exception")

    if treatment_proposal_id is not None:
        proposal = (
            await session.execute(
                select(TreatmentProposal).where(TreatmentProposal.id == treatment_proposal_id)
            )
        ).scalar_one_or_none()
        if proposal is None or proposal.exception_id != exception_id:
            raise ApprovalRefusedError(
                RefusalReason.UNKNOWN_SUBJECT,
                "the proposal does not belong to this exception",
            )

    # §16's countersignature rule, checked here for a clear refusal and enforced again by the
    # `approver_is_not_the_requester` check constraint, which is the control that actually holds:
    # an application check is one refactor from being skipped, and this one guards the boundary
    # between a model's suggestion and a human authorising money to move.
    if decision is ApprovalDecision.EDITED:
        if requested_by is None:
            raise ApprovalRefusedError(
                RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION,
                "an edited treatment must record the principal who requested it",
            )
        if requested_by == principal.id:
            raise ApprovalRefusedError(
                RefusalReason.SELF_COUNTERSIGNED_EDIT,
                "an edited treatment must be authorised by a different principal",
            )
    elif requested_by is not None:
        raise ApprovalRefusedError(
            RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION,
            "only an edited treatment carries a requesting principal",
        )

    row = Approval(
        exception_id=exception_id,
        resolution_version=resolution_version,
        treatment_proposal_id=treatment_proposal_id,
        decision=decision,
        approved_treatment=treatment,
        principal=principal.id,
        requested_by=requested_by,
        approval_token=approval_token,
        decided_at=now,
    )
    session.add(row)

    try:
        await session.flush()
    except IntegrityError as error:
        # Two constraints can land here and they mean different things to an operator, so they are
        # reported differently rather than as one generic conflict.
        message = str(error.orig)
        if "approver_is_not_the_requester" in message:
            raise ApprovalRefusedError(
                RefusalReason.SELF_COUNTERSIGNED_EDIT,
                "an edited treatment must be authorised by a different principal",
            ) from error
        if "uq_approval_token" in message:
            raise ApprovalRefusedError(
                RefusalReason.TOKEN_ALREADY_USED,
                "this approval token has already been consumed",
            ) from error
        if "uq_approval_exception_resolution_version" in message:
            raise ApprovalRefusedError(
                RefusalReason.ALREADY_DECIDED,
                "this exception already has a decision at this resolution version",
            ) from error
        raise

    return ApprovalRecord(
        approval_id=row.id,
        exception_id=exception_id,
        resolution_version=resolution_version,
        decision=decision,
        approved_treatment=treatment,
        principal=principal.id,
        approval_token=approval_token,
        requested_by=requested_by,
    )


def role_may_read_queue(principal: Principal) -> bool:
    """Every configured role may read the queue.

    Stated as a function rather than assumed, so the console has one place to ask and a future role
    that should *not* see the queue has one place to be refused.
    """
    return principal.role in (Role.ANALYST, Role.CONTROLLER, Role.OPERATOR)
