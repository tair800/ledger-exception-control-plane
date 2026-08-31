"""Run the completed M2 pipeline once and collect what it did.

**This module decides nothing.** It calls the real boundaries in order — ingestion's
:func:`~..ingest.interpret`, the matcher's :func:`~..matching.match`, the classifier's
:func:`~..classification.classify`, the calculator's :func:`~..money.compute_adjustment` — and
counts their answers. There is no parser here, no matching rule, no taxonomy and no formula; a
guard test walks this package's AST to keep it that way. If the numbers in the report are wrong,
the pipeline is wrong, which is the only property that makes a demo worth showing.

**No database.** Every boundary M2 exposes has a pure entry point, so the snapshot runs from a
generated corpus in memory: no PostgreSQL, no container, nothing to clean up afterwards. Ingestion's
persistence is the one part not exercised, and the report says so rather than implying otherwise.

**Two kinds of number, kept apart.** :class:`PipelineSnapshot` holds what the pipeline *did* —
everything a running system would know about itself. :class:`GroundTruth` holds how that compares
with what each fixture case was *constructed* to be, which is knowledge only a synthetic corpus has.
The renderer keeps them in separate sections for the same reason: conflating them is how a demo
starts quietly grading itself.
"""

from __future__ import annotations

import collections
import dataclasses
import uuid
from typing import Final

from ledger_exception_control_plane.classification import (
    Classification,
    SettlementMovement,
    accounting_period,
    classify,
    movement_type,
)
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.fixtures.generator import GeneratedCorpus, generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.ingest import NormalisedLine, interpret
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    MatchOutcome,
    MatchRule,
    match,
)
from ledger_exception_control_plane.money import (
    DEMO_LEDGER_CONTEXT,
    AdjustmentInstruction,
    ExceptionFacts,
    LedgerContext,
    compute_adjustment,
)

#: The seed the committed corpus uses. Same constant, same reason: one reproducible origin.
DEFAULT_SEED: Final = 20260829

#: How many scenario instances the snapshot builds.
#:
#: 200 gives every catalogue entry enough instances for its counts to mean something — a residual
#: mix, a tolerance band that actually absorbs something, a handful of ambiguities — while keeping
#: the rendered page small enough to commit and read. The canonical profile holds exactly one of
#: each condition, which describes the *catalogue* rather than the pipeline.
DEFAULT_INSTANCES: Final = 200

#: The treatment the calculator is exercised with.
#:
#: An approved treatment is an input to M2.4, not something it decides, and nothing in the system
#: chooses one yet — that is M3 proposing and M5 approving. ``REBOOK`` is used so the snapshot shows
#: the calculator working at all, and the report says plainly that the treatment was supplied by the
#: demo rather than decided by anything.
DEMO_TREATMENT: Final = TreatmentCode.REBOOK

#: Namespace for the deterministic line identifiers this module mints. See :func:`_line_id`.
_ID_NAMESPACE: Final = uuid.UUID("6d656d6f-0000-4000-8000-000000000001")


@dataclasses.dataclass(frozen=True, slots=True)
class IngestionCounts:
    """What the ingestion boundary made of the settlement files."""

    batches: int
    lines_offered: int
    lines_parsed: int
    batches_quarantined: int
    quarantine_reasons: dict[str, int]


@dataclasses.dataclass(frozen=True, slots=True)
class MatchingCounts:
    """What the matcher decided. ``ambiguous`` is a refusal, not a failure."""

    considered: int
    exact: int
    tolerance: int
    ambiguous: int
    unmatched: int
    entries_available: int
    entries_consumed: int


@dataclasses.dataclass(frozen=True, slots=True)
class ClassificationCounts:
    """What the classifier proved about the residuals."""

    residuals: int
    by_class: dict[str, int]
    by_rule: dict[str, int]

    @property
    def classified(self) -> int:
        return self.residuals - self.by_class.get("unclassified", 0)


@dataclasses.dataclass(frozen=True, slots=True)
class CalculatorCounts:
    """What the calculator priced, and why it declined the rest."""

    considered: int
    priced: int
    refused_by_reason: dict[str, int]
    priced_by_account: dict[str, int]


@dataclasses.dataclass(frozen=True, slots=True)
class Example:
    """One representative row, carried through to the report so a reader can see a real case."""

    stage: str
    reference: str
    detail: str
    outcome: str


@dataclasses.dataclass(frozen=True, slots=True)
class GroundTruth:
    """How the pipeline's answers compare with what each case was *constructed* to be.

    **Only a synthetic corpus knows any of this.** It is kept out of :class:`PipelineSnapshot` and
    rendered in its own section because a running system has no equivalent — presenting the two
    together would make a demo look like it was measuring itself.
    """

    false_matches: int
    classifications_correct: int
    classifications_wrong: int
    classifications_under: int
    classifications_no_intent: int
    wrong_financial_instructions: int


