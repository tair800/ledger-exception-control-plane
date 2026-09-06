"""The `/api/v1` surface: the human gate, the recovery queue, and the reads the console needs.

Increments 5.1 (the gate) and 4.4 (`/recovery`).

`PROJECT_SPEC.md` §10 sketches the surface; this module implements the part M5.1 owns:

    POST /exceptions/{id}/approve · /reject — requires an authenticated principal; returns the
    claimed idempotency key.

**Authorization is server-side and fails closed.** The client sends a bearer token and nothing else
about who it is: the role comes from the registry, never from the request body, and a request with
no token, an unknown token or an insufficient role is refused before anything is read. There is no
"principal" field a caller could set — a control plane that let the client name its own actor would
be recording a claim rather than a fact, and the audit trail would be worthless.

**The gate refuses; it does not compute.** No route here calculates an amount, derives an operation
identifier, or writes an ``adjustment``. Approving authorises a *treatment code*; M2.4 turns that
into money afterwards, deterministically. Guard tests hold both properties.

**No route posts to a ledger.** ``/recovery`` records what an operator *found*, never what the
system should now do about it: there is no re-send endpoint, and adding one would put an
operator-shaped hole in the ambiguity gate. §13.5's manual branch is where the automatic path
*stops*, and the HTTP surface has to mean that too.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.audit import UNRECORDED_CORRELATION_ID, emit
from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    ApprovalDecision,
    AuditOutcome,
    AuditTool,
    ExceptionRecord,
    Outbox,
    PostingAttempt,
    RecoveryItem,
    RecoveryResolution,
    TreatmentCode,
    TreatmentProposal,
)
from ledger_exception_control_plane.db.models import SettlementLine
from ledger_exception_control_plane.operations.approval import (
    APPROVAL_SCOPE,
    ApprovalRefusedError,
    RefusalReason,
    record_decision,
)
from ledger_exception_control_plane.operations.recovery import (
    RECOVERY_SCOPE,
    RecoveryRefusal,
    RecoveryRefusedError,
    open_items,
    resolve_item,
    stale_items,
)
from ledger_exception_control_plane.security import Principal, PrincipalRegistry

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["control-plane"])

#: HTTP status for each refusal. A refusal an operator can fix by acting differently is 403 or 409;
#: one caused by asking about something that is not there is 404.
_REFUSAL_STATUS: Final[dict[RefusalReason, int]] = {
    RefusalReason.ROLE_MAY_NOT_APPROVE: status.HTTP_403_FORBIDDEN,
    RefusalReason.ROLE_MAY_NOT_EDIT: status.HTTP_403_FORBIDDEN,
    RefusalReason.SELF_COUNTERSIGNED_EDIT: status.HTTP_403_FORBIDDEN,
    RefusalReason.TOKEN_ALREADY_USED: status.HTTP_409_CONFLICT,
    RefusalReason.ALREADY_DECIDED: status.HTTP_409_CONFLICT,
    RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION: status.HTTP_422_UNPROCESSABLE_ENTITY,
    RefusalReason.UNKNOWN_SUBJECT: status.HTTP_404_NOT_FOUND,
    RefusalReason.SUPERSESSION_BLOCKED: status.HTTP_409_CONFLICT,
}

#: The same mapping for the recovery queue's refusals. Kept separate rather than merged: the two
#: vocabularies belong to different increments and different roles, and one dictionary keyed by two
#: enums would make a missing entry silently fall through to whichever default a merge introduced.
_RECOVERY_STATUS: Final[dict[RecoveryRefusal, int]] = {
    RecoveryRefusal.ROLE_MAY_NOT_RECOVER: status.HTTP_403_FORBIDDEN,
    RecoveryRefusal.APPROVER_MAY_NOT_RESOLVE: status.HTTP_403_FORBIDDEN,
    RecoveryRefusal.ALREADY_RESOLVED: status.HTTP_409_CONFLICT,
    RecoveryRefusal.CONFIRMED_WITHOUT_REFERENCE: status.HTTP_422_UNPROCESSABLE_ENTITY,
    RecoveryRefusal.REFERENCE_WITHOUT_CONFIRMATION: status.HTTP_422_UNPROCESSABLE_ENTITY,
    RecoveryRefusal.UNKNOWN_ITEM: status.HTTP_404_NOT_FOUND,
}


def _registry(request: Request) -> PrincipalRegistry:
    return request.app.state.principals  # type: ignore[no-any-return]


async def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the bearer token to a configured principal, or refuse.

    ``WWW-Authenticate: Bearer`` on the 401 because a client that gets an unadorned 401 cannot tell
    which scheme to use. The message never says whether the token was absent, malformed or simply
    unknown — distinguishing those turns the endpoint into an oracle for guessing tokens.
    """
    unauthorised = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="an authenticated principal is required",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorised

    principal = _registry(request).authenticate(authorization[7:].strip())
    if principal is None:
        raise unauthorised
    return principal


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(request.app.state.engine) as session:
        yield session


