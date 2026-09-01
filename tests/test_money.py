"""The deterministic adjustment calculator — increment M2.4.

Everything here runs without a database, a network, a clock or a model, because the thing under test
is a pure function and that is the whole claim. Four groups:

* the supported calculation matrix, every combination stated as data rather than as prose;
* the refusals — each closed reason reached by the condition it names, and nothing reached by two;
* the money contract: exact ``Decimal``, explicit currency, declared quantisation, no rounding;
* the firewall — what the calculator cannot see, cannot import, and cannot be handed.

The last group carries mutation tests. A guard nobody has watched fail is a guard nobody knows
works, so each one is re-run against a source tree with its own violation injected.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import decimal
import inspect
import pathlib
import uuid
from typing import Final

import pytest

from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.money import (
    ACCOUNT_CHARGEBACKS,
    ACCOUNT_REVENUE,
    ACCOUNT_WRITE_OFFS,
    DEMO_ACCOUNT_POLICY,
    DEMO_LEDGER_CONTEXT,
    ROUNDING,
    AccountPolicy,
    AdjustmentInstruction,
    AmbiguousAccountPolicyError,
    ExceptionFacts,
    LedgerContext,
    NonCalculable,
    account_policy,
    compute_adjustment,
    is_period,
    period_of,
)

MONEY_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane" / "money"
)

CB_REVERSAL = ExceptionClassification.CHARGEBACK_REVERSAL
REFUND = ExceptionClassification.CROSS_PERIOD_REFUND
FEE_SPLIT = ExceptionClassification.FEE_SPLIT
UNCLASSIFIED = ExceptionClassification.UNCLASSIFIED
PARTIAL_CAPTURE = ExceptionClassification.PARTIAL_CAPTURE
FX_ROUNDING = ExceptionClassification.FX_ROUNDING

REBOOK = TreatmentCode.REBOOK
ACCRUE = TreatmentCode.ACCRUE
WRITE_OFF = TreatmentCode.WRITE_OFF
ESCALATE = TreatmentCode.ESCALATE

#: A fixed identifier. The calculation must not depend on it, which a test below proves by changing
#: it and demanding an identical amount.
EXCEPTION_ID: Final = uuid.UUID("3f2c1a44-0000-4000-8000-000000000001")

JULY_10 = dt.date(2026, 7, 10)


def facts(
    classification: ExceptionClassification,
    amount: str,
    *,
    currency: str = "EUR",
    value_date: dt.date = JULY_10,
    originating_period: str | None = "2026-06",
    exception_id: uuid.UUID = EXCEPTION_ID,
) -> ExceptionFacts:
    """One exception's structured facts. Amounts are built from text, never from a float."""
    return ExceptionFacts(
        exception_id=exception_id,
        classification=classification,
        amount=decimal.Decimal(amount),
        currency=currency,
        value_date=value_date,
        originating_period=originating_period,
    )


# ======================================================================================
# The supported calculation matrix
# ======================================================================================

#: Every combination the demo policy supports, as data.
#:
#: Stated as a table rather than as one test per case so that the matrix *is* the test: a
#: combination cannot be quietly supported without appearing here, and the closure test below
#: asserts every pair not listed refuses.
SUPPORTED: Final = [
    # classification, treatment, amount, expected account, expected period
    (CB_REVERSAL, REBOOK, "326.92", ACCOUNT_CHARGEBACKS, "2026-07"),
    (CB_REVERSAL, ACCRUE, "326.92", ACCOUNT_CHARGEBACKS, "2026-06"),
    (CB_REVERSAL, WRITE_OFF, "326.92", ACCOUNT_WRITE_OFFS, "2026-07"),
    (REFUND, REBOOK, "-540.86", ACCOUNT_REVENUE, "2026-07"),
    (REFUND, ACCRUE, "-540.86", ACCOUNT_REVENUE, "2026-06"),
    (REFUND, WRITE_OFF, "-540.86", ACCOUNT_WRITE_OFFS, "2026-07"),
]


@pytest.mark.parametrize(
    ("classification", "treatment", "amount", "account", "period"),
    SUPPORTED,
    ids=lambda v: v.value if hasattr(v, "value") else str(v),
)
def test_every_supported_combination_prices_exactly(
    classification: ExceptionClassification,
    treatment: TreatmentCode,
    amount: str,
    account: str,
    period: str,
) -> None:
    """The whole matrix, amount by amount.

    The amount is the settlement movement's own, unchanged and sign included, in every row — that is
    the single formula. What the treatment changes is the account and the period, never the number,
    which is the property that makes the containment claim structural.
    """
    result = compute_adjustment(facts(classification, amount), treatment, DEMO_LEDGER_CONTEXT)

    assert isinstance(result, AdjustmentInstruction)
    assert result.amount == decimal.Decimal(amount)
    assert result.currency == "EUR"
    assert result.account_code == account
    assert result.period == period
    assert result.treatment is treatment
    assert result.exception_id == EXCEPTION_ID
    assert result.ledger_context_version == DEMO_LEDGER_CONTEXT.version


