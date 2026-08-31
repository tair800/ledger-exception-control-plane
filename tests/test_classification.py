"""Unit tests for deterministic residual classification — increment M2.3.

The decision is a pure function, so almost everything worth asserting is assertable without a
database. Three things are being proven here:

* each reachable class fires on the evidence it declares, and **does not** fire when one piece of
  that evidence is removed — a rule that only ever fires is not a rule;
* precedence is a declared property rather than a consequence of source order;
* the classifier cannot reach the things it must not: no ledger entry, no free text, no fixture
  construction label, no monetary output.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import itertools
import pathlib
import uuid
from typing import Final

import pytest

from ledger_exception_control_plane.classification import (
    CLASSIFIER_VERSION,
    RULE_CLASSIFICATION,
    RULE_PRECEDENCE,
    RULES,
    ClassificationRule,
    MovementType,
    SettlementMovement,
    accounting_period,
    classify,
    correlation_id_for,
)
from ledger_exception_control_plane.classification.engine import _Evidence, _relate
from ledger_exception_control_plane.db.control import ExceptionClassification

CLASSIFICATION_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "ledger_exception_control_plane"
    / "classification"
)

ORDER: Final = "ORD-2026-000001"
NAMESPACE: Final = uuid.UUID("2f6f6b7a-0000-4000-8000-000000000000")

CAPTURE = MovementType.CAPTURE
FEE = MovementType.FEE
REFUND = MovementType.REFUND
CHARGEBACK = MovementType.CHARGEBACK
CB_REVERSAL = MovementType.CHARGEBACK_REVERSAL

JUNE = dt.date(2026, 6, 15)
JULY = dt.date(2026, 7, 3)


def movement(
    label: str,
    amount: str,
    kind: MovementType | None,
    *,
    matched: bool = False,
    reference: str | None = ORDER,
    currency: str = "EUR",
    value_date: dt.date = JUNE,
) -> SettlementMovement:
    """One settlement line, addressed by a readable label so failures name the row.

    ``kind`` is positional and has no default on purpose. Every rule now turns on the declared
    movement type, so a test that forgot to state one would be exercising a case the classifier
    treats as evidence-free — which is exactly the case a default would hide.
    """
    return SettlementMovement(
        id=uuid.uuid5(NAMESPACE, label),
        merchant_reference=reference,
        movement=kind,
        amount=decimal.Decimal(amount),
        currency=currency,
        value_date=value_date,
        matched=matched,
    )


def outcome(
    subject: SettlementMovement, *context: SettlementMovement
) -> tuple[ExceptionClassification, ClassificationRule]:
    """Classify one subject against a context, returning the class and the rule that reached it."""
    (decision,) = classify([subject], list(context))
    assert decision.line_id == subject.id
    return decision.classification, decision.rule_id


# ======================================================================================
# chargeback_reversal — a credit that exactly reverses a debit the ledger already booked
# ======================================================================================


def test_a_credit_reversing_a_booked_debit_is_a_chargeback_reversal() -> None:
    """The positive case. The ledger carries the chargeback; nothing carries its reversal."""
    reversal = movement("cb-reversal", "326.92", CB_REVERSAL)
    assert outcome(reversal, movement("cb", "-326.92", CHARGEBACK, matched=True)) == (
        ExceptionClassification.CHARGEBACK_REVERSAL,
        ClassificationRule.REVERSAL_OF_BOOKED_CHARGEBACK,
    )


@pytest.mark.parametrize(
    "declared",
    [MovementType.REFUND, MovementType.ADJUSTMENT, MovementType.CAPTURE, MovementType.FEE, None],
)
def test_a_credit_that_is_not_a_declared_chargeback_reversal_is_never_classified_as_one(
    declared: MovementType | None,
) -> None:
    """The correction, stated as directly as it can be stated.

    Every case here is a credit that exactly reverses a chargeback the ledger has already booked —
    identical to the real thing in sign, amount, currency, date and counterpart. Only the declared
    movement type differs, and none of them says ``chargeback_reversal``.

    The old rule read direction alone and called all of these chargeback reversals. A refund
    reversal is not a chargeback reversal; an operational correction is not either; and ``None`` —
    a type this system does not recognise, or a row that predates the column — is not evidence of
    anything. The safe answer is the fallback, and coverage is not a reason to prefer any other.
    """
    credit = movement("credit", "326.92", declared)
    booked = movement("cb", "-326.92", CHARGEBACK, matched=True)

    classification, rule = outcome(credit, booked)
    assert classification is not ExceptionClassification.CHARGEBACK_REVERSAL
    assert classification is ExceptionClassification.UNCLASSIFIED
    assert rule is ClassificationRule.NO_RULE_MATCHED


def test_a_declared_chargeback_reversal_needs_a_declared_chargeback_to_reverse() -> None:
    """The other half: the declaration on this row is not enough on its own.

    The booked counterpart here is a ``capture``, so there is no chargeback for this credit to
    reverse whatever the PSP called it. Evidence has to agree with itself — a declared type nobody
    corroborates is a claim, not a fact — and this is what stops the fix from being "trust the type
    field", which would be a different single point of failure rather than none.
    """
    credit = movement("credit", "326.92", CB_REVERSAL)
    booked_capture = movement("capture", "-326.92", CAPTURE, matched=True)

    assert outcome(credit, booked_capture) == (
        ExceptionClassification.UNCLASSIFIED,
        ClassificationRule.NO_RULE_MATCHED,
    )


def test_a_credit_whose_counterpart_the_ledger_never_booked_is_not_a_reversal() -> None:
    """The near miss that matters most.

    Same two rows, same amounts, same order — but the ledger never reconciled the debit. There is
    then nothing booked to reverse, and the pair is some other condition entirely. A rule that
    ignored ``matched`` would call this a chargeback reversal and be wrong about which of two
    unreconciled rows the ledger holds.
    """
    reversal = movement("cb-reversal", "326.92", CB_REVERSAL)
    classification, rule = outcome(reversal, movement("cb", "-326.92", CHARGEBACK, matched=False))
    assert classification is ExceptionClassification.UNCLASSIFIED
    assert rule is ClassificationRule.NO_RULE_MATCHED


def test_a_credit_reversing_a_different_amount_is_not_a_reversal() -> None:
    """Exact equality, not a band. One cent apart is not a reversal of that movement."""
    assert outcome(
        movement("credit", "326.92", CB_REVERSAL),
        movement("debit", "-326.91", CHARGEBACK, matched=True),
    )[0] is (ExceptionClassification.UNCLASSIFIED)


def test_a_credit_with_two_booked_offsets_refuses_rather_than_choosing() -> None:
    """Ambiguity is information. Two booked debits of the same size both exactly offset the credit,
    and nothing says which one it reverses — so the classifier declines, the same way the matcher
    declines a line with two candidate entries."""
    classification, rule = outcome(
        movement("credit", "50.00", CB_REVERSAL),
        movement("debit-a", "-50.00", CHARGEBACK, matched=True),
        movement("debit-b", "-50.00", CHARGEBACK, matched=True),
    )
    assert classification is ExceptionClassification.UNCLASSIFIED
    assert rule is ClassificationRule.NO_RULE_MATCHED


def test_a_counterpart_on_a_different_order_is_not_related() -> None:
    """The relation is keyed on the merchant's reference, exactly. A booked debit belonging to
    another order explains nothing about this credit."""
    assert (
        outcome(
            movement("credit", "50.00", CB_REVERSAL),
            movement("debit", "-50.00", CHARGEBACK, matched=True, reference="ORD-2026-999999"),
        )[0]
        is ExceptionClassification.UNCLASSIFIED
    )


def test_a_counterpart_in_another_currency_is_not_related() -> None:
    """No conversion happens anywhere in this system, so amounts in different currencies do not
    offset each other — they are incomparable, not merely unequal."""
    assert (
        outcome(
            movement("credit", "50.00", CB_REVERSAL),
            movement("debit", "-50.00", CHARGEBACK, matched=True, currency="USD"),
        )[0]
        is ExceptionClassification.UNCLASSIFIED
    )


# ======================================================================================
# cross_period_refund — a debit reversing a booked credit, across a period boundary
# ======================================================================================


def test_a_refund_settling_in_a_later_period_is_a_cross_period_refund() -> None:
    refund = movement("refund", "-540.86", REFUND, value_date=JULY)
    assert outcome(
        refund, movement("capture", "540.86", CAPTURE, matched=True, value_date=JUNE)
    ) == (
        ExceptionClassification.CROSS_PERIOD_REFUND,
        ClassificationRule.REFUND_OF_BOOKED_CAPTURE_ACROSS_PERIODS,
    )


def test_a_refund_settling_in_its_own_period_falls_to_the_fallback() -> None:
    """The near miss for the period test, and an honest gap rather than a bug.

    A refund that settles in the month of the capture it reverses is a real condition — and FR-4's
    taxonomy has no class for it. Borrowing ``cross_period_refund`` would make the class name a lie
    on every such row, so the line is unclassified and an analyst decides.
    """
    refund = movement("refund", "-540.86", REFUND, value_date=dt.date(2026, 6, 28))
    classification, rule = outcome(
        refund, movement("capture", "540.86", CAPTURE, matched=True, value_date=JUNE)
    )
    assert classification is ExceptionClassification.UNCLASSIFIED
    assert rule is ClassificationRule.NO_RULE_MATCHED


def test_an_in_period_refund_is_not_relabelled_by_the_group_rule() -> None:
    """The regression for the defect adversarial review found, and it was a real one.

    A full refund of an already-booked capture *in the same period* is the case the taxonomy has no
    class for, and the period test declines it correctly. But precedence orders the rules that
    *fire* — so with one further unmatched credit on the same order, the group-shape rule was free
    to claim the line, and the refund came back ``fee_split``. Adding an unrelated row to an order
    changed the class of the refund, and a customer refund was labelled a PSP deduction.

    It is the same defect M2.2 corrected in ADR-043, in a new place: an unresolved claim from a
    higher-priority rule being settled by a lower-priority one. Any exact offset already booked now
    means the line is a reversal question, and a reversal question the system cannot answer is
    ``unclassified``.
    """
    booked = movement("capture", "200.00", CAPTURE, matched=True, value_date=dt.date(2026, 6, 5))
    refund = movement("refund", "-200.00", REFUND, value_date=dt.date(2026, 6, 20))
    unrelated = movement("second-capture", "250.00", CAPTURE, value_date=dt.date(2026, 6, 22))

    assert outcome(refund, booked, refund, unrelated) == (
        ExceptionClassification.UNCLASSIFIED,
        ClassificationRule.NO_RULE_MATCHED,
    )
    # And the answer must not depend on that third row existing at all.
    assert outcome(refund, booked, refund)[0] is ExceptionClassification.UNCLASSIFIED


def test_ambiguous_reversal_evidence_does_not_fall_through_to_the_group_rule() -> None:
    """The same hole, reached by the other route.

    Two booked movements that both exactly offset the subject make the reversal unprovable, and the
    reversal rules decline. Before the fix, the group rule then settled the line anyway — resolving
    an ambiguity by weakening the rule that detected it.
    """
    subject = movement("credit", "50.00", CB_REVERSAL)
    first = movement("debit-a", "-50.00", CHARGEBACK, matched=True)
    second = movement("debit-b", "-50.00", CHARGEBACK, matched=True)
    fee = movement("fee", "-1.00", FEE)

    assert outcome(subject, subject, first, second, fee) == (
        ExceptionClassification.UNCLASSIFIED,
        ClassificationRule.NO_RULE_MATCHED,
    )


def test_a_fee_split_is_unaffected_by_the_reversal_block() -> None:
    """The complement: the block must not have quietly disabled the rule it guards.

    None of a genuine fee split's rows offsets anything the ledger booked, so all three still
    classify. Without this, the two tests above would pass against a rule that never fires.
    """
    gross = movement("gross", "1244.71", CAPTURE)
    fee = movement("fee", "-7.94", FEE)
    assert outcome(gross, gross, fee)[0] is ExceptionClassification.FEE_SPLIT
    assert outcome(fee, gross, fee)[0] is ExceptionClassification.FEE_SPLIT


def test_the_period_boundary_is_the_calendar_month_not_a_day_count() -> None:
    """One day apart across a month end is a period crossing; twenty-nine days inside one is not.

    Stated as a test because "different period" is the kind of phrase that quietly becomes "more
    than thirty days" when someone reimplements it.
    """
    assert accounting_period(dt.date(2026, 6, 30)) != accounting_period(dt.date(2026, 7, 1))
    assert accounting_period(dt.date(2026, 6, 1)) == accounting_period(dt.date(2026, 6, 30))
    assert accounting_period(dt.date(2026, 6, 5)) == "2026-06"
    assert accounting_period(dt.date(2026, 12, 31)) == "2026-12"


def test_the_period_is_read_from_the_business_date_and_never_from_a_clock() -> None:
    """A classification that changed with the day it was re-run would not be a classification.

    ``accounting_period`` takes the date it reports on, so there is no clock to read; the guard
    below proves the package never reaches for one either.
    """
    for year in (2020, 2026, 2031):
        assert accounting_period(dt.date(year, 2, 14)) == f"{year}-02"


# ======================================================================================
# fee_split — one movement the PSP reported across several rows
# ======================================================================================


def test_a_capture_reported_with_its_fees_is_a_fee_split() -> None:
    """Every row of the split is classified, not just the capture: all three are residual, all
    three belong to the same condition, and each needs its own decision."""
    gross = movement("gross", "1244.71", CAPTURE)
    scheme = movement("scheme-fee", "-2.13", FEE)
    processing = movement("processing-fee", "-7.94", FEE)

    for subject in (gross, scheme, processing):
        assert outcome(subject, gross, scheme, processing) == (
            ExceptionClassification.FEE_SPLIT,
            ClassificationRule.FEES_DEDUCTED_FROM_A_CAPTURE,
        )


def test_rows_of_one_sign_are_not_a_split() -> None:
    """Two captures on one order — the repeated-reference case — carry no deduction, so there is
    nothing split. Without this the rule would fire on any order with more than one row."""
    first = movement("dup-1", "948.24", CAPTURE)
    second = movement("dup-2", "964.43", CAPTURE)
    assert outcome(first, first, second)[0] is ExceptionClassification.UNCLASSIFIED


def test_a_deduction_that_equals_the_inflow_is_an_offset_not_a_split() -> None:
    """The near miss the strictness condition exists for.

    A chargeback and its reversal, neither reconciled, are equal and opposite on one order — the
    same *shape* as a capture with a fee, and a completely different condition. A fee comes out of
    a capture and so must be strictly smaller than it; an equal deduction is a reversal.
    """
    debit = movement("cb", "-500.00", CHARGEBACK)
    credit = movement("cb-reversal", "500.00", CB_REVERSAL)
    assert outcome(credit, debit, credit)[0] is ExceptionClassification.UNCLASSIFIED
    assert outcome(debit, debit, credit)[0] is ExceptionClassification.UNCLASSIFIED


def test_deductions_exceeding_the_inflow_are_not_a_split() -> None:
    """A deduction larger than what it is deducted from is not a fee, whatever it is."""
    capture = movement("capture", "10.00", CAPTURE)
    huge = movement("adjustment", "-4000.00", FEE)
    assert outcome(capture, capture, huge)[0] is ExceptionClassification.UNCLASSIFIED


def test_a_reconciled_row_is_not_part_of_the_split() -> None:
    """The group is the *unreconciled* rows. A capture the ledger already booked is settled, and
    counting it would let a fee left over from a reconciled capture look like a live split."""
    fee = movement("fee", "-2.13", FEE)
    assert outcome(fee, movement("gross", "1244.71", CAPTURE, matched=True), fee)[0] is (
        ExceptionClassification.UNCLASSIFIED
    )


# ======================================================================================
# The fallback
# ======================================================================================


def test_a_line_with_no_related_movements_is_unclassified() -> None:
    """The lone residual — a partial capture, an FX rounding difference, a near miss. Nothing in
    the settlement data relates it to anything, and the ledger entry that would explain it cannot
    be identified. The fallback is the correct answer, not a failure to reach one."""
    classification, rule = outcome(movement("lonely", "2799.97", CAPTURE))
    assert classification is ExceptionClassification.UNCLASSIFIED
    assert rule is ClassificationRule.NO_RULE_MATCHED


def test_lines_with_no_merchant_reference_are_never_related_to_each_other() -> None:
    """Two absent references are not a shared reference.

    Both rows would otherwise form a group — a credit and a smaller debit — and be called a fee
    split on the strength of a value neither of them has. Manufacturing a relationship out of two
    nulls is the one thing a control system must never do with missing data.
    """
    credit = movement("anon-credit", "500.00", CAPTURE, reference=None)
    debit = movement("anon-debit", "-2.00", FEE, reference=None)
    assert outcome(credit, credit, debit)[0] is ExceptionClassification.UNCLASSIFIED
    assert outcome(debit, credit, debit)[0] is ExceptionClassification.UNCLASSIFIED


def test_a_zero_amount_movement_has_no_direction_and_is_unclassified() -> None:
    """Every rule turns on direction, and zero has none. It falls through rather than being
    silently treated as a credit by a ``>= 0`` written where ``> 0`` was meant."""
    assert outcome(
        movement("zero", "0.00", CB_REVERSAL), movement("booked", "0.00", CHARGEBACK, matched=True)
    )[0] is (ExceptionClassification.UNCLASSIFIED)


def test_the_fallback_carries_a_rule_id_of_its_own() -> None:
    """``unclassified`` is a decision the system took, not an absence of one. A row recording it
    with no rule id would be indistinguishable from a row written before this classifier existed."""
    _, rule = outcome(movement("lonely", "1.00", CAPTURE))
    assert rule is ClassificationRule.NO_RULE_MATCHED
    assert RULE_CLASSIFICATION[rule] is ExceptionClassification.UNCLASSIFIED


# ======================================================================================
# Precedence, determinism and the shape of the rule set
# ======================================================================================


def test_exact_offset_evidence_excludes_the_group_rule_entirely() -> None:
    """Evidence about the line itself removes it from the group rule's reach.

    This credit exactly reverses a booked debit *and* sits in an unreconciled group carrying a
    smaller deduction. Before the review this was the rule set's one genuine overlap, resolved by
    the declared precedence; the overlap turned out to be the defect rather than the design, because
    precedence only orders the rules that *fire* and left a declining reversal rule to be overruled.

    Now the group rule declines whenever any booked offset exists, so a line the reversal family has
    a claim on is never available to it — whatever the family concludes.
    """
    subject = movement("credit", "500.00", CB_REVERSAL)
    booked = movement("booked-debit", "-500.00", CHARGEBACK, matched=True)
    stray_fee = movement("fee", "-1.50", FEE)
    evidence = _evidence_for(subject, subject, booked, stray_fee)

    assert RULES[ClassificationRule.REVERSAL_OF_BOOKED_CHARGEBACK](evidence)
    assert not RULES[ClassificationRule.FEES_DEDUCTED_FROM_A_CAPTURE](evidence)
    assert outcome(subject, subject, booked, stray_fee) == (
        ExceptionClassification.CHARGEBACK_REVERSAL,
        ClassificationRule.REVERSAL_OF_BOOKED_CHARGEBACK,
    )


def test_no_two_rules_can_ever_fire_on_the_same_line() -> None:
    """The rule set is pairwise disjoint, so the outcome cannot depend on rule order at all.

    That is a stronger property than "precedence resolves the overlap", and it is the one this
    increment ended up with: the two reversal rules differ by sign, and the group rule requires
    *zero* booked offsets where both reversal rules require exactly one. :data:`RULE_PRECEDENCE`
    remains declared and inspectable, and is now a safety net a future fourth rule would need rather
    than something today's answers depend on.

    Swept over the shapes that could plausibly collide rather than asserted from the source, because
    the whole lesson of the finding this replaces is that reading the rules is how the overlap was
    missed.
    """
    booked_offsets = (0, 1, 2)
    for amount in ("500.00", "-500.00", "0.00"):
        for offsets in booked_offsets:
            for same_period in (True, False):
                for extra_fee in (True, False):
                    subject = movement("subject", amount, CB_REVERSAL, value_date=JULY)
                    context = [subject]
                    for index in range(offsets):
                        context.append(
                            movement(
                                f"offset-{index}",
                                str(-decimal.Decimal(amount)),
                                CHARGEBACK,
                                matched=True,
                                value_date=JULY if same_period else JUNE,
                            )
                        )
                    if extra_fee:
                        context.append(movement("fee", "-1.00", FEE, value_date=JULY))

                    evidence = _evidence_for(subject, *context)
                    fired = [rule for rule, predicate in RULES.items() if predicate(evidence)]
                    assert len(fired) <= 1, (
                        f"{fired} both fired for amount={amount} offsets={offsets} "
                        f"same_period={same_period} extra_fee={extra_fee}"
                    )


def _evidence_for(subject: SettlementMovement, *context: SettlementMovement) -> _Evidence:
    """Build the private evidence view the rule predicates take, for the disjointness test above.

    Reaching into a private helper is deliberate and confined to this one place: the disjointness
    claim is about the *predicates*, and going through :func:`classify` would only show which rule
    won, never whether a second one also fired.
    """
    (evidence,) = _relate([subject], list(context))
    return evidence


def test_every_rule_states_the_class_it_assigns() -> None:
    """A total mapping. A rule added without a class would otherwise fail at the moment it first
    fired, in production, on a residual."""
    assert set(RULE_CLASSIFICATION) == set(ClassificationRule)
    assert set(RULES) == set(ClassificationRule) - {ClassificationRule.NO_RULE_MATCHED}
    assert set(RULE_PRECEDENCE) == set(RULES)
    assert len(RULE_PRECEDENCE) == len(set(RULE_PRECEDENCE)), "precedence lists each rule once"


def test_no_rule_assigns_a_class_the_evidence_cannot_support() -> None:
    """``partial_capture`` and ``fx_rounding`` are declared in the taxonomy and assigned by nothing.

    Both are claims about a line's relationship to one particular ledger entry, and no deterministic
    key identifies that entry — so no rule may reach them. Asserted rather than left to review,
    because the tempting fix for a low coverage number is to point one of these at a shape that
    merely resembles it.
    """
    assigned = set(RULE_CLASSIFICATION.values())
    assert ExceptionClassification.PARTIAL_CAPTURE not in assigned
    assert ExceptionClassification.FX_ROUNDING not in assigned
    assert assigned == {
        ExceptionClassification.CHARGEBACK_REVERSAL,
        ExceptionClassification.CROSS_PERIOD_REFUND,
        ExceptionClassification.FEE_SPLIT,
        ExceptionClassification.UNCLASSIFIED,
    }


def test_classification_does_not_depend_on_the_order_the_inputs_arrive_in() -> None:
    """Every permutation of a group produces the same decision for every line.

    The rules are set-based by construction, and this is what makes that a checked property rather
    than an intention. A classification that changed with the order rows came back from a query
    would be a defect even when both answers looked reasonable.
    """
    rows = [
        movement("gross", "1000.00", CAPTURE),
        movement("fee", "-4.00", FEE),
        movement("booked-capture", "250.00", CAPTURE, matched=True),
        movement("refund", "-250.00", REFUND, value_date=JULY),
        movement("orphan", "77.00", CAPTURE, reference=None),
    ]
    expected = {d.line_id: (d.classification, d.rule_id) for d in classify(rows, rows)}
    assert len(expected) == len(rows)

    for permutation in itertools.permutations(rows):
        ordered = list(permutation)
        assert {
            d.line_id: (d.classification, d.rule_id) for d in classify(ordered, ordered)
        } == expected


def test_repeating_the_same_classification_gives_the_same_answer() -> None:
    """Stability across runs, with no clock, counter or random source able to move it."""
    rows = [movement("gross", "80.00", CAPTURE), movement("fee", "-1.00", FEE)]
    first = classify(rows, rows)
    for _ in range(5):
        assert classify(rows, rows) == first


def test_a_caller_need_not_include_the_subjects_in_the_context() -> None:
    """The context is unioned with the subjects inside :func:`classify`, so forgetting to pass them
    cannot silently change an answer — a group of three rows classified with an empty context would
    otherwise look like three unrelated lines."""
    rows = [movement("gross", "60.00", CAPTURE), movement("fee", "-2.00", FEE)]
    assert classify(rows, []) == classify(rows, rows) == classify(rows, rows + rows)


def test_the_correlation_id_is_derived_and_stable() -> None:
    """Same file, same row, same id — including across a re-delivery, because it is derived from
    the payload hash rather than from anything about this particular run (§11)."""
    digest = "a" * 64
    assert correlation_id_for(digest, 7) == f"lecp:{digest}:000007"
    assert correlation_id_for(digest, 7) == correlation_id_for(digest, 7)
    assert correlation_id_for(digest, 7) != correlation_id_for(digest, 8)
    assert correlation_id_for(digest, 8) != correlation_id_for("b" * 64, 8)
    assert len(correlation_id_for(digest, 999999)) <= 128, "must fit exception.correlation_id"


def test_the_classifier_version_is_a_declared_constant_in_the_persisted_shape() -> None:
    """Bumped by hand when the rules change; never derived from a clock or a commit, both of which
    would move without a decision having been taken. The shape matches the column's check."""
    import re

    assert re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,31}", CLASSIFIER_VERSION)
    for rule in ClassificationRule:
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rule.value), rule