class DecisionRequest(BaseModel):
    """What a caller may say. Note what is absent: the principal.

    ``extra="forbid"`` so a client cannot smuggle a ``principal`` field past the model and have some
    later refactor read it. The actor is resolved from the token, always.
    """

    model_config = ConfigDict(extra="forbid")

    resolution_version: int = Field(ge=1)
    approval_token: str = Field(min_length=8, max_length=64)
    treatment: TreatmentCode | None = None
    treatment_proposal_id: uuid.UUID | None = None
    requested_by: str | None = Field(default=None, max_length=128)


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: uuid.UUID
    exception_id: uuid.UUID
    resolution_version: int
    decision: str
    approved_treatment: str | None
    principal: str
    #: §10: the approve endpoint "returns the claimed idempotency key".
    approval_token: str


class ExceptionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    classification: str
    status: str
    psp_reference: str | None
    currency: str | None
    amount: str | None
    correlation_id: str
    has_proposal: bool
    decided: bool


class PostingAttemptView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_no: int
    state: str
    outcome: str | None
    sent_at: dt.datetime
    posting_ref: str | None


class ExceptionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    classification: str
    status: str
    correlation_id: str
    line: dict[str, str | None]
    proposal: dict[str, str | bool | None] | None
    approval: dict[str, str | None] | None
    adjustment: dict[str, str | None] | None
    outbox: dict[str, str | int | None] | None
    attempts: list[PostingAttemptView]


async def _audit_refusal(
    request: Request,
    *,
    tool: AuditTool,
    scope_granted: str,
    principal: Principal,
    correlation_id: str,
) -> None:
    """Record that the system refused, in a transaction of its own.

    §14 requires a refused action to leave a trace — *"Rejected; audit event recorded"* — and the
    service that refuses cannot write one: it raises inside the caller's transaction, and that
    transaction is rolled back precisely because the action did not happen. An event written there
    would roll back with it.

    So the refusal is recorded **here**, at the boundary that owns an engine of its own, after the
    rollback and before the response. The limit is worth stating plainly: a future caller reaching
    the service directly — a worker, a command line — would refuse without this event, and would
    have to record its own. There is one boundary today and this is it.
    """
    async with AsyncSession(request.app.state.engine) as audit_session, audit_session.begin():
        await emit(
            audit_session,
            tool=tool,
            outcome=AuditOutcome.FAILURE,
            correlation_id=correlation_id,
            occurred_at=dt.datetime.now(tz=dt.UTC),
            scope_granted=scope_granted,
            principal=principal.id,
        )


async def _correlation_of(session: AsyncSession, exception_id: uuid.UUID) -> str:
    found = (
        await session.execute(
            select(ExceptionRecord.correlation_id).where(ExceptionRecord.id == exception_id)
        )
    ).scalar_one_or_none()
    return found or UNRECORDED_CORRELATION_ID