def test_the_treatment_never_changes_the_amount() -> None:
    """Read directly off the matrix: within a class, every treatment prices the same number.

    This is the invariant the AI boundary rests on. A model may one day influence the treatment
    code; if a treatment could move the amount, that influence would reach money. It cannot, because
    the amount comes from the settlement line and the treatment selects only where it lands.
    """
    for classification in (CB_REVERSAL, REFUND):
        amounts = {
            result.amount
            for treatment in (REBOOK, ACCRUE, WRITE_OFF)
            if isinstance(
                result := compute_adjustment(
                    facts(classification, "123.45"), treatment, DEMO_LEDGER_CONTEXT
                ),
                AdjustmentInstruction,
            )
        }
        assert amounts == {decimal.Decimal("123.45")}, classification


def test_the_sign_of_the_settlement_movement_is_preserved() -> None:
    """A credit stays a credit and a debit stays a debit. Direction is the sign, not a flag.

    ``adjustment`` holds one signed amount against one account, so there is no debit/credit pair to
    get the wrong way round — and no place for a sign to be flipped by a treatment.
    """
    credit = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, DEMO_LEDGER_CONTEXT)
    debit = compute_adjustment(facts(REFUND, "-540.86"), REBOOK, DEMO_LEDGER_CONTEXT)

    assert isinstance(credit, AdjustmentInstruction) and credit.amount > 0
    assert isinstance(debit, AdjustmentInstruction) and debit.amount < 0
    assert -debit.amount == decimal.Decimal("540.86")


def test_a_negative_amount_is_not_treated_as_an_error_or_flipped() -> None:
    """Refunds, fees and chargebacks are legitimately negative — ``settlement_line`` imposes no sign
    constraint for exactly that reason, and neither does this."""
    for amount in ("-0.0001", "-1", "-9999999999999999.9999"):
        result = compute_adjustment(facts(REFUND, amount), REBOOK, DEMO_LEDGER_CONTEXT)
        assert isinstance(result, AdjustmentInstruction)
        assert result.amount == decimal.Decimal(amount)


# ======================================================================================
# Closure: everything not in the matrix refuses
# ======================================================================================


def test_every_combination_outside_the_matrix_fails_closed() -> None:
    """The complement of the matrix, swept exhaustively.

    Six classes times four treatments is twenty-four combinations; six are supported. This asserts
    the other eighteen all refuse — so a class cannot become priceable by accident, and a new
    treatment cannot inherit an account it was never configured for.
    """
    supported = {(c, t) for c, t, *_ in SUPPORTED}
    for classification in ExceptionClassification:
        for treatment in TreatmentCode:
            result = compute_adjustment(
                facts(classification, "100.00"), treatment, DEMO_LEDGER_CONTEXT
            )
            if (classification, treatment) in supported:
                assert isinstance(result, AdjustmentInstruction)
            else:
                assert isinstance(result, NonCalculable), (
                    f"{classification.value} + {treatment.value} was priced and should not be"
                )


@pytest.mark.parametrize("treatment", [REBOOK, ACCRUE, WRITE_OFF, ESCALATE])
def test_unclassified_can_never_produce_an_adjustment(treatment: TreatmentCode) -> None:
    """The system could not say what the residual is, so it cannot say what to post.

    Named separately from the sweep above because it is the one a coverage number would push
    hardest against: ``unclassified`` is the largest group of residuals by far, and pricing it would
    improve every percentage in the project while being a guess every time.
    """
    result = compute_adjustment(facts(UNCLASSIFIED, "2799.97"), treatment, DEMO_LEDGER_CONTEXT)
    assert isinstance(result, NonCalculable)


@pytest.mark.parametrize("treatment", [REBOOK, ACCRUE, WRITE_OFF])
def test_a_fee_split_is_never_priced_from_one_of_its_rows(treatment: TreatmentCode) -> None:
    """The ledger already carries the net; the rows are its decomposition.

    Pricing one row would post part of a movement whose whole the calculator cannot see, and would
    double-count what is already booked. The correct treatment is a two-legged reclassification,
    which one signed amount against one account cannot express.
    """
    result = compute_adjustment(facts(FEE_SPLIT, "1244.71"), treatment, DEMO_LEDGER_CONTEXT)
    assert result is NonCalculable.NO_ACCOUNT_MAPPED


@pytest.mark.parametrize("classification", [PARTIAL_CAPTURE, FX_ROUNDING])
def test_the_classes_no_exception_can_carry_are_not_priced(
    classification: ExceptionClassification,
) -> None:
    """M2.3 assigns neither (ADR-045), so configuring an account for them would claim a capability
    that does not exist. Asserted so that making them reachable later is a deliberate act in two
    places rather than one."""
    result = compute_adjustment(facts(classification, "100.00"), REBOOK, DEMO_LEDGER_CONTEXT)
    assert result is NonCalculable.NO_ACCOUNT_MAPPED