# ======================================================================================
# Scope: what this package must not be able to reach
# ======================================================================================


def _referenced_names(tree: ast.Module) -> list[str]:
    """Every identifier the module reaches for, by any of the three routes that can reach one.

    Bare names and attributes are the obvious two. **Import aliases are the third**, and leaving
    them out was a real hole: ``from ...db.models import LedgerEntry`` binds the name without
    producing an ``ast.Name`` at the import itself, so a guard walking only names and attributes
    accepted a module that had pulled the ledger table straight in. Found by the injection test at
    the bottom of this file, which is the entire reason that test exists.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.alias):
            found.append(node.name.rsplit(".", 1)[-1])
            if node.asname:
                found.append(node.asname)
    return found


def _classification_sources() -> list[tuple[str, ast.Module]]:
    paths = sorted(CLASSIFICATION_ROOT.rglob("*.py"))
    assert len(paths) >= 4, "the guards must be walking real files"
    return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def test_no_float_appears_anywhere_in_the_classification_package() -> None:
    """Every monetary comparison is ``Decimal``. In binary floating point the fee-split test
    ``sum(deductions) < max(inflows)`` can flip on a value that is exactly equal."""
    for name, tree in _classification_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "float", f"{name} references float"
            if isinstance(node, ast.Attribute):
                assert node.attr != "float", f"{name} references float"


def test_the_classification_package_cannot_reach_the_fixture_corpus() -> None:
    """The ground-truth firewall.

    Tests may compare this classifier's output to what the corpus was *constructed* to represent.
    Production code may not read that construction label, because a classifier that could would be
    graded against its own input and every accuracy number in the project would be circular.
    """
    for name, tree in _classification_sources():
        dumped = ast.dump(tree)
        for label in (
            "fixtures",
            "intended_classification",
            "scenario_id",
            "MatchIntent",
            "ScenarioKind",
            "Awkwardness",
            "catalogue",
        ):
            assert label not in dumped, f"{name} references {label}"


def test_the_classification_package_never_imports_the_matcher() -> None:
    """An allowlist, and the strongest available statement that M2.3 does not re-match.

    ``matching`` is absent from it deliberately: importing the engine would put ``CandidateEntry``,
    the tolerance policy and the mutual-uniqueness rule within reach, and "we simply do not call
    it" is a promise rather than a property. M2.2 stays the single matching authority because this
    package cannot express matching at all.
    """
    permitted_stdlib = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "uuid",
        "typing",
    }
    permitted_third_party = {"sqlalchemy"}
    permitted_internal = {
        "ledger_exception_control_plane.classification",
        "ledger_exception_control_plane.db.models",
        "ledger_exception_control_plane.db.control",
    }
    for name, tree in _classification_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("ledger_exception_control_plane"):
                    assert any(module.startswith(p) for p in permitted_internal), (
                        f"{name} imports {module}, which is outside the classification boundary"
                    )
                else:
                    assert module.split(".")[0] in permitted_stdlib | permitted_third_party, (
                        f"{name} imports {module}, which is not on the allowlist"
                    )


def test_the_classification_package_never_touches_a_ledger_entry_or_a_match_result() -> None:
    """Containment against re-matching, at the level of the names themselves.

    A rule cannot consume a ledger entry it cannot name, and cannot record a match it cannot
    construct. ``MatchResult`` appears only as an eligibility predicate — this asserts that even
    that is a read — while ``LedgerEntry`` must not appear at all.
    """
    forbidden = {"LedgerEntry", "CandidateEntry", "CandidateLine", "TolerancePolicy", "MatchRule"}
    writers = {"insert", "pg_insert", "update", "delete"}
    for name, tree in _classification_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in forbidden, f"{name} references {referenced}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if called in writers:
                    for argument in node.args:
                        assert not (
                            isinstance(argument, ast.Name) and argument.id == "MatchResult"
                        ), f"{name} writes to match_result via {called}()"


def test_the_classification_package_computes_no_adjustment_and_touches_no_money_path() -> None:
    """M2.4's boundary, asserted from this side.

    Classification answers what condition can be proved; it never answers what should be posted.
    The calculator, the account mapping, the period assignment and the treatment enum all belong to
    later increments, and an amount computed here would reach the ledger with no approval behind it.
    """
    forbidden = {
        "Adjustment",
        "compute_adjustment",
        "TreatmentCode",
        "TreatmentProposal",
        "Approval",
        "account_code",
        "operation_id",
        "proposed_amount",
        "adjustment_amount",
    }
    for name, tree in _classification_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in forbidden, f"{name} references {referenced}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in forbidden, f"{name} contains the literal {node.value!r}"


def test_the_calculator_lives_outside_classification_and_is_not_reachable_from_it() -> None:
    """The M2.4 boundary, from this side.

    This assertion used to be "no calculator exists anywhere", which was right while M2.4 was
    unstarted and became wrong the moment it landed. What it was really protecting is the
    *direction*: classification answers what condition can be proved, the calculator answers what a
    treatment instructs, and the first must never reach the second. A residual's class cannot depend
    on what it would cost to fix.
    """
    root = CLASSIFICATION_ROOT.parent
    assert (root / "money" / "calculator.py").exists(), "M2.4 exists and lives in its own package"

    for _name, tree in _classification_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in {"compute_adjustment", "AdjustmentInstruction"}, referenced
        dumped = ast.dump(tree)
        assert "money" not in dumped, "classification must not reach the money path"


def test_the_classifier_sees_no_free_text_and_no_ledger_field() -> None:
    """Containment by construction, the same way ``CandidateLine`` contains the matcher.

    ``psp_reference`` is excluded as deliberately as ``memo`` is. The corpus builds a fee split as
    ``X``, ``X-fee1``, ``X-fee2`` and a reversal as ``X``, ``X-rev``, so a classifier that could see
    the PSP's reference could read those suffixes and score beautifully against this corpus while
    encoding nothing but one generator's naming habit.
    """
    forbidden = {
        "memo",
        "description",
        "psp_reference",
        "account_code",
        "external_ref",
        "ledger_entry_id",
        "rationale",
        "scenario_id",
    }
    assert not (set(SettlementMovement.__dataclass_fields__) & forbidden)
    assert set(SettlementMovement.__dataclass_fields__) == {
        "id",
        "merchant_reference",
        "movement",
        "amount",
        "currency",
        "value_date",
        "matched",
    }


def test_the_classification_package_reads_no_clock() -> None:
    """No ``now``, no ``today``, no ``utcnow``. The only date in play is the value date the
    settlement file stated, so re-running in a different month cannot move a classification."""
    for name, tree in _classification_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"now", "utcnow", "today", "time", "monotonic"}, (
                    f"{name} reads a clock via .{node.attr}"
                )
            if isinstance(node, ast.Name):
                assert node.id not in {"random", "randint", "choice", "shuffle"}, (
                    f"{name} references {node.id}"
                )


@pytest.mark.parametrize(
    ("guard", "violation"),
    [
        (
            test_no_float_appears_anywhere_in_the_classification_package,
            "difference = float(line.amount)",
        ),
        (
            test_the_classification_package_cannot_reach_the_fixture_corpus,
            "from ledger_exception_control_plane.fixtures.catalogue import BuiltScenario",
        ),
        (
            test_the_classification_package_never_imports_the_matcher,
            "from ledger_exception_control_plane.matching import match",
        ),
        (
            test_the_classification_package_never_touches_a_ledger_entry_or_a_match_result,
            "from ledger_exception_control_plane.db.models import LedgerEntry",
        ),
        (
            test_the_classification_package_computes_no_adjustment_and_touches_no_money_path,
            "from ledger_exception_control_plane.db.control import Adjustment",
        ),
        (
            test_the_classification_package_reads_no_clock,
            "assigned_at = dt.datetime.now()",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_each_guard_rejects_the_violation_it_exists_for(
    guard: object, violation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard nobody has seen fail is a guard nobody knows works.

    Each is re-run against a source tree carrying **one** violation — its own, not a pile of them —
    so a guard that passed by coincidence while a different guard did the rejecting is caught here.
    The violation is injected into the *parsed* sources rather than onto disk, so a crashed test
    cannot leave a poisoned file in the package.
    """
    monkeypatch.setattr(
        "tests.test_classification._classification_sources",
        lambda: [("injected.py", ast.parse(violation))],
    )
    with pytest.raises(AssertionError):
        guard()  # type: ignore[operator]


def test_the_injected_violations_are_the_only_reason_those_guards_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the proof above: on a clean tree every one of them passes.

    Without this, a guard that raised unconditionally would sail through the parametrised test and
    look like it was doing its job.
    """
    monkeypatch.setattr(
        "tests.test_classification._classification_sources",
        lambda: [("injected.py", ast.parse("x = 1"))],
    )
    test_no_float_appears_anywhere_in_the_classification_package()
    test_the_classification_package_cannot_reach_the_fixture_corpus()
    test_the_classification_package_never_imports_the_matcher()
    test_the_classification_package_never_touches_a_ledger_entry_or_a_match_result()
    test_the_classification_package_computes_no_adjustment_and_touches_no_money_path()
    test_the_classification_package_reads_no_clock()
