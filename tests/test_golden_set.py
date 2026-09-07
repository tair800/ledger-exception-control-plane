"""M6.1 — the golden set: its schema, its labels, and what it must not contain.

The plan asks 6.1 for *"scorer unit tests; golden set schema validation"*. The schema validation is
here; the scorer's is in ``test_scorer.py``.

**The tests that matter most are the negative ones.** A golden set is an answer key, and the ways an
answer key goes wrong are quiet: a label read off the same field the model is graded on, a class
labelled that nothing produces, a set so imbalanced that a constant answer scores well without
anyone noticing. Each has a test below.

No database, no model, no network: every stage the generator calls is pure, which is what lets this
run on every build.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Final

import pytest

from ledger_exception_control_plane.classification.taxonomy import (
    RULE_CLASSIFICATION,
    ClassificationRule,
)
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.ingest import interpret
from ledger_exception_control_plane.money import DEMO_ACCOUNT_POLICY
from tests.cassette_builder import CORPUS_LINES, corpus_subjects
from tests.evaluation.golden import (
    GOLDEN_INSTANCES,
    GOLDEN_PATH,
    GOLDEN_PROFILE,
    GOLDEN_SCHEMA_VERSION,
    GOLDEN_SEED,
    HOLD_OUT_EVERY,
    GoldenRecord,
    build_golden_set,
    load_golden_set,
    render_golden_set,
)
from tests.evaluation.labels import (
    REACHABLE_CLASSIFICATIONS,
    UNREACHABLE_CLASSIFICATIONS,
    LabelRule,
    LabelSource,
    label_for,
)

#: How many exceptions the canonical corpus produces, and therefore how many the committed cassette
#: covers. Stated here rather than derived, so a change in the corpus fails the join test with a
#: message about the corpus instead of silently redefining what "complete" means.
CANONICAL_CORPUS_EXCEPTIONS: Final = 13


# ======================================================================================
# The label declaration
# ======================================================================================


def test_every_reachable_classification_has_a_label_and_no_other_does() -> None:
    """A class the classifier can assign must be labellable; one it cannot must not be.

    Both halves are refusals rather than defaults. An unlabelled reachable class would leave a
    golden record every model scores identically on; a label for an unreachable class would be a
    row that can never appear, which is a claim about behaviour nothing exercises.
    """
    for classification in REACHABLE_CLASSIFICATIONS:
        assert label_for(classification, originating_period=None).treatment in TreatmentCode

    assert set(UNREACHABLE_CLASSIFICATIONS) == {
        ExceptionClassification.PARTIAL_CAPTURE,
        ExceptionClassification.FX_ROUNDING,
    }
    for classification in UNREACHABLE_CLASSIFICATIONS:
        with pytest.raises(ValueError, match="no exception can carry it"):
            label_for(classification, originating_period=None)


def test_the_reachable_set_is_read_from_the_rule_table_rather_than_restated() -> None:
    """Adding a classification rule must extend the reachable set by itself.

    A hand-written list would let a new rule ship a class the golden set silently never labels, and
    the failure would look like a coverage gap rather than a missing decision.
    """
    assert set(REACHABLE_CLASSIFICATIONS) == {
        RULE_CLASSIFICATION[rule] for rule in ClassificationRule
    }


def test_escalate_is_the_label_exactly_where_nothing_is_priceable() -> None:
    """**The claim the whole set's shape rests on, checked against the account policy.**

    Two of the four reachable classes have no configured account under any treatment, deliberately,
    and for those the correct action is to refer the case to a human. Asserted against the policy
    rather than against a list, so configuring an account for ``fee_split`` tomorrow fails this test
    and forces the label to be reconsidered — which is the right outcome, because it would have
    changed.
    """
    for classification in REACHABLE_CLASSIFICATIONS:
        priceable = any(
            DEMO_ACCOUNT_POLICY.account_for(classification, treatment) is not None
            for treatment in TreatmentCode
            if treatment is not TreatmentCode.ESCALATE
        )
        label = label_for(classification, originating_period="2026-05")
        assert (label.treatment is TreatmentCode.ESCALATE) is not priceable, (
            f"{classification.value}: priceable={priceable} but label={label.treatment.value}"
        )
        assert label.escalation_is_correct is not priceable


def test_a_cross_period_refund_accrues_only_when_it_has_somewhere_to_accrue_into() -> None:
    """The one label that depends on a per-exception fact rather than on the class.

    ``ACCRUE`` posts into the period of the movement it reverses and refuses without one, so a
    refund with no established counterpart cannot be accrued — and labelling it ``ACCRUE`` anyway
    would make the "correct" answer one the calculator declines to price.
    """
    with_counterpart = label_for(
        ExceptionClassification.CROSS_PERIOD_REFUND, originating_period="2026-05"
    )
    without = label_for(ExceptionClassification.CROSS_PERIOD_REFUND, originating_period=None)

    assert with_counterpart.treatment is TreatmentCode.ACCRUE
    assert with_counterpart.rule is LabelRule.REFUND_ACCRUES_TO_THE_PERIOD_IT_REVERSES
    assert "2026-05" in with_counterpart.why

    assert without.treatment is TreatmentCode.REBOOK
    assert without.rule is LabelRule.REFUND_WITHOUT_A_COUNTERPART_PERIOD


def test_a_chargeback_reversal_is_recognised_where_it_settled() -> None:
    """A reversal is an event of its own period, so the counterpart period changes nothing here."""
    for period in (None, "2026-01", "2026-05"):
        label = label_for(ExceptionClassification.CHARGEBACK_REVERSAL, originating_period=period)
        assert label.treatment is TreatmentCode.REBOOK
        assert label.rule is LabelRule.REVERSAL_RECOGNISED_WHEN_IT_OCCURS


def test_every_label_clause_carries_a_reason_a_reviewer_can_disagree_with() -> None:
    """A label without a stated reason is an assertion; §20's hold-out slice exists to be argued
    with, and a reviewer cannot argue with a bare treatment code."""
    seen: set[LabelRule] = set()
    for classification in REACHABLE_CLASSIFICATIONS:
        for period in (None, "2026-05"):
            label = label_for(classification, originating_period=period)
            seen.add(label.rule)
            assert len(label.why) > 40, f"{label.rule.value} has no real reason"
            assert label.source is LabelSource.DERIVED

    assert seen == set(LabelRule), f"unexercised label clause(s): {sorted(set(LabelRule) - seen)}"


# ======================================================================================
# Identity — the golden set and the graded subjects must be the same records
# ======================================================================================


def test_the_golden_set_and_the_cassette_covered_subjects_join_completely() -> None:
    """**The test this whole identity migration exists for.**

    The golden set is an answer key. A key that cannot be joined to the answers it grades is not a
    key, and for the whole of the first version of M6 it could not be: the golden generator derived
    its own ``uuid5`` identifiers from a content hash and a line number, while the committed
    cassettes and the real pipeline's ``ExceptionSubject`` are both keyed on the corpus row's own
    ``id``. Thirteen exceptions on each side of the canonical corpus, **zero in common** — and
    every arithmetic check still passed, because 13 and 13 look right and nothing compared the
    identifiers.

    So this asserts the join, not the counts. It fails if the overlap is anything other than total,
    and it says what to do about it.
    """
    subjects = {str(subject.exception_id) for subject, _ in corpus_subjects()}
    golden = {
        record.exception_id
        for record in build_golden_set(profile=Profile.CANONICAL, instances=CORPUS_LINES).records
    }
    overlap = subjects & golden

    assert len(subjects) == CANONICAL_CORPUS_EXCEPTIONS, (
        f"the canonical corpus now produces {len(subjects)} exceptions, not "
        f"{CANONICAL_CORPUS_EXCEPTIONS}; the cassette and this constant both need updating"
    )
    assert overlap == subjects == golden, (
        f"the golden set and the cassette-covered subjects share {len(overlap)} of "
        f"{len(subjects)} identifiers. They must share all of them: both sides key an exception "
        "by the corpus row's own `id`. If this is zero, one side has started deriving its own "
        "identifiers again — fix the derivation rather than the assertion, because a golden set "
        "that joins to nothing grades nothing while still reporting a number."
    )


def test_the_golden_set_agrees_with_what_ingestion_parses_from_the_same_bytes() -> None:
    """The property the generator gave up when it stopped calling ``interpret``, checked directly.

    The generator now takes its settlement facts from the corpus rows rather than from ingestion's
    parse of the raw file, because only the rows carry the identifier everything else uses. That is
    a real reduction in what the generator itself exercises, so the equivalence it relies on is
    asserted here instead of assumed: for every line of the corpus the golden set is generated
    from, the row and what ``interpret`` produces from the very same bytes agree on every field a
    golden record reads.

    If a future ingestion change made the two disagree, this fails — which is the correct place for
    it to fail, because the answer would then depend on which side of the parse you asked.
    """
    generated = generate(GOLDEN_SEED, GOLDEN_PROFILE, GOLDEN_INSTANCES)

    compared = 0
    for batch in generated.corpus.batches:
        parsed, defects = interpret(generated.files[batch.raw_payload_path])
        assert not defects, "the seeded corpus must parse cleanly"
        by_line_number = {line.line_number: line for line in parsed}
        assert len(by_line_number) == len(batch.lines)

        for row in batch.lines:
            line = by_line_number[row.line_number]
            assert (
                row.psp_reference,
                row.merchant_reference,
                row.transaction_type,
                row.amount,
                row.currency,
                row.value_date,
            ) == (
                line.psp_reference,
                line.merchant_reference,
                line.transaction_type,
                line.amount,
                line.currency,
                line.value_date,
            ), f"row {row.id} disagrees with ingestion's parse of the same line"
            compared += 1

    assert compared == generated.corpus.line_count


# ======================================================================================
# The committed artefact
# ======================================================================================


def test_the_committed_golden_set_matches_its_generator() -> None:
    """The drift check, as a test as well as a command.

    The CLI says it in one line for a red build; this fails in the suite that runs on every commit,
    which is where a stale artefact is cheapest to notice.
    """
    assert GOLDEN_PATH.read_text(encoding="utf-8") == render_golden_set(build_golden_set())


def test_the_committed_file_is_byte_identical_across_platforms() -> None:
    """The drift check above compares *text*; this one compares bytes.

    ``read_text`` translates newlines, so a file written on Windows in text mode — 251 CRLFs where
    CI writes 251 LFs — satisfied every existing check while not being the same file. "Same seed,
    same bytes" is the property §20 needs from a committed generator, so the bytes are asserted.
    """
    raw = GOLDEN_PATH.read_bytes()
    assert b"\r" not in raw, (
        "the committed golden set contains carriage returns; the generator must write it with "
        'newline="\\n" so regeneration is byte-identical on every platform'
    )
    assert raw.endswith(b"}\n")


def test_the_committed_set_loads_and_declares_its_own_provenance() -> None:
    """The header carries what is needed to regenerate the file exactly.

    In the file rather than in a sidecar, because provenance that can be separated from the artefact
    it describes will be.
    """
    golden = load_golden_set()
    header = json.loads(GOLDEN_PATH.read_text(encoding="utf-8").splitlines()[0])

    assert golden.schema_version == GOLDEN_SCHEMA_VERSION
    assert header["seed"] == golden.seed
    assert header["profile"] == golden.profile
    assert header["instances"] == golden.instances
    assert header["records"] == len(golden.records) == 250
    assert header["by_classification"] == golden.by_classification


def test_no_record_carries_the_corpus_construction_metadata() -> None:
    """**The firewall, applied to the answer key itself.**

    A golden record describes the exception *as the system classified it*. If it also carried the
    scenario id or the intended classification, a future harness could grade a model against the
    field the corpus was built from rather than against the state the model was shown — and the
    two differ: the corpus intends ``partial_capture`` for two scenarios that the classifier
    assigns something else entirely.
    """
    forbidden = {"scenario_id", "intended_classification", "match_intent", "awkwardness", "intent"}
    fields = set(GoldenRecord.__dataclass_fields__)
    assert not (fields & forbidden), (
        f"a golden record carries construction metadata: {fields & forbidden}"
    )

    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        keys = set(json.loads(line))
        assert not (keys & forbidden), f"the committed file carries {keys & forbidden}"


def test_the_generator_does_not_read_a_construction_label() -> None:
    """The same rule as above, enforced on the generator rather than on its output.

    Parsed, because the generator's docstring discusses the metadata at length and a substring scan
    would flag the explanation rather than a use.
    """
    source = (pathlib.Path(__file__).resolve().parents[0] / "evaluation" / "golden.py").read_text(
        encoding="utf-8"
    )
    identifiers = {
        node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name | ast.Attribute)
    }
    for label in ("scenario_id", "intended_classification", "match_intent", "awkwardness"):
        assert label not in identifiers, f"the golden generator reads fixture metadata: {label}"


def test_every_record_is_labelled_and_internally_consistent() -> None:
    golden = load_golden_set()
    for record in golden.records:
        assert TreatmentCode(record.expected_treatment) in TreatmentCode
        assert ExceptionClassification(record.classification) in REACHABLE_CLASSIFICATIONS
        assert ClassificationRule(record.rule_id) in ClassificationRule
        assert LabelRule(record.label_rule) in LabelRule
        assert LabelSource(record.label_source) in LabelSource
        assert record.escalation_is_correct is (
            record.expected_treatment == TreatmentCode.ESCALATE.value
        ), "a record disagrees with itself about whether escalating is correct"
        assert record.settlement_period[:4].isdigit() and record.settlement_period[4] == "-"
        assert "." in record.amount or record.amount.isdigit(), "the amount must be exact text"


def test_the_hold_out_slice_is_stable_and_spread_across_the_classifications() -> None:
    """§20 wants a hold-out slice. Two properties make it usable, and neither is automatic.

    It must be the *same* slice on every run, or a person confirming labels would be confirming a
    different set each time; and it must not consist of a single classification, or confirming it
    would say nothing about the rest.
    """
    golden = load_golden_set()
    again = build_golden_set()

    held = [record.exception_id for record in golden.hold_out]
    assert held == [record.exception_id for record in again.hold_out], "the slice moved"
    # Indices 0, 10, ... 240 over 250 records: a ceiling division, not a floor plus one. Written
    # out because the first version of this line used the wrong formula and failed on correct code.
    expected = -(-len(golden.records) // HOLD_OUT_EVERY)
    assert len(held) == expected == 25

    classes = {record.classification for record in golden.hold_out}
    assert len(classes) >= 2, f"the hold-out slice is one classification: {classes}"


def test_no_record_claims_a_human_label_because_no_human_has_confirmed_one() -> None:
    """**The honest state of §20's hold-out requirement, asserted rather than described.**

    The slice is selected, the mechanism to carry a human label exists, and nobody has confirmed
    one. Marking these ``human`` without a person would be inventing provenance, which is worse
    than the gap it would hide — so this test pins the gap, and it is the test that should fail on
    the day the owner confirms the slice.
    """
    golden = load_golden_set()
    assert golden.human_labelled == (), (
        "a record claims a human label; if a person really confirmed it, update this test and say "
        "who and when"
    )
    assert all(record.label_source == LabelSource.DERIVED.value for record in golden.records)


def test_the_label_distribution_is_imbalanced_and_the_set_says_so() -> None:
    """**The most important fact about this set, asserted so it cannot be forgotten.**

    214 of 250 labels are ``ESCALATE``. A scorer reporting bare accuracy over it would credit a
    constant answer with 85.6%, and the whole design of ``scorer.py`` follows from that. If a future
    corpus change balanced the set, this test would fail — and the scorer's baseline reporting could
    then be reconsidered rather than left in place for a reason that had stopped being true.
    """
    golden = load_golden_set()
    counts = golden.by_expected_treatment
    majority = max(counts.values()) / len(golden.records)

    assert counts["escalate"] == 214
    assert majority > 0.8, (
        "the set is no longer dominated by one label; the scorer's baseline reporting was designed "
        "for an imbalance that has gone"
    )
    assert len(counts) >= 3, "a set with fewer than three distinct labels cannot show confusion"


def test_the_priceable_records_are_a_small_minority_and_are_not_empty() -> None:
    """The records where a wrong answer is a wrong financial instruction.

    Both bounds matter: zero would make ``accuracy_on_priceable`` undefined and the headline
    meaningless, and a majority would mean the imbalance above had gone.
    """
    golden = load_golden_set()
    priceable = [
        record
        for record in golden.records
        if record.expected_treatment != TreatmentCode.ESCALATE.value
    ]
    assert len(priceable) == 36
    assert 0 < len(priceable) < len(golden.records) // 2