@pytest.mark.parametrize("classification", list(ExceptionClassification))
def test_escalate_is_refused_before_anything_else_is_considered(
    classification: ExceptionClassification,
) -> None:
    """``escalate`` is not a pricing failure — it is the outcome that says pricing was never
    appropriate, and ``adjustment`` refuses a row for one outright. It reports its own reason for
    every class, including the classes that are otherwise priceable."""
    result = compute_adjustment(facts(classification, "326.92"), ESCALATE, DEMO_LEDGER_CONTEXT)
    assert result is NonCalculable.TREATMENT_IS_ESCALATE


def test_an_account_cannot_be_configured_for_escalate() -> None:
    """Closed at the configuration boundary too. An account mapped to ``escalate`` could never be
    used, and its presence would imply the opposite."""
    with pytest.raises(ValueError, match="escalate is never posted"):
        account_policy([(CB_REVERSAL, ESCALATE, ACCOUNT_WRITE_OFFS)])


# ======================================================================================
# Account mapping
# ======================================================================================


def test_a_missing_mapping_fails_closed_rather_than_defaulting() -> None:
    """An empty policy prices nothing. There is no fallback account and no default: a combination
    nobody configured cannot produce a financial instruction."""
    empty = dataclasses.replace(DEMO_LEDGER_CONTEXT, accounts=account_policy([]))
    result = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, empty)
    assert result is NonCalculable.NO_ACCOUNT_MAPPED


def test_a_pair_configured_twice_is_refused_when_the_policy_is_built() -> None:
    """Ambiguous configuration raises where it is written, not halfway through a calculation.

    A ``dict`` literal would have resolved this by keeping whichever rule came last — a silent
    choice between two ledger accounts, which is exactly the decision a data structure must not
    make on anyone's behalf.
    """
    with pytest.raises(AmbiguousAccountPolicyError, match="4900 and 6900"):
        account_policy(
            [
                (CB_REVERSAL, REBOOK, ACCOUNT_CHARGEBACKS),
                (CB_REVERSAL, REBOOK, ACCOUNT_WRITE_OFFS),
            ]
        )


def test_a_neighbouring_treatment_does_not_borrow_another_account() -> None:
    """Within one class, ``write_off`` goes somewhere else than ``rebook`` — and the difference is
    the account, not the amount. A policy that returned one account for everything would pass every
    positive test above."""
    rebook = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, DEMO_LEDGER_CONTEXT)
    write_off = compute_adjustment(facts(CB_REVERSAL, "326.92"), WRITE_OFF, DEMO_LEDGER_CONTEXT)

    assert isinstance(rebook, AdjustmentInstruction)
    assert isinstance(write_off, AdjustmentInstruction)
    assert rebook.account_code != write_off.account_code
    assert rebook.amount == write_off.amount


def test_a_neighbouring_class_does_not_borrow_another_account() -> None:
    """A refund does not post to the chargeback account, and a chargeback reversal does not reduce
    revenue. The two structured keys both matter."""
    chargeback = compute_adjustment(facts(CB_REVERSAL, "100.00"), REBOOK, DEMO_LEDGER_CONTEXT)
    refund = compute_adjustment(facts(REFUND, "-100.00"), REBOOK, DEMO_LEDGER_CONTEXT)

    assert isinstance(chargeback, AdjustmentInstruction)
    assert isinstance(refund, AdjustmentInstruction)
    assert chargeback.account_code == ACCOUNT_CHARGEBACKS
    assert refund.account_code == ACCOUNT_REVENUE


def test_a_malformed_account_code_is_refused_at_configuration() -> None:
    """The column accepts a bounded string, so the policy checks the shape where it is declared —
    not when something eventually tries to post it."""
    for bad in ("", "revenue", "41", "41000", " 4100"):
        with pytest.raises(ValueError, match="account code"):
            account_policy([(CB_REVERSAL, REBOOK, bad)])


def test_the_demo_policy_maps_only_what_it_claims_to() -> None:
    """The configuration itself, pinned. A change to what is priceable must be deliberate."""
    assert set(DEMO_ACCOUNT_POLICY.rules) == {(c, t) for c, t, *_ in SUPPORTED}
    assert DEMO_ACCOUNT_POLICY.account_for(FEE_SPLIT, REBOOK) is None
    assert DEMO_ACCOUNT_POLICY.account_for(UNCLASSIFIED, WRITE_OFF) is None


# ======================================================================================
# Period assignment
# ======================================================================================


def test_rebook_and_write_off_use_the_settlement_period() -> None:
    """The movement is recognised when it settled, so the period is the one its value date falls
    in — the settlement file's date, never today's."""
    for treatment in (REBOOK, WRITE_OFF):
        result = compute_adjustment(
            facts(CB_REVERSAL, "10.00", value_date=dt.date(2026, 9, 30)),
            treatment,
            dataclasses.replace(DEMO_LEDGER_CONTEXT, earliest_open_period="2026-01"),
        )
        assert isinstance(result, AdjustmentInstruction)
        assert result.period == "2026-09"


