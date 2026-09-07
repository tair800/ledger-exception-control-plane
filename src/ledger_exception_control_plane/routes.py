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

from ledger_exception_control_plane import __version__
from ledger_exception_control_plane.audit import (
    NO_AUTHORITY,
    UNRECORDED_CORRELATION_ID,
    approval_scope,
    emit,
    scope_for,
)
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    ApprovalDecision,
    AuditEvent,
    AuditOutcome,
    AuditTool,
    DeadLetter,
    Evidence,
    ExceptionRecord,
    Outbox,
    PostingAttempt,
    RecoveryItem,
    RecoveryResolution,
    ReplayState,
    TreatmentCode,
    TreatmentProposal,
    TreatmentProposalEvidence,
)
from ledger_exception_control_plane.db.models import SettlementLine
from ledger_exception_control_plane.ledger import (
    Fault,
    FaultInjectingLedger,
    SimulatedLedger,
)
from ledger_exception_control_plane.operations import outcome_code
from ledger_exception_control_plane.operations.approval import (
    ApprovalRefusedError,
    RefusalReason,
    record_decision,
)
from ledger_exception_control_plane.operations.dispatcher import (
    DispatchRefusedError,
    dispatch_once,
)
from ledger_exception_control_plane.operations.recovery import (
    RecoveryRefusal,
    RecoveryRefusedError,
    open_items,
    resolve_item,
    stale_items,
)
from ledger_exception_control_plane.operations.retry import replay_dead_letter
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


class EvidenceView(BaseModel):
    """One addressable evidence record, and whether the proposal actually cited it.

    ``cited`` is the field a reviewer needs and the one a naive read would omit. §6.1 requires a
    proposal to reference the evidence it used, and 3.3 validates that every citation is a subset
    of the pack the model was shown — so the interesting question in the console is not *what
    evidence existed* but *which of it the model claimed to rely on*. Returning the pack without
    that distinction would show a reviewer a list and let them assume all of it was cited.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    kind: str
    content: str
    cited: bool


class AuditEventView(BaseModel):
    """One row of the append-only trail, as §11 recorded it.

    Fields the system cannot truthfully know are returned as ``null`` rather than omitted or
    filled: ``agent_identity`` is null because this system is not an agent, and
    ``region_jurisdiction`` is null because no model call is made. ADR-058 took that decision and
    the console must not quietly hide it — a trail that renders an absent field as blank space
    looks like a trail with nothing to say.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    occurred_at: dt.datetime
    principal: str
    agent_identity: str | None
    tool: str
    scope_granted: str
    approval_decision: str
    approver: str | None
    model: str | None
    region_jurisdiction: str | None
    outcome: str
    correlation_id: str


class ExceptionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    classification: str
    status: str
    correlation_id: str
    line: dict[str, str | None]
    #: The pack the model was shown, with the citations it made. Ordered by kind then id, so two
    #: reads of an unchanged exception render identically.
    evidence: list[EvidenceView]
    proposal: dict[str, str | bool | None] | None
    approval: dict[str, str | None] | None
    adjustment: dict[str, str | None] | None
    outbox: dict[str, str | int | None] | None
    attempts: list[PostingAttemptView]
    #: The audit trail for this exception's correlation id, oldest first. Returned on the detail
    #: rather than behind a second request because §10 asks for *full provenance* in one read and
    #: 7.1's exit criterion is that provenance is reachable in two clicks.
    audit: list[AuditEventView]


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
            # **The scope a refused action ran under is the one actually held, never the one
            # attempted.** An operator whose approval is refused because their role may not approve
            # held no approval authority at all, and stamping `approval:operator` on that row would
            # assert an authorisation §16 does not grant — permanently, in an append-only table, in
            # the one field §11 provides for answering this question. A refusal for some other
            # reason keeps the real scope: there the authority was held and the refusal was about
            # something else.
            scope_granted=(
                approval_scope(principal.role.value)
                if principal.may_record_decision()
                else NO_AUTHORITY
            ),
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

    # The pack, and which of it this proposal cited. Two queries rather than a join, because the
    # citation set belongs to *one* proposal and the pack belongs to the exception: an exception
    # with no proposal still has evidence, and a join would have returned nothing for it.
    evidence_rows = (
        (
            await session.execute(
                select(Evidence)
                .where(Evidence.exception_id == exception_id)
                .order_by(Evidence.kind, Evidence.id)
            )
        )
        .scalars()
        .all()
    )
    cited: set[uuid.UUID] = set()
    if proposal is not None:
        cited = set(
            (
                await session.execute(
                    select(TreatmentProposalEvidence.evidence_id).where(
                        TreatmentProposalEvidence.treatment_proposal_id == proposal.id
                    )
                )
            )
            .scalars()
            .all()
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

    # Keyed on the correlation id rather than on the exception id, deliberately: §11's trail spans
    # ingestion through posting and several of its rows belong to no single exception row, so a
    # query by exception id would return a trail with the interesting parts missing. The correlation
    # id is the thing 5.2 built to survive the whole path.
    audit_rows = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.correlation_id == exception_row.correlation_id)
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )
        .scalars()
        .all()
    )

    return ExceptionDetail(
        id=exception_row.id,
        classification=str(exception_row.classification),
        status=str(exception_row.status),
        correlation_id=exception_row.correlation_id,
        evidence=[
            EvidenceView(
                id=row.id,
                kind=str(row.kind),
                content=row.content,
                cited=row.id in cited,
            )
            for row in evidence_rows
        ],
        audit=[
            AuditEventView(
                id=row.id,
                occurred_at=row.occurred_at,
                principal=row.principal,
                agent_identity=row.agent_identity,
                tool=str(row.tool),
                scope_granted=row.scope_granted,
                approval_decision=str(row.approval_decision),
                approver=row.approver,
                model=row.model,
                region_jurisdiction=row.region_jurisdiction,
                outcome=str(row.outcome),
                correlation_id=row.correlation_id,
            )
            for row in audit_rows
        ],
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
            scope_granted=(
                scope_for(AuditTool.RECOVER)
                if principal.may_work_operations_queues()
                else NO_AUTHORITY
            ),
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


# ======================================================================================
# /dlq — what exhausted its retry budget, and the way back (increment 4.3, §15)
# ======================================================================================


class DeadLetterView(BaseModel):
    """One dead-lettered dispatch, with the envelope an operator judges it by.

    **No monetary amount, and that is a schema guarantee rather than a choice made here.** The
    `dlq` table's envelope is JSONB and a check constraint rejects amount-like keys in it, because
    money in JSONB would bypass the money constraints and become the numeric escape hatch the
    schema forbids everywhere else. The amount is reconstructed from `adjustment` at replay time,
    so the console shows an operator *what failed and why* and never invites them to re-price it.

    ``adjustment_id`` is resolved through the outbox row so the console can link a dead letter back
    to the exception it came from in one hop; without it the queue is a list of identifiers an
    operator cannot navigate.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    outbox_id: uuid.UUID
    adjustment_id: uuid.UUID
    operation_id: str
    reason: str
    attempts: int
    replay_state: str
    created_at: dt.datetime
    replayed_at: dt.datetime | None


@router.get("/dlq", response_model=list[DeadLetterView])
async def list_dead_letters(
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
    pending_only: bool = True,
) -> list[DeadLetterView]:
    """The dead-letter queue. **Operator work, so an approver may not see it as their queue.**

    §16 separates the roles: the principal who authorises a posting is not the principal who works
    the failure queues, and 4.4 already enforces the mirror of this rule for recovery. Refusing
    here rather than filtering keeps the reason legible — an analyst is told they lack the
    authority, instead of being shown an empty queue and left to conclude nothing failed.

    ``pending_only`` defaults to true because a replayed entry is history and an operator opening
    the queue wants work. The full list stays reachable, which is what makes the default a
    convenience rather than a hidden filter.
    """
    if not principal.may_work_operations_queues():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the dead-letter queue is worked by the operator role",
        )

    query = (
        select(DeadLetter, Outbox.adjustment_id)
        .join(Outbox, Outbox.id == DeadLetter.outbox_id)
        .order_by(DeadLetter.created_at)
    )
    if pending_only:
        query = query.where(DeadLetter.replay_state == ReplayState.PENDING)

    rows = (await session.execute(query)).all()
    return [
        DeadLetterView(
            id=entry.id,
            outbox_id=entry.outbox_id,
            adjustment_id=adjustment_id,
            # Read off the persisted envelope, never re-derived: 4.1's identifier is the one the
            # original send carried, and recomputing it here would be a second derivation nobody
            # asked for and the one place a re-derived key could diverge from the sent one.
            operation_id=str(entry.envelope.get("operation_id", "")),
            reason=entry.reason,
            attempts=entry.attempts,
            replay_state=str(entry.replay_state),
            created_at=entry.created_at,
            replayed_at=entry.replayed_at,
        )
        for entry, adjustment_id in rows
    ]


class ReplayReportView(BaseModel):
    """What a replay did, in exactly the terms 4.3 recorded it.

    **Every field here exists on ``ReplayReport``.** The first version of this model carried an
    ``applied_count`` read "off the ledger", which sounded like the right thing and was not a field
    the report has — mypy refused it. Worth recording, because a response model that invents a
    field is how a console ends up displaying a number the system never measured.

    ``detail`` is returned because 4.3 writes it for the outcomes it owns, and an operator deciding
    whether to escalate needs the reason rather than only the verdict.
    """

    model_config = ConfigDict(extra="forbid")

    dlq_id: uuid.UUID
    adjustment_id: uuid.UUID
    operation_id: str
    outcome: str
    posting_ref: str | None
    detail: str
    #: Whether this replay closed the entry, as 4.3 defines closure. Derived there, not here.
    resolved: bool


@router.post("/dlq/{dlq_id}/replay", response_model=ReplayReportView)
async def replay_dead_letter_entry(
    dlq_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> ReplayReportView:
    """Re-send one dead-lettered dispatch. **An irreversible financial write, on a human's order.**

    Every safety property is inherited rather than re-implemented here, which is the point:
    :func:`~ledger_exception_control_plane.operations.retry.replay_dead_letter` reads the persisted
    operation identifier instead of re-deriving one, re-reads the persisted instruction instead of
    rebuilding it, and applies the dispatcher's own gates. This route adds authority and a name to
    the order, and nothing else.

    **The principal is required and is the caller's own.** 4.3 made ``--principal`` mandatory on the
    replay command after every event a human-ordered re-send produced recorded ``system``; an HTTP
    route that omitted it would reintroduce exactly that. It is taken from the authenticated
    identity rather than from the request body, so an operator cannot order a replay in someone
    else's name.

    Deliberately not idempotent at the HTTP layer, and deliberately not made so: a second POST is a
    second *order*, and the protection against it duplicating a financial effect is the operation
    identifier and the ledger's capability contract, not a request cache. Making this endpoint
    swallow a repeat would move a financial guarantee into HTTP plumbing, where §13 says it must
    never live.
    """
    if not principal.may_work_operations_queues():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a replay is an operator action",
        )

    report = await replay_dead_letter(
        request.app.state.engine,
        dlq_id=dlq_id,
        adapter=request.app.state.ledger_adapter,
        now=dt.datetime.now(dt.UTC),
        principal=principal.id,
    )
    return ReplayReportView(
        dlq_id=report.dlq_id,
        adjustment_id=report.adjustment_id,
        operation_id=report.operation_id,
        outcome=report.outcome.value,
        posting_ref=report.posting_ref,
        detail=report.detail,
        resolved=report.resolved,
    )


# ======================================================================================
# /meta and /demo — what this instance is, and the control that makes §19.1 visible
# ======================================================================================


class MetaView(BaseModel):
    """What kind of instance this is. Unauthenticated, and carries nothing that could be sensitive.

    Unauthenticated deliberately: the console needs to know whether to render the demo controls
    *before* a principal has authenticated, and version plus a demo flag are not facts worth
    protecting. Nothing here names a dependency, an origin, a principal or a configuration value.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    demo_mode: bool
    #: What the configured ledger calls itself. Displayed so a visitor is told, in the console
    #: itself, that the strong guarantee they are watching rests on a simulated ledger written in
    #: this repository — see OPEN-11. A demo that quietly implied a real provider would be the
    #: overclaim this project exists to avoid.
    ledger_adapter: str


@router.get("/meta", response_model=MetaView)
async def meta(request: Request) -> MetaView:
    """This instance's identity. No authentication, no dependencies, no secrets."""
    settings: Settings = request.app.state.settings
    return MetaView(
        version=__version__,
        demo_mode=settings.demo_mode,
        ledger_adapter=str(request.app.state.ledger_adapter.name),
    )


class InjectedFaultReport(BaseModel):
    """What the injected fault did to the books, and what the system did about it.

    The two are reported separately because their difference is the entire demonstration.
    ``ledger_applied_count`` is the **simulated ledger's own count** for this operation identifier;
    ``recorded_outcome`` is what this system concluded. §19.1's whole subject is that a client
    cannot infer the first from the second, so a report that showed only one of them would be
    showing the visitor the wrong thing.
    """

    model_config = ConfigDict(extra="forbid")

    adjustment_id: uuid.UUID
    operation_id: str
    fault: str
    #: What the caller was told. For the lost-response fault this is ``unknown``, which is the
    #: honest answer and the only one available.
    recorded_outcome: str
    #: How many times the ledger actually applied this operation. **One**, and that is the point.
    ledger_applied_count: int
    #: How many requests reached the ledger. Greater than the applied count when a duplicate was
    #: suppressed, which is how a visitor can tell suppression from a request never arriving.
    ledger_posts_received: int
    explanation: str


@router.post("/demo/exceptions/{exception_id}/inject-fault", response_model=InjectedFaultReport)
async def inject_fault(
    exception_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(_session)],
) -> InjectedFaultReport:
    """Dispatch one approved adjustment through a ledger that commits and then loses the response.

    **This is M7.2's exit criterion made reachable: a visitor triggers the failure §19.1 names and
    sees that no second financial effect is applied.** It reuses 4.5's fault-injection port rather
    than simulating a crash, so what the visitor watches is the same mechanism the kill test
    measures — not a re-enactment of it.

    Two guards, both refusals rather than filters:

    * **It exists only in demo mode.** A fault injector reachable in a deployment doing real work
      is a defect however carefully it is documented, so this returns 404 — not 403 — when
      ``demo_mode`` is false. 403 would confirm the route exists and invite someone to find the
      credential for it; 404 says there is nothing here, which is true.
    * **It is an operator action.** Injecting a fault dispatches a financial write, and §16 puts
      that on the role that works the failure queues rather than on the role that authorises
      postings.

    The count returned is read from the ledger, never from our own rows.
    """
    settings: Settings = request.app.state.settings
    if not settings.demo_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not principal.may_work_operations_queues():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="injecting a fault dispatches a financial write; that is an operator action",
        )

    approval = (
        (
            await session.execute(
                select(Approval)
                .where(Approval.exception_id == exception_id)
                .where(Approval.decision == ApprovalDecision.APPROVED)
                .order_by(Approval.decided_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="nothing is approved for this exception, so there is no posting to fault",
        )

    adjustment = (
        (await session.execute(select(Adjustment).where(Adjustment.approval_id == approval.id)))
        .scalars()
        .first()
    )
    if adjustment is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the approved treatment has not been priced, so there is no posting to fault",
        )

    # A fresh inner ledger per injection, so the count the visitor reads is this demonstration's
    # and not an accumulation across everyone who pressed the button.
    inner = SimulatedLedger()
    faulted = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)

    # A refusal here is the dispatcher working, not failing: it declines an operation that is
    # already finished, already in flight, or whose last outcome was ambiguous — §13.5 forbids
    # re-sending that last one, which is the whole guarantee. Returning 409 with the reason says
    # so; the first version let the exception escape and the console got a 500 for a correct
    # decision, which would have read as a broken demo instead of an enforced rule.
    try:
        result = await dispatch_once(
            request.app.state.engine,
            adjustment_id=adjustment.id,
            adapter=faulted,
            sent_at=dt.datetime.now(dt.UTC),
        )
    except DispatchRefusedError as refusal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "dispatch_refused",
                "message": str(refusal),
                "hint": (
                    "this posting has already reached a state the dispatcher will not send "
                    "from; seed the demonstration again for a posting that is awaiting dispatch"
                ),
            },
        ) from refusal

    return InjectedFaultReport(
        adjustment_id=adjustment.id,
        operation_id=adjustment.operation_id,
        fault=Fault.COMMIT_THEN_LOSE_RESPONSE.value,
        recorded_outcome=outcome_code(result.outcome).value,
        ledger_applied_count=inner.applied_count(adjustment.operation_id),
        ledger_posts_received=inner.posts_received,
        explanation=(
            "The ledger committed the posting and the response was lost, so this system recorded "
            "the outcome as UNKNOWN rather than guessing. It did not retry: an ambiguous "
            "irreversible write never enters the retry path. The count above is the ledger's own, "
            "and it is one."
        ),
    )