@dataclasses.dataclass(frozen=True, slots=True)
class PipelineSnapshot:
    """One run of the deterministic pipeline, end to end."""

    profile: str
    seed: int
    instances: int
    treatment: str
    ledger_context_version: str
    functional_currency: str
    ingestion: IngestionCounts
    matching: MatchingCounts
    classification: ClassificationCounts
    calculator: CalculatorCounts
    examples: tuple[Example, ...]
    ground_truth: GroundTruth


def _line_id(content_hash: str, line_number: int) -> uuid.UUID:
    """A stable identifier for a parsed line.

    Minted here because ingestion mints one at *persistence*.

    ``ingest.service`` assigns a UUIDv4 when it writes the row (ADR-022). This snapshot never writes
    a row, so it derives one from the batch's content hash and the line's position instead — which
    keeps the report reproducible where a v4 would make every run different.

    It changes no outcome. No matching, classification or pricing rule reads an identifier: the
    matcher uses ids only to break ties in its *output order*, which a test proves the decisions do
    not depend on, and the classifier uses them only to avoid comparing a line with itself.
    """
    return uuid.uuid5(_ID_NAMESPACE, f"{content_hash}:{line_number}")


def _ingest(
    generated: GeneratedCorpus,
) -> tuple[IngestionCounts, list[tuple[str, NormalisedLine]]]:
    """Run the real ingestion boundary over every settlement file, valid and invalid.

    ``interpret`` is ingestion's pure half — parse then normalise, the whole of what M2.1 decides
    about a payload. The persistence half needs a database and writes nothing here; the report says
    so rather than letting a reader assume the snapshot proved it.

    The deliberately invalid artifacts committed alongside the corpus are run through the same
    function, because a quarantine path that is never exercised is a claim rather than a behaviour.
    """
    parsed: list[tuple[str, NormalisedLine]] = []
    offered = 0
    for batch in generated.corpus.batches:
        payload = generated.files[batch.raw_payload_path]
        offered += payload.decode("utf-8").count("\n") - 1  # rows, less the header
        lines, defects = interpret(payload)
        assert not defects, "the committed corpus must parse cleanly"
        parsed.extend((batch.content_hash, line) for line in lines)

    quarantined = 0
    reasons: collections.Counter[str] = collections.Counter()
    for name, payload in generated.files.items():
        if not (name.startswith("invalid/") and name.endswith(".csv")):
            continue
        _lines, defects = interpret(payload)
        if defects:
            quarantined += 1
            reasons[defects[0].code.value] += 1

    return (
        IngestionCounts(
            batches=len(generated.corpus.batches),
            lines_offered=offered,
            lines_parsed=len(parsed),
            batches_quarantined=quarantined,
            quarantine_reasons=dict(sorted(reasons.items())),
        ),
        parsed,
    )