def test_accrue_uses_the_originating_period_not_the_settlement_period() -> None:
    """The whole difference between the two treatments.

    A refund settling in July that reverses a June capture is accrued into June, so the two land
    together. If ``accrue`` fell back to the settlement period it would produce the same instruction
    as ``rebook`` while claiming to be a different treatment.
    """
    result = compute_adjustment(
        facts(REFUND, "-540.86", value_date=JULY_10, originating_period="2026-06"),
        ACCRUE,
        DEMO_LEDGER_CONTEXT,
    )
    assert isinstance(result, AdjustmentInstruction)
    assert result.period == "2026-06"
    assert period_of(JULY_10) == "2026-07", "and it is genuinely a different period"


def test_accrue_without_an_originating_period_refuses() -> None:
    """There is nothing to accrue *into*. Falling back to the settlement date would silently turn an
    accrual into a rebooking."""
    result = compute_adjustment(
        facts(REFUND, "-540.86", originating_period=None), ACCRUE, DEMO_LEDGER_CONTEXT
    )
    assert result is NonCalculable.NO_ORIGINATING_PERIOD


@pytest.mark.parametrize(
    ("value_date", "expected"),
    [
        (dt.date(2026, 6, 1), "2026-06"),
        (dt.date(2026, 6, 30), "2026-06"),
        (dt.date(2026, 7, 1), "2026-07"),
        (dt.date(2026, 12, 31), "2026-12"),
        (dt.date(2027, 1, 1), "2027-01"),
        (dt.date(2028, 2, 29), "2028-02"),
    ],
)
def test_period_boundaries_are_exact(value_date: dt.date, expected: str) -> None:
    """Last day of a month, first day of the next, the year boundary, and a leap day. A period is a
    calendar month and nothing about it is a day count."""
    assert period_of(value_date) == expected


@pytest.mark.parametrize(
    "malformed", ["not-a-period", "2026-13", "2026-00", "26-6", "2026-6", "", "2026-06-01", "  "]
)
def test_a_malformed_originating_period_is_refused_rather_than_carried(malformed: str) -> None:
    """The one field a caller *derives* rather than reads, and therefore the one that can arrive
    malformed.

    It flows straight into ``adjustment.period``, which the column constrains to ``YYYY-MM``.
    Unchecked, ``"2026-13"`` produced a financial instruction carrying a month that does not exist
    and the failure surfaced at persistence in a later increment; the empty string was worse, since
    it compared as "closed" and refused for a reason that was not the real one.

    Refused rather than raised, because the calculator must stay total — a malformed input must not
    be able to end a batch run.
    """
    result = compute_adjustment(
        facts(REFUND, "-540.86", originating_period=malformed), ACCRUE, DEMO_LEDGER_CONTEXT
    )
    assert result is NonCalculable.PERIOD_MALFORMED


def test_the_period_a_supported_calculation_emits_always_has_the_right_shape() -> None:
    """The complement: every priced instruction carries a period the column would accept."""
    for classification, treatment, amount, *_ in SUPPORTED:
        result = compute_adjustment(facts(classification, amount), treatment, DEMO_LEDGER_CONTEXT)
        assert isinstance(result, AdjustmentInstruction)
        assert is_period(result.period), result.period


def test_a_closed_period_refuses_rather_than_finding_the_next_open_one() -> None:
    """No approved policy says where a movement belonging to a closed period should go instead, and
    "the next open period" is a decision nobody has taken. Refusing keeps that decision with the
    people entitled to make it."""
    closed = dataclasses.replace(DEMO_LEDGER_CONTEXT, earliest_open_period="2026-08")
    result = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, closed)
    assert result is NonCalculable.PERIOD_CLOSED


def test_the_open_period_boundary_is_inclusive_and_orders_across_a_year() -> None:
    """The earliest open period is itself open, and the comparison is chronological.

    ``YYYY-MM`` is zero-padded and fixed-width so string order is date order — true, but exactly the
    kind of claim that deserves a test at a year boundary rather than a comment.
    """
    ctx = dataclasses.replace(DEMO_LEDGER_CONTEXT, earliest_open_period="2026-12")
    assert ctx.is_open("2026-12")
    assert ctx.is_open("2027-01")
    assert not ctx.is_open("2026-11")
    assert not ctx.is_open("2025-12")


def test_a_ledger_context_refuses_a_malformed_period_or_currency() -> None:
    for bad_period in ("2026-13", "2026-00", "26-06", "2026-6", ""):
        with pytest.raises(ValueError, match="YYYY-MM"):
            dataclasses.replace(DEMO_LEDGER_CONTEXT, earliest_open_period=bad_period)
    for bad_currency in ("eur", "EURO", "E", ""):
        with pytest.raises(ValueError, match="ISO 4217"):
            dataclasses.replace(DEMO_LEDGER_CONTEXT, functional_currency=bad_currency)


# ======================================================================================
# Currency, zero, and the money contract
# ======================================================================================