async def _decide(
    request: Request,
    session: AsyncSession,
    *,
    exception_id: uuid.UUID,
    body: DecisionRequest,
    principal: Principal,
    decision: ApprovalDecision,
) -> DecisionResponse:
    try:
        record = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=body.resolution_version,
            principal=principal,
            decision=decision,
            approval_token=body.approval_token,
            now=dt.datetime.now(tz=dt.UTC),
            treatment=body.treatment,
            treatment_proposal_id=body.treatment_proposal_id,
            requested_by=body.requested_by,
        )
    except ApprovalRefusedError as refusal:
        await session.rollback()
        await _audit_refusal(
            request,
            tool=AuditTool.APPROVE,
            scope_granted=f"{APPROVAL_SCOPE}:{principal.role.value}",
            principal=principal,
            correlation_id=await _correlation_of(session, exception_id),
        )
        raise HTTPException(
            status_code=_REFUSAL_STATUS[refusal.reason],
            detail={"reason": refusal.reason.value},
        ) from refusal

    await session.commit()
    return DecisionResponse(
        approval_id=record.approval_id,
        exception_id=record.exception_id,
        resolution_version=record.resolution_version,
        decision=record.decision.value,
        approved_treatment=(
            record.approved_treatment.value if record.approved_treatment is not None else None
        ),
        principal=record.principal,
        approval_token=record.approval_token,
    )


@router.post("/exceptions/{exception_id}/approve", response_model=DecisionResponse)
async def approve(
    exception_id: uuid.UUID,
    body: DecisionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
) -> DecisionResponse:
    """Authorise the proposed treatment. An edit is a different verb, below."""
    return await _decide(
        request,
        session,
        exception_id=exception_id,
        body=body,
        principal=principal,
        decision=ApprovalDecision.APPROVED,
    )


@router.post("/exceptions/{exception_id}/edit", response_model=DecisionResponse)
async def edit(
    exception_id: uuid.UUID,
    body: DecisionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
) -> DecisionResponse:
    """Authorise a treatment **different** from the one proposed.

    A separate route rather than a flag on ``approve``, because it carries a different authorisation
    rule: controller only, and never the principal who requested it. A flag would have made the
    stricter path reachable by forgetting to set it.
    """
    return await _decide(
        request,
        session,
        exception_id=exception_id,
        body=body,
        principal=principal,
        decision=ApprovalDecision.EDITED,
    )


@router.post("/exceptions/{exception_id}/reject", response_model=DecisionResponse)
async def reject(
    exception_id: uuid.UUID,
    body: DecisionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
) -> DecisionResponse:
    """Decline. Authorises nothing, and must not name a treatment."""
    return await _decide(
        request,
        session,
        exception_id=exception_id,
        body=body,
        principal=principal,
        decision=ApprovalDecision.REJECTED,
    )


@router.get("/exceptions", response_model=list[ExceptionSummary])
async def list_exceptions(
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
    limit: int = 50,
) -> list[ExceptionSummary]:
    """The queue. Every configured role may read it; only some may act on it."""
    rows = (
        await session.execute(
            select(ExceptionRecord, SettlementLine)
            .join(SettlementLine, ExceptionRecord.settlement_line_id == SettlementLine.id)
            .order_by(ExceptionRecord.created_at.desc())
            .limit(min(limit, 200))
        )
    ).all()

    summaries: list[ExceptionSummary] = []
    for exception_row, line in rows:
        proposal = (
            await session.execute(
                select(TreatmentProposal.id).where(
                    TreatmentProposal.exception_id == exception_row.id
                )
            )
        ).first()
        approval = (
            await session.execute(
                select(Approval.id).where(Approval.exception_id == exception_row.id)
            )
        ).first()
        summaries.append(
            ExceptionSummary(
                id=exception_row.id,
                classification=str(exception_row.classification),
                status=str(exception_row.status),
                psp_reference=line.psp_reference,
                currency=line.currency,
                amount=str(line.amount),
                correlation_id=exception_row.correlation_id,
                has_proposal=proposal is not None,
                decided=approval is not None,
            )
        )
    return summaries


