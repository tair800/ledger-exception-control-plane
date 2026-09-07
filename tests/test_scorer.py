"""M6.1 — the scorer: the arithmetic, and the ways a score can mislead.

The plan asks 6.1 for *"scorer unit tests"*. The arithmetic is the easy half and is checked first
against hand-built sets where every number is countable by eye.

**The other half is what the tests are really for.** A scorer over this golden set can be right and
still mislead, because 214 of 250 labels are ``ESCALATE``: report accuracy alone and a model that
decides nothing looks 85.6% good. So the tests below drive **adversarial answerers** — a constant,
an inverted answerer, one that skips half the set — and assert the report exposes each of them.
A metric that cannot be embarrassed by a constant is not a metric.

No database, no model, no network.
"""

from __future__ import annotations

import dataclasses

import pytest

from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from tests.evaluation.golden import GoldenRecord, GoldenSet, load_golden_set
from tests.evaluation.labels import label_for
from tests.evaluation.scorer import CassetteOrigin, Proposal, Score, score


def _record(
    exception_id: str,
    classification: ExceptionClassification,
    *,
    originating_period: str | None = None,
) -> GoldenRecord:
    """A golden record whose label comes from the real label module, not from a literal.

    Built through :func:`label_for` so a hand-made fixture cannot disagree with the declaration the
    committed set was generated from — which would make these tests pass against a scorer that had
    stopped matching the artefact.
    """
    label = label_for(classification, originating_period=originating_period)
    return GoldenRecord(
        exception_id=exception_id,
        classification=classification.value,
        rule_id="no_rule_matched",
        amount="100.00",
        currency="EUR",
        value_date="2026-06-01",
        settlement_period="2026-06",
        originating_period=originating_period,
        transaction_type="capture",
        has_merchant_reference=True,
        expected_treatment=label.treatment.value,
        label_rule=label.rule.value,
        label_source=label.source.value,
        label_why=label.why,
        escalation_is_correct=label.escalation_is_correct,
        held_out=False,
    )


def _set(*records: GoldenRecord) -> GoldenSet:
    return GoldenSet(schema_version="1", seed=1, profile="bulk", instances=1, records=records)


#: Two priceable records and two that are not: small enough to count by hand, and shaped like the
#: committed set in the one way that matters — a mix of escalate and non-escalate labels.
SMALL: GoldenSet = _set(
    _record("a", ExceptionClassification.CHARGEBACK_REVERSAL),
    _record("b", ExceptionClassification.CROSS_PERIOD_REFUND, originating_period="2026-05"),
    _record("c", ExceptionClassification.FEE_SPLIT),
    _record("d", ExceptionClassification.UNCLASSIFIED),
)


def _perfect(golden: GoldenSet) -> list[Proposal]:
    return [
        Proposal(
            record.exception_id,
            TreatmentCode(record.expected_treatment),
            abstained=record.escalation_is_correct,
        )
        for record in golden.records
    ]


def _constant(golden: GoldenSet, treatment: TreatmentCode) -> list[Proposal]:
    return [
        Proposal(record.exception_id, treatment, abstained=treatment is TreatmentCode.ESCALATE)
        for record in golden.records
    ]


# ======================================================================================
# The arithmetic
# ======================================================================================


def test_the_small_set_is_shaped_the_way_these_tests_assume() -> None:
    """Stated once so every count below can be read without re-deriving it."""
    assert [record.expected_treatment for record in SMALL.records] == [
        "rebook",
        "accrue",
        "escalate",
        "escalate",
    ]


def test_a_perfect_answerer_scores_everything() -> None:
    result = score(SMALL, _perfect(SMALL), origin=CassetteOrigin.CAPTURED)

    assert (result.scored, result.correct) == (4, 4)
    assert result.accuracy == 1.0
    assert result.accuracy_on_priceable == 1.0
    assert result.priceable == 2
    assert result.abstained_where_a_treatment_was_available == 0
    assert result.is_complete