def test_an_adjustment_in_another_currency_refuses_rather_than_converting() -> None:
    """No conversion exists anywhere in this system, so a movement in a currency the books are not
    kept in cannot be priced. Substituting the settlement amount would post the right number in the
    wrong unit, which is worse than posting nothing."""
    for currency in ("USD", "GBP", "JPY", "BHD"):
        result = compute_adjustment(
            facts(CB_REVERSAL, "326.92", currency=currency), REBOOK, DEMO_LEDGER_CONTEXT
        )
        assert result is NonCalculable.CURRENCY_NOT_FUNCTIONAL


def test_the_result_always_states_its_currency() -> None:
    """Never implicit, and never inherited from the ledger context: it is the currency the movement
    actually happened in, which the refusal above guarantees is the functional one."""
    result = compute_adjustment(facts(REFUND, "-1.00"), REBOOK, DEMO_LEDGER_CONTEXT)
    assert isinstance(result, AdjustmentInstruction)
    assert result.currency == "EUR" == DEMO_LEDGER_CONTEXT.functional_currency


def test_a_zero_amount_is_refused_deliberately() -> None:
    """A zero adjustment instructs the ledger to do nothing while carrying the full weight of an
    approved financial instruction. Whatever the residual was, it was not that."""
    for zero in ("0", "0.00", "-0.0000", "0E-4"):
        result = compute_adjustment(facts(CB_REVERSAL, zero), REBOOK, DEMO_LEDGER_CONTEXT)
        assert result is NonCalculable.AMOUNT_IS_ZERO, zero


@pytest.mark.parametrize(
    "amount",
    [
        "1.23456",
        "0.00001",
        "-1.23456",
        "10000000000000000",
        "-10000000000000000",
        "NaN",
        "Infinity",
    ],
)
def test_an_amount_outside_the_money_contract_is_refused_not_rounded(amount: str) -> None:
    """The point of the whole increment, in one assertion.

    Five decimal places, an over-large magnitude, ``NaN`` and infinity all refuse. None is rounded,
    truncated or clamped to fit: inventing a rounding rule so a number satisfies the schema is the
    defect ADR-020 exists to prevent, and it would be a defect here for the same reason.
    """
    result = compute_adjustment(facts(CB_REVERSAL, amount), REBOOK, DEMO_LEDGER_CONTEXT)
    assert result is NonCalculable.AMOUNT_OUTSIDE_MONEY_CONTRACT


@pytest.mark.parametrize(
    "amount",
    [
        "1.00000000000000000000000000001",
        "-1.00000000000000000000000000001",
        "1.000000000000000000000000000000005",
        "0.12345678901234567890123456789",
        "1e-10",
    ],
)
def test_a_long_decimal_is_refused_rather_than_rounded_away_by_the_context(amount: str) -> None:
    """The defect adversarial review found, and it was real.

    The contract check used to scale by ``10**4`` and ask whether the result was integral — the
    right question through the wrong instrument. ``scaleb`` is a *context* operation and rounds to
    the context's precision, 28 significant digits by default, so an amount with 29 decimal places
    scaled to something integral and was **priced**. The guard rounded the evidence away before
    inspecting it, which is the same class of mistake as letting the database round on the way in.
    """
    result = compute_adjustment(facts(CB_REVERSAL, amount), REBOOK, DEMO_LEDGER_CONTEXT)
    assert result is NonCalculable.AMOUNT_OUTSIDE_MONEY_CONTRACT


def test_the_result_does_not_depend_on_the_ambient_decimal_context() -> None:
    """Purity includes not reading process-global state, and the decimal context is exactly that.

    Some unrelated caller setting ``getcontext().prec`` must not be able to change whether a
    financial instruction is produced. Run under a deliberately tiny precision, every answer is
    identical — which the old ``scaleb`` implementation could not have satisfied.
    """
    inputs = [
        (CB_REVERSAL, "326.92"),
        (CB_REVERSAL, "9999999999999999.9999"),
        (CB_REVERSAL, "1.23456"),
        (CB_REVERSAL, "1.00000000000000000000000000001"),
        (REFUND, "-540.86"),
        (REFUND, "120.450000"),
    ]
    baseline = [compute_adjustment(facts(c, a), REBOOK, DEMO_LEDGER_CONTEXT) for c, a in inputs]
    for precision in (1, 3, 9, 60):
        with decimal.localcontext() as ctx:
            ctx.prec = precision
            ctx.rounding = decimal.ROUND_FLOOR
            assert [
                compute_adjustment(facts(c, a), REBOOK, DEMO_LEDGER_CONTEXT) for c, a in inputs
            ] == baseline, f"the answer changed at prec={precision}"


def test_the_magnitude_boundary_matches_the_column_exactly() -> None:
    """The largest value the column holds is priced; one quantum more is refused. Off by one here
    would mean an instruction the database rejects at the moment of posting."""
    largest = compute_adjustment(
        facts(CB_REVERSAL, "9999999999999999.9999"), REBOOK, DEMO_LEDGER_CONTEXT
    )
    assert isinstance(largest, AdjustmentInstruction)
    for over in ("10000000000000000", "-10000000000000000", "10000000000000000.0000"):
        assert compute_adjustment(facts(CB_REVERSAL, over), REBOOK, DEMO_LEDGER_CONTEXT) is (
            NonCalculable.AMOUNT_OUTSIDE_MONEY_CONTRACT
        )