def build(
    seed: int = DEFAULT_SEED,
    profile: Profile = Profile.BULK,
    instances: int = DEFAULT_INSTANCES,
    ledger_ctx: LedgerContext = DEMO_LEDGER_CONTEXT,
) -> PipelineSnapshot:
    """Run the pipeline once and return everything the report needs.

    The stages are chained: what ingestion parsed is what the matcher sees, what the matcher leaves
    is what the classifier reads, and what the classifier decided is what the calculator prices.
    Nothing is recomputed from the corpus halfway through.
    """
    generated = generate(seed, profile, instances)
    ingestion, parsed = _ingest(generated)

    scenario_of: dict[uuid.UUID, str] = {}
    by_reference = {
        (batch.content_hash, row.line_number): row.scenario_id
        for batch in generated.corpus.batches
        for row in batch.lines
    }

    candidates: list[CandidateLine] = []
    normalised: dict[uuid.UUID, NormalisedLine] = {}
    for content_hash, line in parsed:
        identifier = _line_id(content_hash, line.line_number)
        candidates.append(
            CandidateLine(
                id=identifier,
                line_number=line.line_number,
                amount=line.amount,
                currency=line.currency,
                value_date=line.value_date,
            )
        )
        normalised[identifier] = line
        scenario_of[identifier] = by_reference[(content_hash, line.line_number)]

    entries = [
        CandidateEntry(
            id=entry.id,
            external_ref=entry.external_ref,
            amount=entry.amount,
            currency=entry.currency,
            booked_on=entry.booked_at.date(),
        )
        for entry in generated.corpus.ledger_entries
    ]
    scenario_of_entry = {entry.id: entry.scenario_id for entry in generated.corpus.ledger_entries}

    outcome = match(candidates, entries, DEFAULT_POLICY)
    matched_lines = {pair.line_id for pair in outcome.matches}
    matching = MatchingCounts(
        considered=len(candidates),
        exact=sum(1 for p in outcome.matches if p.rule is MatchRule.EXACT_AMOUNT),
        tolerance=sum(1 for p in outcome.matches if p.rule is MatchRule.AMOUNT_WITHIN_TOLERANCE),
        ambiguous=len(outcome.ambiguous_line_ids),
        unmatched=len(outcome.unmatched_line_ids),
        entries_available=len(entries),
        entries_consumed=len(outcome.matches),
    )

    movements = [
        SettlementMovement(
            id=candidate.id,
            merchant_reference=normalised[candidate.id].merchant_reference,
            movement=movement_type(normalised[candidate.id].transaction_type),
            amount=candidate.amount,
            currency=candidate.currency,
            value_date=candidate.value_date,
            matched=candidate.id in matched_lines,
        )
        for candidate in candidates
    ]
    residual_movements = [m for m in movements if not m.matched]
    decisions = classify(residual_movements, movements)

    classification = ClassificationCounts(
        residuals=len(residual_movements),
        by_class=dict(
            sorted(collections.Counter(d.classification.value for d in decisions).items())
        ),
        by_rule=dict(sorted(collections.Counter(d.rule_id.value for d in decisions).items())),
    )

    by_movement = {m.id: m for m in movements}
    priced: list[tuple[uuid.UUID, AdjustmentInstruction]] = []
    refusals: collections.Counter[str] = collections.Counter()
    for decision in decisions:
        movement = by_movement[decision.line_id]
        facts = ExceptionFacts(
            exception_id=decision.line_id,
            classification=decision.classification,
            amount=movement.amount,
            currency=movement.currency,
            value_date=movement.value_date,
            originating_period=_originating_period(movement, movements),
        )
        result = compute_adjustment(facts, DEMO_TREATMENT, ledger_ctx)
        if isinstance(result, AdjustmentInstruction):
            priced.append((decision.line_id, result))
        else:
            refusals[result.value] += 1

    calculator = CalculatorCounts(
        considered=len(decisions),
        priced=len(priced),
        refused_by_reason=dict(sorted(refusals.items())),
        priced_by_account=dict(
            sorted(collections.Counter(i.account_code for _, i in priced).items())
        ),
    )

    return PipelineSnapshot(
        profile=profile.value,
        seed=seed,
        instances=instances,
        treatment=DEMO_TREATMENT.value,
        ledger_context_version=ledger_ctx.version,
        functional_currency=ledger_ctx.functional_currency,
        ingestion=ingestion,
        matching=matching,
        classification=classification,
        calculator=calculator,
        examples=_examples(outcome, decisions, movements, normalised, priced, refusals, ledger_ctx),
        ground_truth=_grade(
            generated, scenario_of, scenario_of_entry, outcome, decisions, priced, by_movement
        ),
    )


def _originating_period(
    subject: SettlementMovement, movements: list[SettlementMovement]
) -> str | None:
    """The period of the reconciled movement this one exactly reverses, if there is exactly one.

    Assembled from production fields only — the merchant's reference, the currency, the match state
    and an exact negation. It is an *input* the calculator requires and does not look up, because
    M2.4 performs no I/O; a later increment derives it in the orchestration that assembles a
    calculation. Nothing here decides a classification or an amount.
    """
    if subject.merchant_reference is None:
        return None
    offsets = [
        other
        for other in movements
        if other.id != subject.id
        and other.matched
        and other.merchant_reference == subject.merchant_reference
        and other.currency == subject.currency
        and other.amount == -subject.amount
    ]
    if len(offsets) != 1:
        return None
    return accounting_period(offsets[0].value_date)