def test_the_confusion_matrix_records_every_pair_that_occurred() -> None:
    """§20 asks for confusion across treatment codes. Off-diagonal entries are the mistakes."""
    wrong = [
        Proposal("a", TreatmentCode.ACCRUE),
        Proposal("b", TreatmentCode.ACCRUE),
        Proposal("c", TreatmentCode.ESCALATE, abstained=True),
        Proposal("d", TreatmentCode.WRITE_OFF),
    ]
    result = score(SMALL, wrong, origin=CassetteOrigin.CAPTURED)

    assert result.confusion == {
        ("accrue", "accrue"): 1,
        ("escalate", "escalate"): 1,
        ("escalate", "write_off"): 1,
        ("rebook", "accrue"): 1,
    }
    assert result.correct == 2
    assert sum(result.confusion.values()) == result.scored


def test_abstention_is_split_by_whether_escalating_was_correct() -> None:
    """**One rate would be uninterpretable on this set, so there are two.**

    Abstaining on a fee split is the right action. Abstaining on a chargeback reversal is a refusal
    to do the job. A single "abstention rate" averages a virtue and a failure into one number.
    """
    abstains_on_everything = _constant(SMALL, TreatmentCode.ESCALATE)
    result = score(SMALL, abstains_on_everything, origin=CassetteOrigin.CAPTURED)

    assert result.abstentions == 4
    assert result.abstention_rate == 1.0
    assert result.abstained_where_escalation_was_correct == 2
    assert result.abstained_where_a_treatment_was_available == 2


def test_an_escalate_that_was_chosen_is_not_counted_as_an_abstention() -> None:
    """§6.1 requires an abstaining proposal to carry ``ESCALATE``, so the two are not the same fact.

    A model that *chose* to refer a case has decided something; one that declined to answer has
    not. Deriving abstention from the treatment code would erase that distinction, and the rate
    would then be a count of a vocabulary member rather than of a behaviour.
    """
    chosen = [Proposal(record.exception_id, TreatmentCode.ESCALATE) for record in SMALL.records]
    result = score(SMALL, chosen, origin=CassetteOrigin.CAPTURED)

    assert result.correct == 2, "two of the four labels are escalate"
    assert result.abstentions == 0
    assert result.abstention_rate == 0.0


# ======================================================================================
# The adversarial answerers — what the report has to expose
# ======================================================================================


def test_a_constant_answerer_is_exposed_by_the_baseline_and_the_priceable_split() -> None:
    """**The test this scorer exists for.**

    On the committed set, answering ``ESCALATE`` to all 250 exceptions is 85.6% accurate. Three
    numbers have to make that visible, and all three are asserted here: the baseline it is compared
    with, the zero lift over it, and the accuracy on the records where the answer changes what
    happens.
    """
    golden = load_golden_set()
    result = score(
        golden, _constant(golden, TreatmentCode.ESCALATE), origin=CassetteOrigin.CAPTURED
    )

    assert result.accuracy == pytest.approx(0.856, abs=0.001)
    assert result.majority_baseline == pytest.approx(result.accuracy, abs=1e-9)
    assert result.lift_over_baseline == pytest.approx(0.0, abs=1e-9)
    assert result.accuracy_on_priceable == 0.0, (
        "the constant is wrong on every record where a treatment was available"
    )
    assert result.priceable == 36
    assert result.abstained_where_a_treatment_was_available == 36


def test_the_headline_of_a_constant_answerer_cannot_be_quoted_as_a_good_result() -> None:
    """The headline is what someone will paste into a README, so it carries the caveats itself."""
    golden = load_golden_set()
    result = score(
        golden, _constant(golden, TreatmentCode.ESCALATE), origin=CassetteOrigin.CAPTURED
    )
    line = result.headline()

    assert "0.0% on the 36 priceable" in line
    assert "+0.0%" in line
    assert "85.6% constant-answer baseline" in line


def test_an_answerer_worse_than_a_constant_reports_a_negative_lift() -> None:
    """A model that is worse than answering one code every time must say so in one number."""
    golden = load_golden_set()
    inverted = [
        Proposal(
            record.exception_id,
            TreatmentCode.REBOOK
            if record.expected_treatment == TreatmentCode.ESCALATE.value
            else TreatmentCode.ESCALATE,
        )
        for record in golden.records
    ]
    result = score(golden, inverted, origin=CassetteOrigin.CAPTURED)

    assert result.lift_over_baseline < 0
    assert result.accuracy < result.majority_baseline