def test_a_value_within_the_contract_passes_whatever_its_representation() -> None:
    """Value-based, not representation-based: ``120.450000`` is exactly ``120.45`` and four places
    hold it, so it is accepted unchanged. The scale-based reading would reject a number the column
    stores perfectly — the mistake ADR-020 records having made once already."""
    result = compute_adjustment(facts(CB_REVERSAL, "120.450000"), REBOOK, DEMO_LEDGER_CONTEXT)
    assert isinstance(result, AdjustmentInstruction)
    assert result.amount == decimal.Decimal("120.45")


def test_every_result_records_its_quantisation_and_rounding_mode() -> None:
    """§7 requires both alongside the result. They are recorded and, today, never applied — no
    supported formula can produce a value needing them, which the refusal tests above are the other
    half of."""
    result = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, DEMO_LEDGER_CONTEXT)
    assert isinstance(result, AdjustmentInstruction)
    assert result.quantum == MONEY_QUANTUM == decimal.Decimal("0.0001")
    assert result.rounding == ROUNDING == decimal.ROUND_HALF_UP


def test_no_supported_calculation_ever_needs_to_round() -> None:
    """The claim that rounding is declared but unused, checked rather than asserted in prose.

    Every priced amount equals its input exactly, so quantising it would be a no-op. If a future
    formula changes that, this test is where the change surfaces.
    """
    for classification, treatment, amount, *_ in SUPPORTED:
        result = compute_adjustment(facts(classification, amount), treatment, DEMO_LEDGER_CONTEXT)
        assert isinstance(result, AdjustmentInstruction)
        assert result.amount == result.amount.quantize(MONEY_QUANTUM, rounding=ROUNDING)


# ======================================================================================
# Determinism
# ======================================================================================


def test_repeating_a_calculation_gives_a_field_identical_result() -> None:
    """Same inputs, same output, every time. Nothing here reads a clock, a counter or a random
    source, and this is what would catch one appearing."""
    first = compute_adjustment(facts(REFUND, "-540.86"), ACCRUE, DEMO_LEDGER_CONTEXT)
    for _ in range(20):
        assert compute_adjustment(facts(REFUND, "-540.86"), ACCRUE, DEMO_LEDGER_CONTEXT) == first


def test_the_exception_identifier_takes_part_in_no_arithmetic() -> None:
    """It is provenance. Two exceptions with the same facts price identically, and only the
    identifier on the result differs."""
    one = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, DEMO_LEDGER_CONTEXT)
    other = compute_adjustment(
        facts(CB_REVERSAL, "326.92", exception_id=uuid.uuid4()), REBOOK, DEMO_LEDGER_CONTEXT
    )
    assert isinstance(one, AdjustmentInstruction)
    assert isinstance(other, AdjustmentInstruction)
    assert one.exception_id != other.exception_id
    assert (one.amount, one.account_code, one.period, one.currency) == (
        other.amount,
        other.account_code,
        other.period,
        other.currency,
    )


def test_the_ledger_context_version_is_carried_onto_the_result() -> None:
    """§12.1 binds it into the instruction payload hash, so a configuration change between a first
    attempt and a re-send must yield a different operation identifier. Deriving that identifier is
    M4's; making it derivable is this increment's."""
    other = dataclasses.replace(DEMO_LEDGER_CONTEXT, version="demo-2026-07")
    a = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, DEMO_LEDGER_CONTEXT)
    b = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, other)
    assert isinstance(a, AdjustmentInstruction)
    assert isinstance(b, AdjustmentInstruction)
    assert a.ledger_context_version != b.ledger_context_version
    assert a.amount == b.amount


def test_a_refusal_reports_one_reason_and_the_same_one_every_time() -> None:
    """A case failing several checks reports whichever the fixed order reaches first, consistently.

    A reason that varied with evaluation order would be a poor thing to route an operator by, so
    the order is a decision: escalate, then whether the combination is priceable at all, then
    the values, then the period.
    """
    hopeless = facts(UNCLASSIFIED, "0.00", currency="JPY", originating_period=None)
    assert compute_adjustment(hopeless, ESCALATE, DEMO_LEDGER_CONTEXT) is (
        NonCalculable.TREATMENT_IS_ESCALATE
    )
    for _ in range(5):
        assert compute_adjustment(hopeless, ACCRUE, DEMO_LEDGER_CONTEXT) is (
            NonCalculable.NO_ACCOUNT_MAPPED
        )

    # A mapped combination falls through to the values, so the account check never masks a real
    # blocker — it only answers first when the answer is "this combination is not priceable".
    mapped_but_foreign = facts(CB_REVERSAL, "0.00", currency="JPY")
    assert compute_adjustment(mapped_but_foreign, REBOOK, DEMO_LEDGER_CONTEXT) is (
        NonCalculable.CURRENCY_NOT_FUNCTIONAL
    )


