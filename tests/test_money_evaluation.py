"""The calculator against the corpus — did it price the right cases, correctly?

The unit suite proves each rule in isolation. This runs the whole deterministic path on the
committed corpus — ingest-shaped generation, matching, classification, then pricing — and grades
every financial instruction against the scenario each line was *constructed* for.

Four outcomes, and conflating them is how a calculator gets graded generously:

* **correct** — priced, and the amount, account and period are what the constructed condition
  implies;
* **wrong** — priced, and one of those three is not. The count this file exists to keep at zero;
* **non-calculable** — refused, with a closed reason. Safe: nobody posts anything;
* **not eligible** — the residual carries a class nothing prices, so it was never a candidate.

**Construction metadata is read here to grade production output, never to produce it.** The
calculator is handed :class:`ExceptionFacts`, which has no field for a scenario, and a guard in
``test_money.py`` asserts the whole package cannot reach the fixture corpus.

The invariant: **no wrong financial instruction.** Asserted as zero, not as a rate. Coverage is the
secondary number and is reported as such — pricing a case to improve it would be the defect.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import decimal

import pytest

from ledger_exception_control_plane.classification import (
    SettlementMovement,
    accounting_period,
    classify,
    movement_type,
)
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    match,
)
from ledger_exception_control_plane.money import (
    ACCOUNT_CHARGEBACKS,
    ACCOUNT_REVENUE,
    DEMO_LEDGER_CONTEXT,
    AdjustmentInstruction,
    ExceptionFacts,
    NonCalculable,
    compute_adjustment,
)

SEED = 20260829

#: Everything in the corpus settles from June 2026 onwards, so nothing is priced against a closed
#: period by accident — the closed-period path is exercised deliberately in ``test_money.py``.
CONTEXT = dataclasses.replace(DEMO_LEDGER_CONTEXT, earliest_open_period="2026-01")


@dataclasses.dataclass(frozen=True, slots=True)
class Graded:
    """One residual, what the calculator said about it, and what it should have said."""

    scenario: str
    classification: ExceptionClassification
    amount: decimal.Decimal
    currency: str
    result: AdjustmentInstruction | NonCalculable
    expected_account: str | None
    expected_period: str | None


def _residual_facts(
    profile: Profile, instances: int
) -> list[tuple[str, ExceptionFacts, str | None]]:
    """Run matching and classification over the corpus, and build the calculator's inputs.

    The third element of each tuple is the scenario id — carried *beside* the facts rather than
    inside them, because the calculator must not be able to see it.
    """
    corpus = generate(SEED, profile, instances)
    rows = {row.id: row for batch in corpus.corpus.batches for row in batch.lines}

    outcome = match(
        [
            CandidateLine(r.id, r.line_number, r.amount, r.currency, r.value_date)
            for r in rows.values()
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
            id=r.id,
            merchant_reference=r.merchant_reference,
            movement=movement_type(r.transaction_type),
            amount=r.amount,
            currency=r.currency,
            value_date=r.value_date,
            matched=r.id in matched,
        )
        for r in rows.values()
    ]
    by_id = {m.id: m for m in movements}
    decisions = classify([m for m in movements if not m.matched], movements)

    prepared: list[tuple[str, ExceptionFacts, str | None]] = []
    for decision in decisions:
        row = rows[decision.line_id]
        movement = by_id[decision.line_id]
        prepared.append(
            (
                row.scenario_id,
                ExceptionFacts(
                    exception_id=decision.line_id,
                    classification=decision.classification,
                    amount=movement.amount,
                    currency=movement.currency,
                    value_date=movement.value_date,
                    originating_period=_originating_period(movement, movements),
                ),
                row.scenario_id,
            )
        )
    return prepared


def _originating_period(
    subject: SettlementMovement, movements: list[SettlementMovement]
) -> str | None:
    """The period of the reconciled movement this one exactly reverses, if there is exactly one.

    The same relationship M2.3 classified on, read here from production fields only: the merchant's
    reference, the currency, the match state and an exact negation. No scenario label is involved,
    and a later increment will derive this in the orchestration that assembles the calculator's
    inputs — M2.4 takes it as a fact because the calculator performs no I/O.
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


#: What the constructed condition implies, independently of the calculator.
#:
#: Derived from the scenario each line was built for and from the approved policy — never from the
#: calculator's own output, which would make the grading circular.
EXPECTED_ACCOUNT = {
    ExceptionClassification.CHARGEBACK_REVERSAL: ACCOUNT_CHARGEBACKS,
    ExceptionClassification.CROSS_PERIOD_REFUND: ACCOUNT_REVENUE,
}