def _examples(
    outcome: MatchOutcome,
    decisions: tuple[Classification, ...],
    movements: list[SettlementMovement],
    normalised: dict[uuid.UUID, NormalisedLine],
    priced: list[tuple[uuid.UUID, AdjustmentInstruction]],
    refusals: collections.Counter[str],
    ledger_ctx: LedgerContext,
) -> tuple[Example, ...]:
    """A handful of real rows, one per interesting outcome.

    Picked by *outcome* rather than by scenario, and the first of each in a stable order, so the
    examples come from the pipeline's own answers rather than from a list of cases someone wanted to
    show. References are the PSP's own, which are synthetic.
    """
    chosen: list[Example] = []
    pairs = outcome.matches
    by_id = {m.id: m for m in movements}

    for rule, label in (
        (MatchRule.EXACT_AMOUNT, "Matched exactly"),
        (MatchRule.AMOUNT_WITHIN_TOLERANCE, "Matched within tolerance"),
    ):
        pair = next((p for p in pairs if p.rule is rule), None)
        if pair is not None:
            line = normalised[pair.line_id]
            absorbed = (
                "no difference"
                if pair.tolerance_applied is None
                else (f"absorbed {pair.tolerance_applied} {pair.tolerance_currency}")
            )
            chosen.append(
                Example(
                    "Matching",
                    line.psp_reference,
                    f"{line.amount} {line.currency}",
                    f"{label} — {absorbed}",
                )
            )

    seen_classes: set[str] = set()
    for decision in decisions:
        klass = decision.classification.value
        if klass in seen_classes:
            continue
        seen_classes.add(klass)
        line = normalised[decision.line_id]
        chosen.append(
            Example(
                "Classification",
                line.psp_reference,
                f"{line.amount} {line.currency} · declared {line.transaction_type}",
                f"{klass} — rule {decision.rule_id.value}",
            )
        )

    if priced:
        line = normalised[priced[0][0]]
        instruction = priced[0][1]
        chosen.append(
            Example(
                "Calculator",
                line.psp_reference,
                f"{instruction.amount} {instruction.currency}",
                f"priced to account {instruction.account_code}, period {instruction.period}",
            )
        )

    for reason in sorted(refusals):
        blocked = next(
            (
                normalised[d.line_id]
                for d in decisions
                if _refusal_reason(by_id[d.line_id], d, ledger_ctx) == reason
            ),
            None,
        )
        if blocked is not None:
            chosen.append(
                Example(
                    "Calculator",
                    blocked.psp_reference,
                    f"{blocked.amount} {blocked.currency}",
                    f"refused — {reason}",
                )
            )
    return tuple(chosen)


def _refusal_reason(
    movement: SettlementMovement, decision: Classification, ledger_ctx: LedgerContext
) -> str | None:
    """Ask the calculator again for one line, to label an example with the reason it gave.

    Calling the real function rather than re-deriving the reason: this module must not contain a
    second copy of the rule that decides one.
    """
    facts = ExceptionFacts(
        exception_id=movement.id,
        classification=decision.classification,
        amount=movement.amount,
        currency=movement.currency,
        value_date=movement.value_date,
        originating_period=None,
    )
    result = compute_adjustment(facts, DEMO_TREATMENT, ledger_ctx)
    return None if isinstance(result, AdjustmentInstruction) else result.value


def _grade(
    generated: GeneratedCorpus,
    scenario_of: dict[uuid.UUID, str],
    scenario_of_entry: dict[uuid.UUID, str],
    outcome: MatchOutcome,
    decisions: tuple[Classification, ...],
    priced: list[tuple[uuid.UUID, AdjustmentInstruction]],
    by_movement: dict[uuid.UUID, SettlementMovement],
) -> GroundTruth:
    """Compare the pipeline's answers with what each case was constructed to be.

    Everything here reads ``scenario_id`` and ``intended_classification`` — construction metadata
    that only a synthetic corpus has. It grades output and never feeds it back in; the production
    types the pipeline was handed carry no field for any of it.
    """
    false_matches = sum(
        1
        for pair in outcome.matches
        if scenario_of[pair.line_id] != scenario_of_entry[pair.entry_id]
    )

    intent = {
        scenario.scenario_id: scenario.intended_classification
        for scenario in generated.scenarios.scenarios
    }
    correct = wrong = under = no_intent = 0
    for decision in decisions:
        intended = intent[scenario_of[decision.line_id]]
        assigned = decision.classification
        if intended is None:
            no_intent += 1
        elif assigned is intended:
            correct += 1
        elif assigned is ExceptionClassification.UNCLASSIFIED:
            under += 1
        else:
            wrong += 1

    expected_account = {
        ExceptionClassification.CHARGEBACK_REVERSAL: "4900",
        ExceptionClassification.CROSS_PERIOD_REFUND: "4100",
    }
    assigned_class = {d.line_id: d.classification for d in decisions}
    wrong_instructions = 0
    for line_id, instruction in priced:
        movement = by_movement[line_id]
        if (
            instruction.amount != movement.amount
            or instruction.currency != movement.currency
            or instruction.account_code != expected_account.get(assigned_class[line_id])
        ):
            wrong_instructions += 1

    return GroundTruth(
        false_matches=false_matches,
        classifications_correct=correct,
        classifications_wrong=wrong,
        classifications_under=under,
        classifications_no_intent=no_intent,
        wrong_financial_instructions=wrong_instructions,
    )