def test_the_result_types_are_immutable() -> None:
    """A priced instruction is a record of a decision. Nothing downstream may edit the amount it
    found there and present it as the same calculation."""
    result = compute_adjustment(facts(CB_REVERSAL, "326.92"), REBOOK, DEMO_LEDGER_CONTEXT)
    assert isinstance(result, AdjustmentInstruction)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.amount = decimal.Decimal("1")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEMO_LEDGER_CONTEXT.functional_currency = "USD"  # type: ignore[misc]


# ======================================================================================
# The firewall
# ======================================================================================


def _money_sources() -> list[tuple[str, ast.Module]]:
    paths = sorted(MONEY_ROOT.rglob("*.py"))
    assert len(paths) >= 3, "the guards must be walking real files"
    return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def _referenced_names(tree: ast.Module) -> list[str]:
    """Bare names, attributes and import aliases — the three ways a module reaches an identifier."""
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


def test_the_money_package_contains_no_float() -> None:
    """Every value is ``Decimal``. ``float`` would make ``0.1 + 0.2`` a financial instruction."""
    for name, tree in _money_sources():
        for referenced in _referenced_names(tree):
            assert referenced != "float", f"{name} references float"


def test_the_calculator_cannot_be_handed_free_text() -> None:
    """The firewall as a property of the signature rather than a promise about behaviour.

    Three parameters, and none of them can carry prose. Checked against the real signature and the
    real field names, so adding a ``rationale`` argument or a ``rationale`` field fails here before
    any behaviour changes.
    """
    parameters = list(inspect.signature(compute_adjustment).parameters)
    assert parameters == ["exception", "treatment", "ledger_ctx"], "§6.2 fixes this signature"

    forbidden = {
        "rationale",
        "confidence",
        "prompt",
        "completion",
        "response",
        "recommendation",
        "narrative",
        "memo",
        "description",
        "notes",
        "comment",
        "text",
        "evidence",
    }
    for record in (ExceptionFacts, AdjustmentInstruction, LedgerContext, AccountPolicy):
        fields = set(record.__dataclass_fields__)
        assert not fields & forbidden, f"{record.__name__} carries free text: {fields & forbidden}"

    assert set(ExceptionFacts.__dataclass_fields__) == {
        "exception_id",
        "classification",
        "amount",
        "currency",
        "value_date",
        "originating_period",
    }


def test_the_money_package_never_mentions_a_model_or_a_proposal() -> None:
    """M3 does not exist, and this package is built so it cannot depend on one when it does
    (ADR-003). The intended direction is treatment decision → calculator, never the reverse, and
    absolutely never rationale text → amount."""
    forbidden = {
        "TreatmentProposal",
        "ConfidenceBand",
        "rationale",
        "confidence",
        "abstained",
        "prompt_hash",
        "model_id",
        "model_version",
        "cassette_id",
        "llm",
        "openai",
        "anthropic",
        "provider",
        "completion",
    }
    for name, tree in _money_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in forbidden, f"{name} references {referenced}"


def test_the_money_package_reads_no_clock_and_no_random_source() -> None:
    """Period assignment comes from business dates the settlement file stated. A calculation that
    changed with the day it was re-run would not be a calculation."""
    for name, tree in _money_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"now", "utcnow", "today", "time", "monotonic"}, (
                    f"{name} reads a clock via .{node.attr}"
                )
            if isinstance(node, ast.Name):
                assert node.id not in {"random", "uuid4", "randint", "choice", "shuffle"}, (
                    f"{name} references {node.id}"
                )


def test_the_money_package_performs_no_io() -> None:
    """The plan asks for a test asserting the module performs no I/O. An allowlist is the way to
    assert it: no database session, no HTTP client, no file access, no socket — and no import
    through which one could arrive."""
    permitted_stdlib = {
        "__future__",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "re",
        "uuid",
        "typing",
        "collections",
        # Added at M3.1 for ``MappingProxyType`` alone, which is what makes the account table a
        # snapshot rather than a live dictionary. The guard fired on it, correctly — every addition
        # to this set widens the money boundary and has to earn it. ``types`` is pure introspection
        # and carries no I/O of any kind.
        "types",
    }
    permitted_internal = {
        "ledger_exception_control_plane.money",
        "ledger_exception_control_plane.db.base",
        "ledger_exception_control_plane.db.control",
        "ledger_exception_control_plane.classification.taxonomy",
    }
    for name, tree in _money_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("ledger_exception_control_plane"):
                    assert any(module.startswith(p) for p in permitted_internal), (
                        f"{name} imports {module}, which is outside the money boundary"
                    )
                else:
                    assert module.split(".")[0] in permitted_stdlib, (
                        f"{name} imports {module}, which is not on the allowlist"
                    )