@router.get("/exceptions/{exception_id}", response_model=ExceptionDetail)
async def exception_detail(
    exception_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
) -> ExceptionDetail:
    """Full provenance for one exception: evidence, proposal, decision, money, dispatch.

    §10: *"full provenance: evidence, proposal, approval, adjustment, audit trail"*. This is the
    read the console's detail view is built on, and the reason it is one request rather than six is
    that a reviewer following a posting backwards should not have to assemble it themselves.
    """
    exception_row = (
        await session.execute(select(ExceptionRecord).where(ExceptionRecord.id == exception_id))
    ).scalar_one_or_none()
    if exception_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such exception")

    line = (
        await session.execute(
            select(SettlementLine).where(SettlementLine.id == exception_row.settlement_line_id)
        )
    ).scalar_one()

    proposal = (
        (
            await session.execute(
                select(TreatmentProposal)
                .where(TreatmentProposal.exception_id == exception_id)
                .order_by(TreatmentProposal.proposed_at.desc())
            )
        )
        .scalars()
        .first()
    )

    approval = (
        (
            await session.execute(
                select(Approval)
                .where(Approval.exception_id == exception_id)
                .order_by(Approval.decided_at.desc())
            )
        )
        .scalars()
        .first()
    )

    adjustment = None
    outbox = None
    attempts: list[PostingAttemptView] = []
    if approval is not None:
        adjustment = (
            (await session.execute(select(Adjustment).where(Adjustment.approval_id == approval.id)))
            .scalars()
            .first()
        )

    if adjustment is not None:
        outbox = (
            (await session.execute(select(Outbox).where(Outbox.adjustment_id == adjustment.id)))
            .scalars()
            .first()
        )
        attempt_rows = (
            (
                await session.execute(
                    select(PostingAttempt)
                    .where(PostingAttempt.adjustment_id == adjustment.id)
                    .order_by(PostingAttempt.attempt_no)
                )
            )
            .scalars()
            .all()
        )
        attempts = [
            PostingAttemptView(
                attempt_no=row.attempt_no,
                state=str(row.state),
                outcome=str(row.outcome) if row.outcome is not None else None,
                sent_at=row.sent_at,
                posting_ref=row.posting_ref,
            )
            for row in attempt_rows
        ]

    return ExceptionDetail(
        id=exception_row.id,
        classification=str(exception_row.classification),
        status=str(exception_row.status),
        correlation_id=exception_row.correlation_id,
        line={
            "psp_reference": line.psp_reference,
            "merchant_reference": line.merchant_reference,
            "transaction_type": line.transaction_type,
            "amount": str(line.amount),
            "currency": line.currency,
            "value_date": line.value_date.isoformat(),
        },
        proposal=(
            {
                "id": str(proposal.id),
                "treatment": str(proposal.treatment),
                "confidence": str(proposal.confidence),
                "rationale": proposal.rationale,
                "abstained": proposal.abstained,
                "model_id": proposal.model_id,
                "model_version": proposal.model_version,
            }
            if proposal is not None
            else None
        ),
        approval=(
            {
                "id": str(approval.id),
                "decision": str(approval.decision),
                "approved_treatment": (
                    str(approval.approved_treatment)
                    if approval.approved_treatment is not None
                    else None
                ),
                "principal": approval.principal,
                "requested_by": approval.requested_by,
                "decided_at": approval.decided_at.isoformat(),
            }
            if approval is not None
            else None
        ),
        adjustment=(
            {
                "id": str(adjustment.id),
                "amount": str(adjustment.amount),
                "currency": adjustment.currency,
                "account_code": adjustment.account_code,
                "period": adjustment.period,
                "operation_id": adjustment.operation_id,
                "posting_ref": adjustment.posting_ref,
            }
            if adjustment is not None
            else None
        ),
        outbox=(
            {
                "state": str(outbox.state),
                "last_outcome": str(outbox.last_outcome) if outbox.last_outcome else None,
                "attempt_count": outbox.attempt_count,
            }
            if outbox is not None
            else None
        ),
        attempts=attempts,
    )


# ======================================================================================
# /recovery — where the automatic path stopped (increment 4.4, §13.5 clause 5)
# ======================================================================================


