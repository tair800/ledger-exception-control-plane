"""M3.3 persistence, against real PostgreSQL.

What is being checked is not "does the INSERT work" but the three properties that make a stored
proposal worth trusting:

* **Nothing is written unless the answer is usable.** An unavailable provider and an invalid answer
  both leave no proposal — and the exception untouched, still waiting for a human. That is the
  plan's exit criterion, and it is checked against the database rather than against a return value.
* **The proposal and its citations are one write.** A decision with no stated evidence is not
  provenance, and the association table's foreign keys are what make a fabricated citation
  impossible rather than merely unlikely.
* **Assembly is idempotent.** Evidence ids are derived, so a second run over unchanged facts adds
  no rows. Two concurrent runs cannot race each other into a duplicate pack.

Run against ``lecp_test``, bootstrapped by ``make test-db-init``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Final

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.control import (
    Evidence,
    ExceptionRecord,
    TreatmentProposalEvidence,
)
from ledger_exception_control_plane.db.control import (
    TreatmentProposal as TreatmentProposalRow,
)
from ledger_exception_control_plane.db.engine import create_engine
from ledger_exception_control_plane.db.models import (
    LedgerEntry,
    MatchState,
    SettlementBatch,
    SettlementLine,
)
from ledger_exception_control_plane.llm.flow import ProposalStatus
from ledger_exception_control_plane.llm.port import ProviderRequest
from ledger_exception_control_plane.llm.providers.anthropic_messages import (
    AnthropicMessagesProposer,
)
from ledger_exception_control_plane.llm.service import (
    ExceptionNotFoundError,
    ExceptionNotOpenError,
    ProposalRecord,
    propose_for_exception,
)

pytestmark = pytest.mark.integration

DSN: Final = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

JURISDICTION: Final = "eu-west"
PROPOSED_AT: Final = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


@dataclasses.dataclass(frozen=True, slots=True)
class _Scenario:
    """The rows one test runs against. Typed, so assertions check against production types."""

    exception_a: uuid.UUID
    exception_b: uuid.UUID
    near_ref: str
    far_ref: str


class _FakeTransport:
    def __init__(
        self, payload: Mapping[str, object] | None = None, *, raises: Exception | None = None
    ) -> None:
        self._payload = payload or {}
        self._raises = raises

    async def send(self, request: ProviderRequest) -> Mapping[str, object]:
        if self._raises is not None:
            raise self._raises
        return self._payload


def _answer(**overrides: object) -> dict[str, object]:
    return {
        "treatment": "rebook",
        "confidence": "high",
        "rationale": "The candidate entry offsets the residual line.",
        "evidence_refs": [],
        "abstained": False,
        **overrides,
    }


def _envelope(answer: object) -> dict[str, object]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _proposer(
    payload: Mapping[str, object] | None = None, **kwargs: object
) -> AnthropicMessagesProposer:
    return AnthropicMessagesProposer(_FakeTransport(payload, **kwargs))  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings(postgres_dsn=SecretStr(DSN)))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def scenario(db: AsyncEngine) -> AsyncIterator[_Scenario]:
    """One exception with one plausible candidate, plus a second exception that must stay separate.

    Built directly rather than through the fixture corpus on purpose: this module is about what the
    database does, and a hand-built scenario states the relationships being tested instead of
    relying on a corpus reader to have produced them.
    """
    ids = {
        "batch": uuid.uuid4(),
        "line_a": uuid.uuid4(),
        "line_b": uuid.uuid4(),
        "entry_near": uuid.uuid4(),
        "entry_far": uuid.uuid4(),
        "exception_a": uuid.uuid4(),
        "exception_b": uuid.uuid4(),
    }
    digest = hashlib.sha256(f"m33-{ids['batch']}".encode()).hexdigest()
    # `ledger_entry.external_ref` is unique, so the references have to be scoped to this run —
    # otherwise two tests in the same session collide, and a run that dies before teardown poisons
    # every later one.
    tag = ids["batch"].hex[:8]
    near_ref, far_ref = f"gl-{tag}-near", f"gl-{tag}-far"

    async with AsyncSession(db) as session, session.begin():
        session.add(
            SettlementBatch(
                id=ids["batch"],
                content_hash=digest,
                source="m33",
                raw_payload=b"x",
                received_at=PROPOSED_AT,
                status="parsed",
            )
        )
        for index, key in enumerate(("line_a", "line_b"), start=1):
            session.add(
                SettlementLine(
                    id=ids[key],
                    settlement_batch_id=ids["batch"],
                    line_number=index,
                    psp_reference=f"PSP-{index}",
                    merchant_reference="ORD-4417",
                    transaction_type="refund",
                    amount=decimal.Decimal("326.92"),
                    currency="EUR",
                    value_date=dt.date(2026, 6, 15),
                    match_state=MatchState.UNMATCHED,
                )
            )
        session.add(
            LedgerEntry(
                id=ids["entry_near"],
                external_ref=near_ref,
                account_code="4100",
                amount=decimal.Decimal("326.92"),
                currency="EUR",
                booked_at=dt.datetime(2026, 6, 15, 9, 0, tzinfo=dt.UTC),
                description="capture",
            )
        )
        session.add(
            LedgerEntry(
                id=ids["entry_far"],
                external_ref=far_ref,
                account_code="4100",
                amount=decimal.Decimal("9999.00"),
                currency="EUR",
                booked_at=dt.datetime(2026, 6, 15, 9, 0, tzinfo=dt.UTC),
                description="unrelated",
            )
        )
        for key, line in (("exception_a", "line_a"), ("exception_b", "line_b")):
            session.add(
                ExceptionRecord(
                    id=ids[key],
                    settlement_line_id=ids[line],
                    line_match_state=MatchState.UNMATCHED,
                    classification="cross_period_refund",
                    rule_id="cross_period_refund_v1",
                    classifier_version="1",
                    correlation_id=f"m33-{ids['batch'].hex[:8]}-{line}",
                )
            )

    yield _Scenario(
        exception_a=ids["exception_a"],
        exception_b=ids["exception_b"],
        near_ref=near_ref,
        far_ref=far_ref,
    )

    # Teardown in dependency order. `exception` RESTRICTs its settlement line — deliberately, so
    # deleting a line cannot erase the evidence that it needed a decision (ADR-028) — so the batch
    # cascade cannot be relied on to clear the way.
    exceptions = [ids["exception_a"], ids["exception_b"]]
    async with AsyncSession(db) as session, session.begin():
        proposals = sa.select(TreatmentProposalRow.id).where(
            TreatmentProposalRow.exception_id.in_(exceptions)
        )
        await session.execute(
            sa.delete(TreatmentProposalEvidence).where(
                TreatmentProposalEvidence.treatment_proposal_id.in_(proposals)
            )
        )
        await session.execute(
            sa.delete(TreatmentProposalRow).where(TreatmentProposalRow.exception_id.in_(exceptions))
        )
        await session.execute(sa.delete(Evidence).where(Evidence.exception_id.in_(exceptions)))
        await session.execute(sa.delete(ExceptionRecord).where(ExceptionRecord.id.in_(exceptions)))
        await session.execute(
            sa.delete(SettlementBatch).where(SettlementBatch.content_hash == digest)
        )
        await session.execute(
            sa.delete(LedgerEntry).where(LedgerEntry.id.in_([ids["entry_near"], ids["entry_far"]]))
        )


async def _proposals(db: AsyncEngine, exception_id: uuid.UUID) -> list[TreatmentProposalRow]:
    async with AsyncSession(db) as session:
        rows = await session.execute(
            sa.select(TreatmentProposalRow).where(TreatmentProposalRow.exception_id == exception_id)
        )
        return list(rows.scalars())


async def _evidence(db: AsyncEngine, exception_id: uuid.UUID) -> list[Evidence]:
    async with AsyncSession(db) as session:
        rows = await session.execute(
            sa.select(Evidence).where(Evidence.exception_id == exception_id).order_by(Evidence.id)
        )
        return list(rows.scalars())


@pytest.mark.asyncio
async def test_a_valid_answer_is_recorded_with_its_provenance(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """The proposal, its citations, and the three provenance fields the plan names."""
    first = await _run_assembly(db, scenario, cite=True)

    assert first.proposal_id is not None
    stored = await _proposals(db, scenario.exception_a)
    assert len(stored) == 1

    row = stored[0]
    assert row.treatment == "rebook"
    assert row.rationale.startswith("The candidate entry")
    assert row.model_id
    assert row.model_version
    assert row.prompt_hash == first.outcome.prompt_hash
    assert row.cassette_id is None
    assert row.region_jurisdiction == JURISDICTION

    async with AsyncSession(db) as session:
        citations = list(
            (
                await session.execute(
                    sa.select(TreatmentProposalEvidence.evidence_id).where(
                        TreatmentProposalEvidence.treatment_proposal_id == row.id
                    )
                )
            ).scalars()
        )
    assert citations == [first.evidence_ids[0]]


async def _run_assembly(
    db: AsyncEngine,
    scenario: _Scenario,
    *,
    cite: bool = False,
    answer: object | None = None,
    raises: Exception | None = None,
) -> ProposalRecord:
    """Assemble once to learn the ids, then run for real with an answer that can cite them."""
    if raises is not None:
        return await propose_for_exception(
            db,
            _proposer(raises=raises),
            scenario.exception_a,
            region_jurisdiction=JURISDICTION,
            proposed_at=PROPOSED_AT,
        )

    probe = await propose_for_exception(
        db,
        _proposer(_envelope("not json")),
        scenario.exception_a,
        region_jurisdiction=JURISDICTION,
        proposed_at=PROPOSED_AT,
    )
    refs = [{"evidence_id": str(probe.evidence_ids[0])}] if cite else []
    payload = answer if answer is not None else _answer(evidence_refs=refs)

    return await propose_for_exception(
        db,
        _proposer(_envelope(payload)),
        scenario.exception_a,
        region_jurisdiction=JURISDICTION,
        proposed_at=PROPOSED_AT,
    )


@pytest.mark.asyncio
async def test_both_candidates_become_evidence_and_each_states_why_it_did_not_match(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """Under the inverted rule, the far entry is evidence too — labelled as the near miss it is.

    The name and the body both changed with the rule. This used to assert that only the entry
    inside the tolerance band became evidence, which was the behaviour that made candidate evidence
    effectively unreachable for a real residual: an entry inside the band that was also unique is
    one the matcher took, so the line would not be an exception at all.

    What matters now is that each candidate carries the matcher's verdict, and that the pack orders
    them nearest first.
    """
    record = await _run_assembly(db, scenario, raises=TimeoutError("down"))

    rows = await _evidence(db, scenario.exception_a)
    by_id = {
        row.id: json.loads(row.content) for row in rows if row.kind == "candidate_ledger_entry"
    }
    by_ref = {facts["external_ref"]: facts for facts in by_id.values()}

    assert by_ref[scenario.near_ref]["not_matched_because"] == "inside_tolerance_unmatched"
    assert by_ref[scenario.far_ref]["not_matched_because"] == "outside_amount_band"
    # Rendered at the money column's own scale, which is fixed (ADR-020) and therefore
    # deterministic. The unit suite builds Decimals with two places and sees "0.00"; a value
    # read back from PostgreSQL carries four.
    assert by_ref[scenario.near_ref]["amount_delta"] == "0.0000"

    # Ordering is a property of the *pack*, which `evidence_ids` preserves. Reading it off the
    # database rows was the first version of this assertion and it tested the wrong thing —
    # `_evidence` sorts by id, which is deliberate for a stable read and has nothing to do with
    # proximity. The gate caught it.
    ordered = [eid for eid in record.evidence_ids if eid in by_id]
    assert [by_id[eid]["external_ref"] for eid in ordered] == [
        scenario.near_ref,
        scenario.far_ref,
    ], "the pack must offer the nearest entry first"


@pytest.mark.asyncio
async def test_an_unavailable_provider_records_no_proposal_and_leaves_the_exception_alone(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """**The exit criterion, against the database.**

    The exception is read before and after and compared field by field: no status moved, nothing
    was claimed, nothing was written except the evidence — which is a fact about the case rather
    than about the model, and is true whether or not anybody asked a question.
    """
    async with AsyncSession(db) as session:
        before = (
            await session.execute(
                sa.select(ExceptionRecord).where(ExceptionRecord.id == scenario.exception_a)
            )
        ).scalar_one()
        snapshot = {
            column.name: getattr(before, column.name)
            for column in ExceptionRecord.__table__.columns
        }

    record = await _run_assembly(db, scenario, raises=TimeoutError("read timeout"))

    assert record.outcome.status is ProposalStatus.UNAVAILABLE
    assert record.proposal_id is None
    assert await _proposals(db, scenario.exception_a) == []

    async with AsyncSession(db) as session:
        after = (
            await session.execute(
                sa.select(ExceptionRecord).where(ExceptionRecord.id == scenario.exception_a)
            )
        ).scalar_one()
        assert {
            column.name: getattr(after, column.name) for column in ExceptionRecord.__table__.columns
        } == snapshot


@pytest.mark.asyncio
async def test_an_invalid_answer_records_no_proposal(db: AsyncEngine, scenario: _Scenario) -> None:
    record = await _run_assembly(db, scenario, answer=_answer(treatment="auto_post"))

    assert record.outcome.status is ProposalStatus.INVALID
    assert record.proposal_id is None
    assert await _proposals(db, scenario.exception_a) == []


@pytest.mark.asyncio
async def test_a_proposal_citing_unsupplied_evidence_records_nothing(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """Not a partial write, and not a trimmed citation list. Nothing at all."""
    record = await _run_assembly(
        db, scenario, answer=_answer(evidence_refs=[{"evidence_id": str(uuid.uuid4())}])
    )

    assert record.outcome.status is ProposalStatus.INVALID
    assert await _proposals(db, scenario.exception_a) == []

    # Scoped to this exception and asserted as zero. The first version asked whether
    # `count(*) is not None`, which SQL never answers falsely — a reviewer demonstrated it passing
    # against a table with 492 rows. It would have accepted any number of orphaned citations.
    async with AsyncSession(db) as session:
        orphans = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(TreatmentProposalEvidence)
                .join(
                    TreatmentProposalRow,
                    TreatmentProposalRow.id == TreatmentProposalEvidence.treatment_proposal_id,
                )
                .where(TreatmentProposalRow.exception_id == scenario.exception_a)
            )
        ).scalar()
    assert orphans == 0


@pytest.mark.asyncio
async def test_citing_evidence_that_belongs_to_another_exception_records_nothing(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """The cross-exception attack, end to end through the database."""
    await propose_for_exception(
        db,
        _proposer(_envelope("not json")),
        scenario.exception_b,
        region_jurisdiction=JURISDICTION,
        proposed_at=PROPOSED_AT,
    )
    theirs = await _evidence(db, scenario.exception_b)
    assert theirs, "exception B should have its own pack"

    record = await _run_assembly(
        db, scenario, answer=_answer(evidence_refs=[{"evidence_id": str(theirs[0].id)}])
    )

    assert record.outcome.status is ProposalStatus.INVALID
    assert "not supplied" in (record.outcome.detail or "")
    assert await _proposals(db, scenario.exception_a) == []


@pytest.mark.asyncio
async def test_assembly_is_idempotent(db: AsyncEngine, scenario: _Scenario) -> None:
    """A second run adds no evidence rows, because the ids are derived rather than random."""
    await _run_assembly(db, scenario, raises=TimeoutError("down"))
    first = await _evidence(db, scenario.exception_a)

    await _run_assembly(db, scenario, raises=TimeoutError("down"))
    second = await _evidence(db, scenario.exception_a)

    assert [row.id for row in first] == [row.id for row in second]
    assert len(second) == len(first)


@pytest.mark.asyncio
async def test_concurrent_assembly_does_not_duplicate_evidence(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """Four runs at once produce one pack: a derived id plus ON CONFLICT DO NOTHING."""
    await asyncio.gather(
        *(
            propose_for_exception(
                db,
                _proposer(raises=TimeoutError("down")),
                scenario.exception_a,
                region_jurisdiction=JURISDICTION,
                proposed_at=PROPOSED_AT,
            )
            for _ in range(4)
        )
    )

    rows = await _evidence(db, scenario.exception_a)
    assert len(rows) == len({row.id for row in rows}), "a concurrent run duplicated the pack"

    # Asserted on *this* scenario's rows, not on an absolute table count. The first version said
    # `len(rows) == 2`, and a reviewer broke it with one leftover EUR ledger entry from a sibling
    # integration module — `_load_candidates` reads every unconsumed entry in the currency, so the
    # pack size legitimately depends on what else is in the database.
    kinds = {row.kind for row in rows}
    assert "remittance_reference" in kinds
    assert all(k in {"remittance_reference", "candidate_ledger_entry"} for k in kinds)
    assert sum(1 for row in rows if row.kind == "remittance_reference") == 1
    assert any(scenario.near_ref in row.content for row in rows), "the near entry is evidence"


@pytest.mark.asyncio
async def test_an_unknown_exception_is_refused(db: AsyncEngine) -> None:
    with pytest.raises(ExceptionNotFoundError):
        await propose_for_exception(
            db,
            _proposer(_envelope(_answer())),
            uuid.uuid4(),
            region_jurisdiction=JURISDICTION,
            proposed_at=PROPOSED_AT,
        )


@pytest.mark.asyncio
async def test_the_stored_prompt_hash_reproduces_while_the_sources_are_unchanged(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """Provenance that can be checked — with the limit on that stated, not implied.

    Re-assembling from the live source tables and rebuilding the prompt reproduces the stored hash
    **while nothing underneath has moved.** Two reviewers showed that it stops holding the moment
    the ledger changes: a later match consumes a candidate, or a merchant reference is corrected,
    and the pack differs from the one the model saw. Evidence rows are never rewritten, so the row
    keeps the old text while a fresh assembly sends the new.

    That is a real limitation of this increment rather than a defect to paper over — per-proposal
    pack membership is not recorded, only the subset the model cited — and it is OPEN-13. The test
    is named for what it actually verifies, which the first version was not.
    """
    from ledger_exception_control_plane.llm.evidence import assemble_evidence
    from ledger_exception_control_plane.llm.prompt import build_prompt, prompt_hash
    from ledger_exception_control_plane.llm.service import _load_candidates, _load_subject
    from ledger_exception_control_plane.matching.policy import DEFAULT_POLICY

    record = await _run_assembly(db, scenario, cite=True)
    stored = (await _proposals(db, scenario.exception_a))[0]

    async with AsyncSession(db) as session:
        subject = await _load_subject(session, scenario.exception_a)
        candidates = await _load_candidates(session, subject)

    rebuilt = assemble_evidence(subject, candidates, DEFAULT_POLICY)
    assert prompt_hash(build_prompt(subject, rebuilt)) == stored.prompt_hash
    assert stored.prompt_hash == record.outcome.prompt_hash


@pytest.mark.asyncio
async def test_a_resolved_exception_takes_no_new_proposal(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """A model recommendation on a closed case is advice after the fact.

    A reviewer proposed twice on a ``resolved`` exception and attached two contradictory treatments
    to it, ordered only by a caller-supplied timestamp. ``ExceptionNotFoundError``'s docstring
    already claimed a state check; there was not one.
    """
    async with AsyncSession(db) as session, session.begin():
        await session.execute(
            sa.update(ExceptionRecord)
            .where(ExceptionRecord.id == scenario.exception_a)
            .values(status="resolved")
        )

    with pytest.raises(ExceptionNotOpenError, match="resolved"):
        await propose_for_exception(
            db,
            _proposer(_envelope(_answer())),
            scenario.exception_a,
            region_jurisdiction=JURISDICTION,
            proposed_at=PROPOSED_AT,
        )

    assert await _proposals(db, scenario.exception_a) == []


@pytest.mark.asyncio
async def test_the_audit_arguments_are_validated(db: AsyncEngine, scenario: _Scenario) -> None:
    """§11 makes both part of the audit contract, so neither may be a shrug.

    A reviewer got the empty string, three spaces and ``not-a-region`` into
    ``region_jurisdiction``, and a naive ``proposed_at`` silently shifted by the machine's UTC
    offset — ``12:00`` stored as ``08:00Z`` on a UTC+4 host, in the field whose docstring says it
    exists so the output does not depend on when it ran.
    """
    for jurisdiction in ("", " ", "  "):
        with pytest.raises(ValueError, match="not a processing region"):
            await propose_for_exception(
                db,
                _proposer(_envelope(_answer())),
                scenario.exception_a,
                region_jurisdiction=jurisdiction,
                proposed_at=PROPOSED_AT,
            )

    with pytest.raises(ValueError, match="timezone-aware"):
        await propose_for_exception(
            db,
            _proposer(_envelope(_answer())),
            scenario.exception_a,
            region_jurisdiction=JURISDICTION,
            proposed_at=dt.datetime(2026, 9, 1, 12, 0),
        )

    assert await _proposals(db, scenario.exception_a) == []


@pytest.mark.asyncio
async def test_the_persisted_evidence_content_is_canonical_json(
    db: AsyncEngine, scenario: _Scenario
) -> None:
    """The stored row carries the same unambiguous structure the provider was given.

    Not a ``key=value; key=value`` rendering — that format let untrusted text forge fields, and a
    human reading the audit record was shown the same ambiguity the model was.
    """
    await _run_assembly(db, scenario, raises=TimeoutError("down"))

    for row in await _evidence(db, scenario.exception_a):
        facts = json.loads(row.content)
        assert isinstance(facts, dict)
        assert all(isinstance(k, str) for k in facts)
        assert row.content == json.dumps(
            facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
