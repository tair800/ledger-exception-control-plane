"""Classification *precision*, not coverage — did the classifier assign the right class?

A coverage rate says how many residuals were given a name. It says nothing about whether the names
were right, and the two pull in opposite directions: a classifier that guessed the most common
class for every residual would report excellent coverage and be wrong most of the time. Because an
exception is the control record a treatment, an approval and eventually a ledger posting are all
built on (§8), a wrong class is not a mislabel — it is the first step of a wrong posting.

The M1.3 corpus makes precision measurable. Every settlement line carries the ``scenario_id`` it
was *constructed* for, and each scenario declares the ``intended_classification`` its condition
represents, so each residual can be graded against what it actually is. Four outcomes are
distinguished, and conflating them is how a classifier gets graded generously:

* **correct** — the assigned class is the intended one;
* **under-classified** — ``unclassified`` where a class was intended. Safe: a human decides;
* **wrong** — some *other* class where a class was intended. The failure this file exists to count;
* **no declared intent** — the scenario deliberately declines to predict its own outcome, so there
  is no truth to be right or wrong against.

**This file and its PostgreSQL counterpart are the only places that read construction metadata**,
and they read it to *grade* production output, never to produce it. The classification package
cannot reach any of it — asserted separately by the guards in ``test_classification.py``.

The invariant that matters: **no wrong deterministic classification.** Asserted as zero, not as a
rate.
"""

from __future__ import annotations

import collections
import dataclasses

import pytest

from ledger_exception_control_plane.classification import (
    SettlementMovement,
    classify,
    movement_type,
)
from ledger_exception_control_plane.db.control import ExceptionClassification
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    match,
)

SEED = 20260829


@dataclasses.dataclass(frozen=True, slots=True)
class Measurement:
    """One grading of the classifier against the corpus it was run over."""

    residuals: int
    correct: int
    under_classified: int
    wrong: tuple[tuple[str, str, str], ...]
    no_declared_intent: int
    by_class: dict[str, int]
    by_rule: dict[str, int]
    confusion: dict[tuple[str, str], int]


def measure(profile: Profile, instances: int = 200) -> Measurement:
    """Run the real matcher, then the real classifier, and grade every decision it took."""
    corpus = generate(SEED, profile, instances)

    rows = {row.id: row for batch in corpus.corpus.batches for row in batch.lines}
    outcome = match(
        [
            CandidateLine(row.id, row.line_number, row.amount, row.currency, row.value_date)
            for row in rows.values()
        ],
        [
            CandidateEntry(e.id, e.external_ref, e.amount, e.currency, e.booked_at.date())
            for e in corpus.corpus.ledger_entries
        ],
        DEFAULT_POLICY,
    )
    matched = {pair.line_id for pair in outcome.matches}

    movements = [
        SettlementMovement(
            id=row.id,
            merchant_reference=row.merchant_reference,
            movement=movement_type(row.transaction_type),
            amount=row.amount,
            currency=row.currency,
            value_date=row.value_date,
            matched=row.id in matched,
        )
        for row in rows.values()
    ]
    residuals = [movement for movement in movements if not movement.matched]
    decisions = classify(residuals, movements)

    intent = {
        scenario.scenario_id: scenario.intended_classification
        for scenario in corpus.scenarios.scenarios
    }
    correct = under = no_intent = 0
    wrong: list[tuple[str, str, str]] = []
    confusion: collections.Counter[tuple[str, str]] = collections.Counter()

    for decision in decisions:
        scenario = rows[decision.line_id].scenario_id
        intended = intent[scenario]
        assigned = decision.classification
        confusion[(scenario, assigned.value)] += 1
        if intended is None:
            no_intent += 1
        elif assigned is intended:
            correct += 1
        elif assigned is ExceptionClassification.UNCLASSIFIED:
            under += 1
        else:
            wrong.append((scenario, intended.value, assigned.value))

    return Measurement(
        residuals=len(residuals),
        correct=correct,
        under_classified=under,
        wrong=tuple(wrong),
        no_declared_intent=no_intent,
        by_class=dict(collections.Counter(d.classification.value for d in decisions)),
        by_rule=dict(collections.Counter(d.rule_id.value for d in decisions)),
        confusion=dict(confusion),
    )


# --------------------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "instances"),
    [
        (Profile.CANONICAL, 200),
        (Profile.BULK, 200),
        (Profile.BULK, 1000),
        (Profile.BULK, 4000),
    ],
)
def test_no_residual_is_ever_given_a_class_it_does_not_have(
    profile: Profile, instances: int
) -> None:
    """**Zero wrong classifications.** Asserted as a count, not as a rate.

    Run at four sizes because the rules are relational: a residual is classified from the other
    settlement lines on its merchant reference, and at 4,300 lines two unrelated orders can collide
    on one reference. The rules refuse those rather than resolving them, so the count that grows
    with scale is ``unclassified``, never this one.
    """
    result = measure(profile, instances)
    assert result.wrong == (), (
        f"{len(result.wrong)} wrong classification(s), e.g. {result.wrong[:3]}"
    )


@pytest.mark.parametrize(
    ("profile", "instances"),
    [(Profile.CANONICAL, 200), (Profile.BULK, 200), (Profile.BULK, 4000)],
)
def test_every_assigned_class_is_the_class_the_scenario_was_built_to_represent(
    profile: Profile, instances: int
) -> None:
    """The same invariant from the other end: precision on assigned classes is exactly 1.

    Everything that got a name got the right one. What the classifier does not reach, it declines
    to name — so the only way to move this number is to be wrong.
    """
    result = measure(profile, instances)
    assigned = sum(
        count for (_, klass), count in result.confusion.items() if klass != "unclassified"
    )
    assert assigned > 0
    assert result.correct >= assigned
    assert result.wrong == ()