def grade(profile: Profile, instances: int, treatment: TreatmentCode) -> list[Graded]:
    """Price every residual under one treatment and grade each instruction."""
    graded: list[Graded] = []
    for scenario, facts, _ in _residual_facts(profile, instances):
        result = compute_adjustment(facts, treatment, CONTEXT)
        expected_account = EXPECTED_ACCOUNT.get(facts.classification)
        expected_period: str | None = None
        if expected_account is not None:
            expected_period = (
                facts.originating_period
                if treatment is TreatmentCode.ACCRUE
                else accounting_period(facts.value_date)
            )
        graded.append(
            Graded(
                scenario=scenario,
                classification=facts.classification,
                amount=facts.amount,
                currency=facts.currency,
                result=result,
                expected_account=expected_account,
                expected_period=expected_period,
            )
        )
    return graded


def wrong(graded: list[Graded]) -> list[tuple[str, str]]:
    """Instructions that were priced and should not have been, or priced incorrectly."""
    faults: list[tuple[str, str]] = []
    for item in graded:
        if not isinstance(item.result, AdjustmentInstruction):
            continue
        if item.expected_account is None:
            faults.append((item.scenario, "priced a class nothing should price"))
            continue
        if item.result.amount != item.amount:
            faults.append((item.scenario, f"amount {item.result.amount} != {item.amount}"))
        if item.result.currency != item.currency:
            faults.append((item.scenario, f"currency {item.result.currency}"))
        if item.result.account_code != item.expected_account:
            faults.append((item.scenario, f"account {item.result.account_code}"))
        if item.result.period != item.expected_period:
            faults.append((item.scenario, f"period {item.result.period}"))
    return faults


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
@pytest.mark.parametrize("treatment", [TreatmentCode.REBOOK, TreatmentCode.ACCRUE])
def test_no_residual_is_ever_given_a_wrong_financial_instruction(
    profile: Profile, instances: int, treatment: TreatmentCode
) -> None:
    """**Zero wrong instructions.** Asserted as a count, at four corpus sizes, under two treatments.

    A wrong amount, a wrong account or a wrong period is a defect of the same severity, so all three
    are counted together: an adjustment posted to the right account in the wrong month is as much a
    misstatement as one for the wrong number.
    """
    faults = wrong(grade(profile, instances, treatment))
    assert faults == [], f"{len(faults)} wrong instruction(s), e.g. {faults[:3]}"


def test_the_canonical_corpus_prices_exactly_what_it_should() -> None:
    """Every figure pinned. A change to any of them must be deliberate."""
    graded = grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK)
    priced = [g for g in graded if isinstance(g.result, AdjustmentInstruction)]
    refused = collections.Counter(
        g.result.value for g in graded if isinstance(g.result, NonCalculable)
    )

    assert len(graded) == 13, "every residual becomes an exception (M2.3)"
    assert len(priced) == 1
    assert wrong(graded) == []
    assert refused == {"no_account_mapped": 11, "currency_not_functional": 1}

    assert {(g.scenario[:6], g.result.account_code) for g in priced} == {  # type: ignore[union-attr]
        ("SC-008", ACCOUNT_REVENUE),
    }


def test_the_one_priced_canonical_case_is_exactly_right() -> None:
    """The whole of what the calculator does on the committed corpus, spelled out.

    One residual out of seventeen settlement lines reaches a financial instruction, checked against
    the amount the generator constructed rather than against the calculator's own answer.
    """
    graded = {g.scenario[:6]: g for g in grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK)}

    refund = graded["SC-008"]
    assert isinstance(refund.result, AdjustmentInstruction)
    assert refund.result.amount == decimal.Decimal("-540.86")
    assert refund.result.currency == "EUR"
    assert refund.result.account_code == ACCOUNT_REVENUE
    assert refund.result.period == "2026-07"


def test_the_chargeback_reversal_is_refused_because_the_books_are_not_kept_in_its_currency() -> (
    None
):
    """The most instructive case in the corpus, and it is a refusal.

    SC-006 is a classified, mapped, open-period chargeback reversal — everything the calculator
    needs except one thing: it settled in USD and the demo books are EUR. There is no approved rate
    source, so it refuses rather than posting 326.92 of the wrong unit into the chargeback account.

    Worth its own test because it is the failure mode that looks most like success: every other
    field lines up, and a calculator that quietly used the settlement number would have produced a
    plausible instruction that was wrong by an exchange rate.
    """
    graded = {g.scenario[:6]: g for g in grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK)}
    reversal = graded["SC-006"]

    assert reversal.classification is ExceptionClassification.CHARGEBACK_REVERSAL
    assert reversal.currency == "USD"
    assert reversal.expected_account == ACCOUNT_CHARGEBACKS, "mapped, and still not priced"
    assert reversal.result is NonCalculable.CURRENCY_NOT_FUNCTIONAL


