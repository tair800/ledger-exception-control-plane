"""Assemble, ask, record (increment 3.3). The only module here that touches a database.

The order matters and is not negotiable. Evidence is persisted **before** the model is asked,
because the proposal's citations are foreign keys into it: a proposal recorded against evidence that
was never written would be a provenance record pointing at nothing, which is the exact failure the
association table exists to prevent. Then the model is asked. Then, in one transaction, the proposal
and its citations are written.

**Nothing is written unless the answer is usable.** An unreachable provider and an invalid answer
both leave the database exactly as it was apart from the evidence rows, and those are facts about
the exception rather than about the model — they are true whether or not anybody ever asked a
question. The exception itself is untouched in every branch: still open, still unproposed, still
waiting for a human, which is what NFR-11 asks for.

**The deterministic path is never in the transaction.** This module reads ``settlement_line``,
``ledger_entry`` and ``exception`` and writes only ``evidence``, ``treatment_proposal`` and
``treatment_proposal_evidence``. Matching and classification cannot be blocked by a model call
because nothing here locks anything they write.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import uuid
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.db.control import (
    Evidence,
    ExceptionClassification,
    ExceptionRecord,
    ExceptionStatus,
    TreatmentProposalEvidence,
)
from ledger_exception_control_plane.db.control import (
    TreatmentProposal as TreatmentProposalRow,
)
from ledger_exception_control_plane.db.models import LedgerEntry, MatchResult, SettlementLine
from ledger_exception_control_plane.llm.evidence import (
    CandidateEntryFact,
    EvidencePack,
    ExceptionSubject,
    assemble_evidence,
)
from ledger_exception_control_plane.llm.flow import (
    ProposalOutcome,
    ProposalStatus,
    propose_treatment,
)
from ledger_exception_control_plane.llm.port import TreatmentProposer
from ledger_exception_control_plane.matching.policy import DEFAULT_POLICY, TolerancePolicy

__all__ = [
    "ExceptionNotFoundError",
    "ExceptionNotOpenError",
    "ProposalRecord",
    "propose_for_exception",
]


class ExceptionNotFoundError(LookupError):
    """The exception does not exist, or is not in a state that can carry a proposal."""


class ExceptionNotOpenError(ValueError):
    """The exception has been resolved. A model recommendation would be advice after the fact.

    A reviewer proposed on a ``resolved`` exception twice and got two contradictory treatments
    attached to a closed case, ordered only by a caller-supplied timestamp. The docstring above
    already claimed a state check; there was not one.
    """


#: A processing region, as §11 requires it: non-empty, and not whitespace pretending to be a value.
#:
#: Validated because the argument exists to satisfy a compliance record, and a reviewer showed the
#: empty string, three spaces and ``not-a-region`` all being accepted into it. A closed vocabulary
#: would be better and is not this increment's to invent — the sibling fields in the table
#: (``prompt_hash``, ``rule_id``) at least check shape, and this now does too.
_MIN_JURISDICTION: Final = 2


@dataclasses.dataclass(frozen=True, slots=True)
class ProposalRecord:
    """What the run did. ``proposal_id`` is set only when a row was written."""

    outcome: ProposalOutcome
    evidence_ids: tuple[uuid.UUID, ...]
    proposal_id: uuid.UUID | None = None


async def propose_for_exception(
    db: AsyncEngine,
    proposer: TreatmentProposer,
    exception_id: uuid.UUID,
    *,
    region_jurisdiction: str,
    proposed_at: dt.datetime,
    policy: TolerancePolicy = DEFAULT_POLICY,
) -> ProposalRecord:
    """Assemble evidence for one exception, ask one provider, and record a usable answer.

    ``region_jurisdiction`` is a required argument with no default, and that is deliberate: §11
    makes the processing region of the model call part of the audit contract, and this layer cannot
    know it — the transport that will eventually choose an endpoint is 3.4's, and guessing a
    jurisdiction into a compliance record would be worse than being told.

    ``proposed_at`` is likewise passed in rather than read from a clock. ``created_at`` is the
    database's own row-lifecycle fact; ``proposed_at`` is a business time the caller owns, and a
    ``now()`` in here would make the canonical output of this module depend on when it ran.
    """
    if len(region_jurisdiction.strip()) < _MIN_JURISDICTION:
        raise ValueError(f"not a processing region: {region_jurisdiction!r}")
    if proposed_at.tzinfo is None or proposed_at.utcoffset() is None:
        # A naive datetime is silently reinterpreted in the server's zone. A reviewer passed
        # `datetime(2026, 9, 1, 12, 0)` on a UTC+4 machine and it stored as 08:00Z — the same class
        # of silent shift the project spent an increment removing from money, in the field whose
        # docstring says it exists so the output does not depend on when it ran.
        raise ValueError("proposed_at must be timezone-aware")

    async with AsyncSession(db) as session, session.begin():
        subject = await _load_subject(session, exception_id)
        candidates = await _load_candidates(session, subject)
        evidence = assemble_evidence(subject, candidates, policy)
        await _persist_evidence(session, subject, evidence)

    # Outside the transaction, deliberately. A model call takes seconds and may take a timeout's
    # worth of them; holding a transaction open across it would pin a connection and a snapshot for
    # the duration, for no benefit — the evidence is already durable and the ids are stable.
    outcome = await propose_treatment(proposer, subject, evidence)

    if outcome.status is not ProposalStatus.PROPOSED:
        return ProposalRecord(
            outcome=outcome, evidence_ids=tuple(i.evidence_id for i in evidence.items)
        )

    async with AsyncSession(db) as session, session.begin():
        proposal_id = await _persist_proposal(
            session,
            subject=subject,
            outcome=outcome,
            proposer=proposer,
            region_jurisdiction=region_jurisdiction,
            proposed_at=proposed_at,
        )

    return ProposalRecord(
        outcome=outcome,
        evidence_ids=tuple(i.evidence_id for i in evidence.items),
        proposal_id=proposal_id,
    )


async def _load_subject(session: AsyncSession, exception_id: uuid.UUID) -> ExceptionSubject:
    """Read the exception and its settlement line. Joined on the key, never on resemblance.

    One row, reached by primary key and then by the exception's own foreign key. There is no query
    here that could return a line belonging to a different exception, which is the first half of the
    cross-exception isolation argument — the second half is that the candidate query below is
    filtered by this line's own facts.
    """
    row = (
        await session.execute(
            sa.select(ExceptionRecord, SettlementLine)
            .join(SettlementLine, SettlementLine.id == ExceptionRecord.settlement_line_id)
            .where(ExceptionRecord.id == exception_id)
        )
    ).one_or_none()

    if row is None:
        raise ExceptionNotFoundError(f"no exception {exception_id}")

    record, line = row
    if ExceptionStatus(record.status) is not ExceptionStatus.OPEN:
        raise ExceptionNotOpenError(
            f"exception {exception_id} is {record.status}; a resolved case takes no new proposal"
        )
    return ExceptionSubject(
        exception_id=record.id,
        # Coerced rather than trusted. The column is ``String(32)`` with a check constraint and an
        # enum *annotation*, so SQLAlchemy hands back a plain string — the M3.1 lesson applied one
        # layer down: a bare string that compares equal to a member is not a member, and everything
        # downstream reads ``.value`` off this.
        classification=ExceptionClassification(record.classification),
        settlement_line_id=line.id,
        psp_reference=line.psp_reference,
        merchant_reference=line.merchant_reference,
        transaction_type=line.transaction_type,
        amount=line.amount,
        currency=line.currency,
        value_date=line.value_date,
    )


async def _load_candidates(
    session: AsyncSession, subject: ExceptionSubject
) -> list[CandidateEntryFact]:
    """Unconsumed ledger entries in the same currency. The tolerance test is the assembler's.

    Two filters in SQL and the rest in Python, on purpose. The currency and the consumed-entry
    exclusion are cheap and selective, and they are facts rather than judgements. Whether an entry
    is *close enough* is the matching policy's decision, and it belongs in one place — the
    assembler — rather than half-expressed as a SQL predicate that could drift from it.

    ``ORDER BY id`` is present even though the assembler sorts: an unordered read is a
    nondeterministic read, and relying on the caller to fix it later is how a nondeterministic
    dependency survives a refactor.
    """
    consumed = sa.select(MatchResult.ledger_entry_id)
    rows = (
        await session.execute(
            sa.select(LedgerEntry)
            .where(
                LedgerEntry.currency == subject.currency,
                LedgerEntry.id.not_in(consumed),
            )
            .order_by(LedgerEntry.id)
        )
    ).scalars()

    return [
        CandidateEntryFact(
            entry_id=entry.id,
            external_ref=entry.external_ref,
            account_code=entry.account_code,
            amount=entry.amount,
            currency=entry.currency,
            booked_on=entry.booked_at.date(),
            description=entry.description,
        )
        for entry in rows
    ]


async def _persist_evidence(
    session: AsyncSession, subject: ExceptionSubject, evidence: EvidencePack
) -> None:
    """Write the pack, idempotently.

    ``ON CONFLICT DO NOTHING`` on the primary key, which works only because the ids are derived
    rather than random: re-assembling an unchanged exception produces the same ids and therefore no
    new rows. A second run is a no-op instead of a duplicate pack, and two concurrent runs cannot
    race each other into one.

    **A written row is never updated, and that has a consequence worth stating.** If a source fact
    changes — a merchant reference corrected, a ledger entry consumed by a later match — a fresh
    assembly sends the new facts to the model while the stored row keeps the old ones. Two
    reviewers found this, and it is the right trade rather than a defect to paper over: evidence is
    an audit record of what was shown, and a row that silently rewrote itself would be worse than
    one that is stale.

    What it does mean is that **an old proposal's ``prompt_hash`` is not re-derivable from the
    database.** The hash proves what was sent at the time; verifying it later needs the pack that
    was sent, and per-proposal pack membership is not recorded — only the subset the model chose to
    cite. That is a real limitation of this increment, recorded as OPEN-13 rather than dressed up:
    the fix is a column, and a column is a migration this increment does not own.
    """
    if not evidence.items:
        return

    await session.execute(
        pg_insert(Evidence)
        .values(
            [
                {
                    "id": item.evidence_id,
                    "exception_id": subject.exception_id,
                    "kind": item.kind.value,
                    # The facts, serialised the one way this system serialises anything the model
                    # will read. `content` is `Text`, so a JSON object is stored as its canonical
                    # rendering — sorted keys, fixed separators — and a reader gets the same
                    # unambiguous structure the provider got.
                    "content": json.dumps(
                        dict(item.facts), sort_keys=True, separators=(",", ":"), ensure_ascii=True
                    ),
                    "source_ref": item.source_ref,
                }
                for item in evidence.items
            ]
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )


async def _persist_proposal(
    session: AsyncSession,
    *,
    subject: ExceptionSubject,
    outcome: ProposalOutcome,
    proposer: TreatmentProposer,
    region_jurisdiction: str,
    proposed_at: dt.datetime,
) -> uuid.UUID:
    """The proposal and its citations, in one transaction.

    Atomic because a proposal without its citations is a decision with no stated evidence, and
    citations without their proposal are unreachable rows. Both statements are in the caller's
    ``session.begin()``, so a failure on either leaves neither.
    """
    proposal = outcome.proposal
    assert proposal is not None, "only a PROPOSED outcome reaches here"

    proposal_id = uuid.uuid4()
    session.add(
        TreatmentProposalRow(
            id=proposal_id,
            exception_id=subject.exception_id,
            treatment=proposal.treatment,
            confidence=proposal.confidence,
            rationale=proposal.rationale,
            abstained=proposal.abstained,
            model_id=proposer.model_id,
            model_version=proposer.model_version,
            prompt_hash=outcome.prompt_hash,
            cassette_id=None,
            region_jurisdiction=region_jurisdiction,
            proposed_at=proposed_at,
        )
    )

    for reference in proposal.evidence_refs:
        session.add(
            TreatmentProposalEvidence(
                treatment_proposal_id=proposal_id,
                evidence_id=uuid.UUID(reference.evidence_id),
            )
        )

    return proposal_id