def test_the_money_package_imports_no_orm_and_no_engine() -> None:
    """It reads two modules from ``db`` — the money constants and the enums — and touches no mapper,
    no session and no engine. Arithmetic buried inside ORM code is not testable without a database,
    and the plan requires this to be testable without one."""
    forbidden = {
        "AsyncSession",
        "AsyncEngine",
        "create_engine",
        "select",
        "insert",
        "update",
        "delete",
        "SettlementLine",
        "ExceptionRecord",
        "Adjustment",
        "MatchResult",
        "sqlalchemy",
    }
    for name, tree in _money_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in forbidden, f"{name} references {referenced}"


def test_the_money_package_contains_no_posting_or_retry_machinery() -> None:
    """M4's boundary, asserted from this side. Deriving an operation identifier, writing an attempt
    record, dispatching an outbox row and handling an ``UNKNOWN`` outcome are all M4's; the presence
    of those columns in the schema is not a reason to reach for them here."""
    forbidden = {
        "operation_id",
        "instruction_payload_hash",
        "posting_attempt",
        "PostingAttempt",
        "Outbox",
        "OutboxRow",
        "DeadLetter",
        "RecoveryQueue",
        "PostingOutcome",
        "post",
        "dispatch",
        "retry",
        "replay",
    }
    for name, tree in _money_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in forbidden, f"{name} references {referenced}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in forbidden, f"{name} contains the literal {node.value!r}"


def test_the_money_package_cannot_reach_the_fixture_corpus() -> None:
    """Tests may grade a calculation against construction intent. The calculator may not read it, or
    every evaluation number in the project would be circular."""
    for name, tree in _money_sources():
        dumped = ast.dump(tree)
        for label in ("fixtures", "intended_classification", "scenario_id", "catalogue"):
            assert label not in dumped, f"{name} references {label}"


@pytest.mark.parametrize(
    ("guard", "violation"),
    [
        (test_the_money_package_contains_no_float, "rate = float(amount)"),
        (
            test_the_money_package_never_mentions_a_model_or_a_proposal,
            "from ledger_exception_control_plane.llm.schema import TreatmentProposal",
        ),
        (test_the_money_package_reads_no_clock_and_no_random_source, "when = dt.date.today()"),
        (
            test_the_money_package_performs_no_io,
            "from sqlalchemy.ext.asyncio import AsyncSession",
        ),
        (
            test_the_money_package_imports_no_orm_and_no_engine,
            "from ledger_exception_control_plane.db.control import Adjustment",
        ),
        (
            test_the_money_package_contains_no_posting_or_retry_machinery,
            "operation_id = derive(instruction)",
        ),
        (
            test_the_money_package_cannot_reach_the_fixture_corpus,
            "from ledger_exception_control_plane.fixtures.catalogue import ACCOUNT_REVENUE",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_each_guard_rejects_the_violation_it_exists_for(
    guard: object, violation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation tests the plan's kill-test requirement asks for.

    Each guard is re-run against a source tree carrying **one** violation — its own — so a guard
    that passed by coincidence while a different one did the rejecting is caught. Injected into the
    *parsed* sources rather than onto disk: a crashed test must not be able to leave a mutation
    behind in the money path, which is exactly what an M2.3 verifier did once.
    """
    monkeypatch.setattr(
        "tests.test_money._money_sources",
        lambda: [("injected.py", ast.parse(violation))],
    )
    with pytest.raises(AssertionError):
        guard()  # type: ignore[operator]


def test_the_guards_all_pass_on_a_clean_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the proof above: a guard that raised unconditionally would sail through the
    parametrised test and look like it was working."""
    monkeypatch.setattr(
        "tests.test_money._money_sources", lambda: [("injected.py", ast.parse("x = 1"))]
    )
    test_the_money_package_contains_no_float()
    test_the_money_package_never_mentions_a_model_or_a_proposal()
    test_the_money_package_reads_no_clock_and_no_random_source()
    test_the_money_package_performs_no_io()
    test_the_money_package_imports_no_orm_and_no_engine()
    test_the_money_package_contains_no_posting_or_retry_machinery()
    test_the_money_package_cannot_reach_the_fixture_corpus()


def test_no_model_layer_exists_anywhere_in_the_source_tree() -> None:
    """ADR-003: the calculator is written with no model in the codebase, so it cannot accidentally
    depend on one. The property that matters is the repository's build order, so it is asserted
    across the whole package rather than over one directory's imports.

    ``db.control`` declares a ``treatment_proposal`` *table*, and that is not a model layer — M1.2
    built somewhere for M3 to write. What must not exist is anything that calls a provider: no
    client, no SDK. Checked as imports rather than as a substring scan, which caught the schema and
    was measuring the wrong thing.

    The ``not (root / "llm").exists()`` half was correct at M2.4 and expired at M3.2, which built
    exactly that package. What survives is the half that was always the real claim and is stronger
    for having outlived the increment it was written in: **no provider SDK is imported anywhere**,
    so the calculator still cannot reach a model even now that one has a port. The adapters speak
    wire-level JSON and take their transport by injection.
    """
    root = MONEY_ROOT.parent

    providers = {"openai", "anthropic", "litellm", "instructor", "langchain", "cohere"}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert module.split(".")[0] not in providers, (
                    f"{path.name} imports the model provider {module}"
                )