def test_a_run_that_skipped_records_is_reported_rather_than_scored_on_what_it_managed() -> None:
    """**Silently scoring a partial run is how a harness reports a number nobody can reproduce.**

    An answerer that returns half the set and gets that half right is not 100% accurate. The
    unanswered ids are listed and :attr:`Score.is_complete` is false, which the CLI turns into a
    non-zero exit.
    """
    half = _perfect(SMALL)[:2]
    result = score(SMALL, half, origin=CassetteOrigin.CAPTURED)

    assert result.scored == 2
    assert result.accuracy == 1.0, "it did get the two it answered right"
    assert result.unanswered == ("c", "d")
    assert not result.is_complete


def test_a_proposal_for_an_unknown_exception_is_reported_and_not_scored() -> None:
    """The other direction: an answer to something not in the set is a harness fault, not a
    score."""
    result = score(
        SMALL,
        [*_perfect(SMALL), Proposal("not-in-the-set", TreatmentCode.REBOOK)],
        origin=CassetteOrigin.CAPTURED,
    )

    assert result.scored == 4
    assert result.unknown_ids == ("not-in-the-set",)
    assert not result.is_complete


def test_an_empty_run_scores_zero_rather_than_dividing_by_zero() -> None:
    result = score(SMALL, [], origin=CassetteOrigin.CAPTURED)

    assert (result.scored, result.correct) == (0, 0)
    assert result.accuracy == 0.0
    assert result.accuracy_on_priceable == 0.0
    assert result.abstention_rate == 0.0
    assert not result.is_complete


# ======================================================================================
# Origin: what a run is allowed to claim
# ======================================================================================


def test_only_a_captured_run_is_a_statement_about_a_model() -> None:
    """The committed cassettes are synthesised, so the default state of this repository is a run
    that measures the harness. The flag says so rather than a reader having to know."""
    perfect = _perfect(SMALL)

    assert score(SMALL, perfect, origin=CassetteOrigin.CAPTURED).measures_a_model is True
    assert score(SMALL, perfect, origin=CassetteOrigin.SYNTHESISED).measures_a_model is False
    assert score(SMALL, perfect, origin=CassetteOrigin.NO_MODEL).measures_a_model is False


def test_a_synthesised_run_is_headlined_as_a_harness_check_and_says_so_in_capitals() -> None:
    """**`CLAUDE.md` §10, enforced on the one string most likely to be quoted.**

    A perfect score over synthesised cassettes is a real fact about the harness and no fact at all
    about a model. The sentence has to be unusable as an evaluation result, because whoever copies
    it will not copy the caveat from a docstring.
    """
    line = score(SMALL, _perfect(SMALL), origin=CassetteOrigin.SYNTHESISED).headline()

    assert "THIS IS NOT A MODEL MEASUREMENT" in line
    assert "written by the cassette builder" in line
    assert "accuracy" not in line.split("THIS IS NOT")[0].replace("agreement", "")


def test_a_no_model_run_is_headlined_as_deterministic_and_names_no_model() -> None:
    line = score(SMALL, _perfect(SMALL), origin=CassetteOrigin.NO_MODEL).headline()

    assert "No model was involved." in line
    assert "deterministic arm" in line


def test_the_origin_must_be_stated_and_has_no_default() -> None:
    """A default would be supplied by every caller that forgot it, and the wrong default would
    present a synthesised run as an evaluation result — the one outcome this module must prevent."""
    with pytest.raises(TypeError):
        score(SMALL, _perfect(SMALL))  # type: ignore[call-arg]


def test_the_score_is_a_frozen_record_so_a_reader_cannot_adjust_a_number() -> None:
    """Every field is computed once. A mutable score is a number somebody can tune after seeing it,
    which is the same defect the chaos suite's declared expectations exist to prevent."""
    result = score(SMALL, _perfect(SMALL), origin=CassetteOrigin.CAPTURED)

    assert dataclasses.is_dataclass(Score)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.correct = 0  # type: ignore[misc]
