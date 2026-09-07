"""What a posted adjustment can answer about itself — §5.2's exit criterion, as a read.

`IMPLEMENTATION_PLAN.md` §5.2 states the criterion as a question the system must be able to answer:

    A posted adjustment answers: what evidence, which model, who approved, what was computed, by
    which code path.

A criterion phrased as a question is only met when something can be *asked*. This module is that
something: one anchored read, returning one typed answer, so the claim is demonstrated rather than
asserted — and so a later increment that breaks the chain breaks a test rather than a promise.

**Two halves, kept structurally apart, and that separation is the point.** §11's field set
deliberately carries no amount, no evidence text and no model rationale — an audit trail that
duplicated the amount would be a second copy of a number with exactly one owner. So three of the
five answers cannot come from `audit_event` and must be read from the domain tables. A report that
blurred the two would let a reader believe the audit trail *proved* an amount it never recorded.

    - :attr:`AdjustmentProvenance.trail` is what the audit trail attests.
    - :attr:`AdjustmentProvenance.facts` is what the domain tables hold.
    - :attr:`AdjustmentProvenance.not_recorded` is what neither holds, named explicitly.

That third field is the honest one. "No model was involved" and "a model was involved and we failed
to record which" are different states, and a report that rendered both as an empty cell would be the
kind of artefact that looks like evidence and is not.

**Anchored on identity, never on recency.** Every join here is on a key: the adjustment names its
approval, the approval names its proposal, the proposal names its citations. Nothing is resolved by
"the most recent row for this exception", because a superseded resolution and its replacement would
then be indistinguishable — and superseding is a case this system explicitly supports.

**This is a read. It computes nothing and writes nothing.** No amount is derived here; the value
reported is the one M2.4 computed and the database stored.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    AuditEvent,
    Evidence,
    ExceptionRecord,
    TreatmentProposal,
    TreatmentProposalEvidence,
)

__all__ = [
    "AdjustmentFacts",
    "AdjustmentProvenance",
    "AuditedAction",
    "provenance",
]


@dataclasses.dataclass(frozen=True, slots=True)
class AuditedAction:
    """One row of the audit trail, in the shape §11 fixes."""

    occurred_at: dt.datetime
    tool: str
    outcome: str
    principal: str
    scope_granted: str
    approval_decision: str
    approver: str | None
    model: str | None
    region_jurisdiction: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class AdjustmentFacts:
    """What the domain tables hold. **Not attested by the audit trail** — see the module docstring.

    Every value here is read from the row that owns it. Nothing is recomputed, and the amount in
    particular is the stored one: M2.4 is the sole owner of computing it and a second computation
    here would be a second answer.
    """

    exception_id: uuid.UUID
    classification: str
    correlation_id: str

    #: What evidence the model was shown *and cited*. The citation subset, not the whole pack —
    #: what a proposal was shown is provenance, what it cited is the reasoning.
    cited_evidence: tuple[str, ...]

    #: Which model, and under which version. ``None`` where no proposal informed the decision.
    model: str | None
    prompt_hash: str | None
    proposal_treatment: str | None
    proposal_abstained: bool | None

    #: Who approved, what they authorised, and — on an edit — who asked.
    approver: str
    decision: str
    approved_treatment: str
    requested_by: str | None
    decided_at: dt.datetime

    #: What was computed. The stored value, quoted as text so no float ever renders it.
    amount: str
    currency: str
    account_code: str
    period: str

    #: By which path it reached the ledger.
    operation_id: str
    posting_ref: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class AdjustmentProvenance:
    """The complete answer for one adjustment, with its two halves kept apart."""

    adjustment_id: uuid.UUID
    facts: AdjustmentFacts
    trail: tuple[AuditedAction, ...]

    #: Questions this system cannot answer for this adjustment, each with the reason.
    #:
    #: Present and usually non-empty, because §11 asks for fields this repository cannot fill
    #: truthfully today — ``region_jurisdiction`` above all, since no model call is ever made here.
    #: Rendering those as blanks would let absence read as "not applicable"; naming them keeps the
    #: gap visible, which is the only honest treatment of a field a contract requires and a
    #: deployment cannot supply.
    not_recorded: tuple[str, ...]

    @property
    def answers_the_exit_criterion(self) -> bool:
        """Whether all five of §5.2's questions have an answer.

        A property rather than a comment, so the criterion is something a test evaluates rather than
        something a reader takes on trust. Deliberately strict about *which* answers count: the
        model question is satisfied by a recorded model **or** by there being no proposal at all —
        an adjustment a human decided unaided is fully explained without one — but never by a
        proposal whose model went unrecorded.
        """
        evidence_answered = bool(self.facts.cited_evidence) or self.facts.model is None
        model_answered = (self.facts.model is not None) == (
            self.facts.proposal_treatment is not None
        )
        approved = bool(self.facts.approver) and bool(self.facts.decision)
        computed = bool(self.facts.amount) and bool(self.facts.currency)
        path = bool(self.facts.operation_id) and any(action.tool == "post" for action in self.trail)
        return evidence_answered and model_answered and approved and computed and path


async def provenance(session: AsyncSession, *, adjustment_id: uuid.UUID) -> AdjustmentProvenance:
    """Assemble everything known about one adjustment.

    Raises if the adjustment does not exist, rather than returning an empty answer: "nothing is
    recorded about this adjustment" and "there is no such adjustment" are different findings, and an
    auditor handed the first when the second is true has been misled.
    """
    row = (
        await session.execute(
            select(Adjustment, Approval, ExceptionRecord)
            .join(Approval, Adjustment.approval_id == Approval.id)
            .join(ExceptionRecord, Approval.exception_id == ExceptionRecord.id)
            .where(Adjustment.id == adjustment_id)
        )
    ).one_or_none()
    if row is None:
        raise LookupError(f"no adjustment {adjustment_id}")
    adjustment, approval, exception_row = row

    proposal = None
    if approval.treatment_proposal_id is not None:
        proposal = (
            await session.execute(
                select(TreatmentProposal).where(
                    TreatmentProposal.id == approval.treatment_proposal_id
                )
            )
        ).scalar_one_or_none()

    cited: tuple[str, ...] = ()
    if proposal is not None:
        cited = tuple(
            str(value)
            for value in (
                await session.execute(
                    select(Evidence.kind)
                    .join(
                        TreatmentProposalEvidence,
                        TreatmentProposalEvidence.evidence_id == Evidence.id,
                    )
                    .where(TreatmentProposalEvidence.treatment_proposal_id == proposal.id)
                    .order_by(Evidence.kind)
                )
            )
            .scalars()
            .all()
        )

    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.correlation_id == exception_row.correlation_id)
                .order_by(AuditEvent.occurred_at, AuditEvent.created_at)
            )
        )
        .scalars()
        .all()
    )

    facts = AdjustmentFacts(
        exception_id=exception_row.id,
        classification=str(exception_row.classification),
        correlation_id=exception_row.correlation_id,
        cited_evidence=cited,
        model=(f"{proposal.model_id}@{proposal.model_version}" if proposal is not None else None),
        prompt_hash=proposal.prompt_hash if proposal is not None else None,
        proposal_treatment=str(proposal.treatment) if proposal is not None else None,
        proposal_abstained=proposal.abstained if proposal is not None else None,
        approver=approval.principal,
        decision=str(approval.decision),
        approved_treatment=str(adjustment.approved_treatment),
        requested_by=approval.requested_by,
        decided_at=approval.decided_at,
        # Rendered through ``str`` rather than returned as a Decimal, so nothing downstream can
        # coerce it to a float. The money contract is that an amount is exact or it is refused.
        amount=str(adjustment.amount),
        currency=adjustment.currency,
        account_code=adjustment.account_code,
        period=adjustment.period,
        operation_id=adjustment.operation_id,
        posting_ref=adjustment.posting_ref,
    )

    return AdjustmentProvenance(
        adjustment_id=adjustment_id,
        facts=facts,
        trail=tuple(
            AuditedAction(
                occurred_at=event.occurred_at,
                tool=str(event.tool),
                outcome=str(event.outcome),
                principal=event.principal,
                scope_granted=event.scope_granted,
                approval_decision=str(event.approval_decision),
                approver=event.approver,
                model=event.model,
                region_jurisdiction=event.region_jurisdiction,
            )
            for event in events
        ),
        not_recorded=_gaps(events),
    )


def _gaps(events: Sequence[AuditEvent]) -> tuple[str, ...]:
    """The §11 fields no event for this adjustment could fill, each with its reason.

    Computed from the trail rather than hard-coded, so the day a live transport ships and the region
    starts being recorded, this list shortens by itself instead of going stale in a docstring.
    """
    rows = list(events)
    gaps: list[str] = []
    if any(event.model is not None for event in rows) and not any(
        event.region_jurisdiction is not None for event in rows
    ):
        gaps.append(
            "region_jurisdiction: §11 defines it as the processing region of the model call, and "
            "no model call is made in this repository — no transport ships, no provider SDK is a "
            "dependency, and every committed cassette is marked synthesised. Recording a region "
            "would describe a request that never happened (ADR-058)."
        )
    if not any(event.agent_identity is not None for event in rows):
        gaps.append(
            "agent_identity: §11 admits null for deterministic steps, and §2 states this system is "
            "not an agent — the model proposes a treatment code and takes no action, so there is "
            "no agent to identify (ADR-058)."
        )
    return tuple(gaps)