class RecoveryItemView(BaseModel):
    """One queue entry, with the procedure attached rather than referenced.

    ``evidence_procedure`` is returned in full because §13.5's point is that the operator is told
    what to inspect and what would be sufficient. A queue that returned a reason code and expected
    the operator to look the procedure up somewhere is the "queue is not a control" failure with an
    extra step.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    adjustment_id: uuid.UUID
    operation_id: str
    reason: str
    evidence_procedure: str
    opened_at: dt.datetime
    sla_due_at: dt.datetime
    approving_principal: str
    #: §13.5: *"a stale `UNKNOWN` is an alertable condition"*.
    overdue: bool


class ResolveRequest(BaseModel):
    """An operator's judgement. Note what is absent: the principal, and any instruction to re-send.

    ``posting_ref`` is required by ``confirmed_by_evidence`` and refused by everything else — it is
    the evidence, not a note. The service enforces that; this model does not, because a 422 from
    Pydantic would not say *why* and this refusal is one an operator has to understand.
    """

    model_config = ConfigDict(extra="forbid")

    resolution: RecoveryResolution
    posting_ref: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("/recovery", response_model=list[RecoveryItemView])
async def list_recovery(
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
    stale: bool = False,
    limit: int = 50,
) -> list[RecoveryItemView]:
    """The operator queue, most urgent first. ``?stale=true`` returns only items past their SLA.

    Readable by every configured role, like the exception queue: seeing that an operation is stuck
    is not the same authority as judging what happened to it, and an analyst who cannot see it
    cannot escalate it either.
    """
    now = dt.datetime.now(tz=dt.UTC)
    query = stale_items if stale else open_items
    return [
        RecoveryItemView(
            id=item.id,
            adjustment_id=item.adjustment_id,
            operation_id=item.operation_id,
            reason=item.reason,
            evidence_procedure=item.evidence_procedure,
            opened_at=item.opened_at,
            sla_due_at=item.sla_due_at,
            approving_principal=item.approving_principal,
            overdue=item.overdue,
        )
        for item in await query(session, now=now, limit=min(limit, 200))
    ]


@router.post("/recovery/{recovery_id}/resolve", response_model=RecoveryItemView)
async def resolve_recovery(
    recovery_id: uuid.UUID,
    body: ResolveRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> RecoveryItemView:
    """Record what the operator found. **This never causes a posting.**

    Takes the engine rather than the request-scoped session because the service opens its own
    transaction — it locks the queue row, settles the dispatch and appends the audit event
    together, and a handler-owned session would let the route commit half of that.
    """
    try:
        item = await resolve_item(
            request.app.state.engine,
            recovery_id=recovery_id,
            principal=principal,
            resolution=body.resolution,
            now=dt.datetime.now(tz=dt.UTC),
            posting_ref=body.posting_ref,
        )
    except RecoveryRefusedError as refusal:
        async with AsyncSession(request.app.state.engine) as lookup:
            item_correlation = (
                await lookup.execute(
                    select(ExceptionRecord.correlation_id)
                    .join(Approval, Approval.exception_id == ExceptionRecord.id)
                    .join(Adjustment, Adjustment.approval_id == Approval.id)
                    .join(RecoveryItem, RecoveryItem.adjustment_id == Adjustment.id)
                    .where(RecoveryItem.id == recovery_id)
                )
            ).scalar_one_or_none() or UNRECORDED_CORRELATION_ID
        await _audit_refusal(
            request,
            tool=AuditTool.RECOVER,
            scope_granted=RECOVERY_SCOPE,
            principal=principal,
            correlation_id=item_correlation,
        )
        raise HTTPException(
            status_code=_RECOVERY_STATUS[refusal.refusal],
            detail={"reason": refusal.refusal.value, "message": str(refusal)},
        ) from refusal

    return RecoveryItemView(
        id=item.id,
        adjustment_id=item.adjustment_id,
        operation_id=item.operation_id,
        reason=item.reason,
        evidence_procedure=item.evidence_procedure,
        opened_at=item.opened_at,
        sla_due_at=item.sla_due_at,
        approving_principal=item.approving_principal,
        overdue=item.overdue,
    )