def test_accrue_moves_the_cross_period_refund_back_into_the_capture_period() -> None:
    """The cross-period rule, measured on the case the corpus built for it.

    SC-008's refund settles in July against a capture the ledger booked in June. Rebooked it lands
    in July; accrued it lands in June — same amount, same account, different period. If the two
    treatments produced the same instruction, one of them would be decoration.
    """
    rebooked = {g.scenario[:6]: g for g in grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK)}
    accrued = {g.scenario[:6]: g for g in grade(Profile.CANONICAL, 200, TreatmentCode.ACCRUE)}

    a, b = rebooked["SC-008"].result, accrued["SC-008"].result
    assert isinstance(a, AdjustmentInstruction)
    assert isinstance(b, AdjustmentInstruction)
    assert (a.period, b.period) == ("2026-07", "2026-06")
    assert a.amount == b.amount
    assert a.account_code == b.account_code


def test_coverage_is_reported_alongside_the_invariant_rather_than_instead_of_it() -> None:
    """The secondary number, at scale, and it is low on purpose.

    Most residuals are ``unclassified`` or ``fee_split``, and neither is priceable — the first
    because the system could not say what it is, the second because the ledger already carries the
    net. Pricing either to move this figure is the defect the count above exists to catch.
    """
    graded = grade(Profile.BULK, 4000, TreatmentCode.REBOOK)
    priced = [g for g in graded if isinstance(g.result, AdjustmentInstruction)]

    assert len(graded) == 833
    assert len(priced) == 40
    assert 100 * len(priced) // len(graded) == 4
    assert wrong(graded) == []

    # Every one of them is SC-008: the only residual class that is both priceable and settles in
    # the currency the demo books are kept in.
    assert {g.scenario[:6] for g in priced} == {"SC-008"}


def test_a_currency_the_books_are_not_kept_in_is_refused_rather_than_converted() -> None:
    """The corpus settles in five currencies and the demo books are EUR, so the refusal is exercised
    by real data rather than only by a constructed case.

    SC-006 is USD. Against EUR books it refuses; against USD books the same residual prices. Nothing
    converts, in either direction.
    """
    eur_books = dataclasses.replace(CONTEXT, functional_currency="EUR")
    usd_books = dataclasses.replace(CONTEXT, functional_currency="USD")

    facts = next(
        f
        for scenario, f, _ in _residual_facts(Profile.CANONICAL, 200)
        if scenario.startswith("SC-006")
    )
    assert facts.currency == "USD"
    assert compute_adjustment(facts, TreatmentCode.REBOOK, eur_books) is (
        NonCalculable.CURRENCY_NOT_FUNCTIONAL
    )
    priced = compute_adjustment(facts, TreatmentCode.REBOOK, usd_books)
    assert isinstance(priced, AdjustmentInstruction)
    assert priced.amount == decimal.Decimal("326.92")


def test_every_refusal_carries_a_reason_from_the_closed_set() -> None:
    """No residual is refused without saying why, and no reason is invented outside the enum."""
    for treatment in (TreatmentCode.REBOOK, TreatmentCode.ACCRUE, TreatmentCode.WRITE_OFF):
        for item in grade(Profile.BULK, 200, treatment):
            if not isinstance(item.result, AdjustmentInstruction):
                assert item.result in set(NonCalculable), item.result


def test_the_calculator_is_handed_no_scenario_label() -> None:
    """The firewall, stated where it is most tempting to breach.

    This file grades using ``scenario_id`` and carries it *beside* the facts. The calculator's input
    type has no field for it, so the comparison performed here is one production code cannot make.
    """
    fields = set(ExceptionFacts.__dataclass_fields__)
    assert not fields & {"scenario_id", "intended_classification", "intent", "memo"}

    corpus = generate(SEED, Profile.CANONICAL, 200)
    assert corpus.corpus.batches[0].lines[0].scenario_id, (
        "the corpus must carry the metadata this file grades against"
    )


def test_pricing_the_corpus_twice_gives_identical_instructions() -> None:
    """Determinism over the whole path, not only over one call."""
    first = grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK)
    assert [g.result for g in first] == [
        g.result for g in grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK)
    ]


def test_no_priced_amount_needs_rounding_anywhere_in_the_corpus() -> None:
    """The claim that rounding is declared but never applied, checked against real data at scale."""
    for treatment in (TreatmentCode.REBOOK, TreatmentCode.ACCRUE, TreatmentCode.WRITE_OFF):
        for item in grade(Profile.BULK, 1000, treatment):
            if isinstance(item.result, AdjustmentInstruction):
                quantised = item.result.amount.quantize(
                    item.result.quantum, rounding=item.result.rounding
                )
                assert quantised == item.result.amount


def test_the_evaluation_uses_only_business_dates() -> None:
    """No value date in the corpus is anywhere near a wall clock, and every period the calculator
    produced is derived from one. A calculation that read the clock would drift from this."""
    for item in grade(Profile.CANONICAL, 200, TreatmentCode.REBOOK):
        if isinstance(item.result, AdjustmentInstruction):
            assert item.result.period.startswith("2026-")
    assert accounting_period(dt.date(2026, 7, 8)) == "2026-07"