def test_precision_is_reported_in_full_for_the_canonical_corpus() -> None:
    """Every figure the increment claims, pinned. A change to any of them must be deliberate."""
    result = measure(Profile.CANONICAL)

    assert result.residuals == 13
    assert result.correct == 9
    assert result.under_classified == 3
    assert result.wrong == ()
    assert result.no_declared_intent == 1
    assert result.by_class == {
        "unclassified": 8,
        "fee_split": 3,
        "chargeback_reversal": 1,
        "cross_period_refund": 1,
    }
    assert result.by_rule == {
        "no_rule_matched": 8,
        "fees_deducted_from_a_capture": 3,
        "reversal_of_booked_chargeback": 1,
        "refund_of_booked_capture_across_periods": 1,
    }


def test_precision_is_reported_in_full_for_the_bulk_corpus() -> None:
    result = measure(Profile.BULK, 200)

    assert result.residuals == 39
    assert result.correct == 23
    assert result.under_classified == 9
    assert result.wrong == ()
    assert result.no_declared_intent == 7
    assert result.by_class == {
        "unclassified": 21,
        "fee_split": 12,
        "chargeback_reversal": 4,
        "cross_period_refund": 2,
    }


def test_coverage_is_reported_alongside_precision_rather_than_instead_of_it() -> None:
    """Coverage is the secondary number and is recorded as such.

    Roughly two residuals in five are classified, and the shortfall is almost entirely the two
    classes the evidence cannot support. Reporting only this figure would make the honest refusal
    below look like a defect; reporting only precision would hide how much work still reaches a
    human. Both are stated.
    """
    result = measure(Profile.BULK, 4000)
    classified = result.residuals - result.by_class["unclassified"]

    assert result.residuals == 833
    assert classified == 360
    assert 100 * classified // result.residuals == 43
    assert result.wrong == ()
    assert result.under_classified == 195


# --------------------------------------------------------------------------------------
# Where the classifier stops, and why
# --------------------------------------------------------------------------------------


def test_every_reachable_class_is_fully_cleared_wherever_its_condition_appears() -> None:
    """The three reachable classes are assigned to *every* instance of their condition.

    Precision without this would be cheap: a classifier that fired once and abstained forever after
    would also report zero wrong answers. Each rule fires on all of its scenario's residuals or the
    rule is too narrow.
    """
    for profile, instances in ((Profile.CANONICAL, 200), (Profile.BULK, 1000)):
        result = measure(profile, instances)
        for (scenario, assigned), count in result.confusion.items():
            if scenario.startswith("SC-005"):
                assert assigned == "fee_split", f"{scenario} left {count} rows unclassified"
            if scenario.startswith("SC-006"):
                assert assigned == "chargeback_reversal"
            if scenario.startswith("SC-008"):
                assert assigned == "cross_period_refund"


def test_the_two_unreachable_classes_are_under_classified_and_never_guessed() -> None:
    """The honest limitation, measured rather than described.

    ``partial_capture`` (SC-004, SC-011) and ``fx_rounding`` (SC-007) are claims about a residual's
    relationship to one particular ledger entry, and nothing deterministically identifies that
    entry. Every one of those residuals lands in ``unclassified`` — not in some neighbouring class
    that happens to fit the shape, which is what makes this a limitation rather than a defect.
    """
    result = measure(Profile.BULK, 4000)
    unreachable = {"SC-004": 120, "SC-007": 55, "SC-011": 20}

    for prefix, expected in unreachable.items():
        rows = {
            klass: count
            for (scenario, klass), count in result.confusion.items()
            if scenario.startswith(prefix)
        }
        assert rows == {"unclassified": expected}, f"{prefix} was given a class it cannot support"

    assert sum(unreachable.values()) == result.under_classified


def test_a_scenario_that_declines_to_predict_its_own_outcome_is_not_graded() -> None:
    """SC-003 is ``tolerance_policy_dependent`` and declares no intended classification, so its
    residuals can be neither right nor wrong. They are counted separately rather than silently
    scored as correct — which is what a ``got == expected or expected is None`` shortcut would do.
    """
    result = measure(Profile.BULK, 1000)
    undeclared = sum(
        count
        for (scenario, _), count in result.confusion.items()
        if scenario.startswith(("SC-001", "SC-002", "SC-003"))
    )
    assert undeclared == result.no_declared_intent == 46


def test_wrong_classifications_stay_at_zero_as_reference_collisions_become_likely() -> None:
    """The trade the design makes, measured.

    The rules relate lines through the merchant's reference, and at volume two unrelated orders can
    draw the same one. A classifier that resolved those would show a rising wrong-answer count;
    this one shows a rising ``unclassified`` count and no wrong answers at any size.
    """
    small, large = measure(Profile.BULK, 200), measure(Profile.BULK, 4000)
    assert large.by_class["unclassified"] > small.by_class["unclassified"]
    assert small.wrong == () and large.wrong == ()


def test_the_measurement_needs_metadata_the_classifier_never_receives() -> None:
    """The firewall, stated where it is most tempting to breach.

    This file grades using ``scenario_id`` and ``intended_classification``. The classifier is handed
    :class:`SettlementMovement`, which has a field for neither — so the comparison performed here is
    one production code physically cannot make.
    """
    fields = set(SettlementMovement.__dataclass_fields__)
    assert not fields & {"scenario_id", "intended_classification", "intent", "kind", "awkwardness"}

    corpus = generate(SEED, Profile.CANONICAL, 200)
    assert corpus.scenarios.scenarios[0].scenario_id
    assert corpus.corpus.batches[0].lines[0].scenario_id, (
        "the corpus must carry the metadata this file grades against"
    )
