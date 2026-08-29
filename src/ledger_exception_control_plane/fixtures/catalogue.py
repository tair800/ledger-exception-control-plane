"""The scenario catalogue — every condition the corpus deliberately contains.

Twelve scenarios, each answering the three questions the plan requires: what condition it
represents, which later increment needs it, and what makes it different from the scenario
beside it. None is speculative padding; a thirteenth would have to earn its place by naming a
test that needs it.

**These builders construct inputs. They do not decide outcomes.** A fee-split scenario is a
fee split because this module *built* it as one — a capture line plus separate fee lines
against a single combined ledger entry — not because anything ran a matcher over it. That
distinction is the whole reason the corpus can be used to judge M2's matcher later: an oracle
derived from the system under test would be worthless.

The FX rate is a **recorded string**, never a computed number. §3 lists a currency-conversion
policy engine as a non-goal and says rates arrive as recorded inputs, so the file carries the
rate the PSP stated. It is deliberately not a monetary column: ``money_column`` carries a
four-decimal ceiling and assumes a paired currency, and a rate has neither property.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
from collections.abc import Callable
from typing import Final

from ledger_exception_control_plane.db.control import ExceptionClassification
from ledger_exception_control_plane.fixtures.determinism import Draw, at, on
from ledger_exception_control_plane.fixtures.money import BHD, EUR, GBP, JPY, USD, Currency, money
from ledger_exception_control_plane.fixtures.schema import (
    Awkwardness,
    MatchIntent,
    ScenarioKind,
)

#: Fictional chart of accounts. Four codes, enough to make account selection a real decision
#: for M2.4 without inventing a chart nobody asked for.
ACCOUNT_SETTLEMENT_CLEARING: Final = "2100"
ACCOUNT_REVENUE: Final = "4100"
ACCOUNT_CHARGEBACKS: Final = "4900"
ACCOUNT_PSP_FEES: Final = "6200"


@dataclasses.dataclass(frozen=True, slots=True)
class BuiltLine:
    """One row of the settlement file, before it is either rendered or materialised."""

    psp_reference: str
    merchant_reference: str | None
    transaction_type: str
    amount: decimal.Decimal
    currency: Currency
    value_date: dt.date
    presentment_amount: decimal.Decimal | None
    presentment_currency: Currency | None
    fx_rate: str | None
    memo: str


@dataclasses.dataclass(frozen=True, slots=True)
class BuiltEntry:
    """One row of the ledger snapshot."""

    external_ref: str
    account_code: str
    amount: decimal.Decimal
    currency: Currency
    booked_at: dt.datetime
    description: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class BuiltScenario:
    scenario_id: str
    kind: ScenarioKind
    intent: MatchIntent
    intended_classification: ExceptionClassification | None
    awkwardness: tuple[Awkwardness, ...]
    why_it_exists: str
    distinguishing_fields: tuple[str, ...]
    lines: tuple[BuiltLine, ...]
    entries: tuple[BuiltEntry, ...]


def _psp_reference(draw: Draw, label: str) -> str:
    return f"psp_{draw.integer(label, 0x1000_0000_0000, 0xFFFF_FFFF_FFFF):012x}"


def _merchant_reference(draw: Draw, label: str) -> str:
    return f"ORD-2026-{draw.integer(label, 100_000, 999_999)}"


def _ledger_reference(draw: Draw, label: str) -> str:
    return f"GL-{draw.integer(label, 10_000_000, 99_999_999)}"


Builder = Callable[[Draw, str], BuiltScenario]


# ======================================================================================
# Matched-intent scenarios — the bulk a deterministic matcher must clear without a model
# ======================================================================================


def _exact_match(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    amount = money(draw.integer("amount", 1_500, 400_000), EUR)
    return BuiltScenario(
        scenario_id="SC-001-exact-match",
        kind=ScenarioKind.EXACT_MATCH,
        intent=MatchIntent.MATCHED,
        intended_classification=None,
        awkwardness=(),
        why_it_exists=(
            "The ordinary case. M2.2 must clear it with no tolerance and no model call, and "
            "M2.2's 'share cleared deterministically' measurement is meaningless without a "
            "population of these."
        ),
        distinguishing_fields=("amount", "currency", "value_date", "merchant_reference"),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=amount,
                currency=EUR,
                value_date=on(2),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"capture for order {merchant}",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=amount,
                currency=EUR,
                booked_at=at(days=2, hours=3),
                description=f"card capture {merchant}",
            ),
        ),
    )


def _reference_mismatch(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    amount = money(draw.integer("amount", 2_000, 300_000), USD)
    return BuiltScenario(
        scenario_id="SC-002-reference-mismatch",
        kind=ScenarioKind.REFERENCE_MISMATCH,
        intent=MatchIntent.MATCHED,
        intended_classification=None,
        awkwardness=(Awkwardness.UNINFORMATIVE_MEMO,),
        why_it_exists=(
            "The ledger description carries no usable reference, so this line can only be "
            "matched on amount, currency and date. It exists so M2.2 cannot pass by comparing "
            "reference strings, which would look correct against a tidier corpus."
        ),
        distinguishing_fields=("ledger description carries no reference",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=amount,
                currency=USD,
                value_date=on(3),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=amount,
                currency=USD,
                booked_at=at(days=3, hours=1),
                description="daily card settlement",
            ),
        ),
    )


def _near_amount_difference(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    units = draw.integer("amount", 5_000, 250_000)
    drift = draw.integer("drift", 1, 3)
    return BuiltScenario(
        scenario_id="SC-003-near-amount-difference",
        kind=ScenarioKind.NEAR_AMOUNT_DIFFERENCE,
        intent=MatchIntent.TOLERANCE_POLICY_DEPENDENT,
        intended_classification=None,
        awkwardness=(),
        why_it_exists=(
            "Differs from the ledger by one to three minor units. Whether it clears depends "
            "on the tolerance bands OPEN-2 has not settled, so the corpus records the "
            "difference and declines to predict the outcome. M2.2's boundary tests need it, "
            "and OPEN-2 cannot be decided responsibly without cases like it."
        ),
        distinguishing_fields=("settlement amount differs from the ledger by 1-3 minor units",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=money(units, GBP),
                currency=GBP,
                value_date=on(4),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"capture {merchant}",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=money(units - drift, GBP),
                currency=GBP,
                booked_at=at(days=4, hours=2),
                description=f"card capture {merchant}",
            ),
        ),
    )


# ======================================================================================
# Residual scenarios — one per class of FR-4's closed taxonomy
# ======================================================================================


def _partial_capture(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    authorised = draw.integer("authorised", 20_000, 400_000)
    captured = authorised - draw.integer("shortfall", 1_000, 15_000)
    return BuiltScenario(
        scenario_id="SC-004-partial-capture",
        kind=ScenarioKind.PARTIAL_CAPTURE,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.PARTIAL_CAPTURE,
        awkwardness=(),
        why_it_exists=(
            "The merchant captured less than the ledger accrued, so the difference is "
            "material rather than a rounding artefact. M2.3 must classify it and M2.4 must "
            "price the shortfall deterministically."
        ),
        distinguishing_fields=("settlement amount materially below the ledger entry",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=money(captured, EUR),
                currency=EUR,
                value_date=on(5),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"partial capture for {merchant}",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=money(authorised, EUR),
                currency=EUR,
                booked_at=at(days=5, hours=1),
                description=f"authorised amount {merchant}",
            ),
        ),
    )


def _fee_split(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    gross = draw.integer("gross", 50_000, 400_000)
    scheme_fee = draw.integer("scheme_fee", 40, 900)
    processing_fee = draw.integer("processing_fee", 60, 1_400)
    return BuiltScenario(
        scenario_id="SC-005-fee-split",
        kind=ScenarioKind.FEE_SPLIT,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.FEE_SPLIT,
        awkwardness=(Awkwardness.SPLIT_ACROSS_ROWS,),
        why_it_exists=(
            "The PSP reports the capture and each fee on its own row while the ledger booked "
            "one net entry, so no single settlement line equals any single ledger entry. This "
            "is the case that breaks a one-to-one matcher, and it is why match_result carries "
            "a unique constraint on settlement_line_id rather than allowing fan-out."
        ),
        distinguishing_fields=(
            "three settlement rows share one psp_reference stem",
            "no single row equals the ledger entry",
        ),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=money(gross, EUR),
                currency=EUR,
                value_date=on(6),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"gross capture {merchant}",
            ),
            BuiltLine(
                psp_reference=f"{reference}-fee1",
                merchant_reference=merchant,
                transaction_type="fee",
                amount=money(-scheme_fee, EUR),
                currency=EUR,
                value_date=on(6),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="scheme fee",
            ),
            BuiltLine(
                psp_reference=f"{reference}-fee2",
                merchant_reference=merchant,
                transaction_type="fee",
                amount=money(-processing_fee, EUR),
                currency=EUR,
                value_date=on(6),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="processing fee",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=money(gross - scheme_fee - processing_fee, EUR),
                currency=EUR,
                booked_at=at(days=6, hours=2),
                description=f"net settlement {merchant}",
            ),
        ),
    )


def _chargeback_reversal(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    amount = draw.integer("amount", 3_000, 120_000)
    return BuiltScenario(
        scenario_id="SC-006-chargeback-reversal",
        kind=ScenarioKind.CHARGEBACK_REVERSAL,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.CHARGEBACK_REVERSAL,
        awkwardness=(),
        why_it_exists=(
            "A chargeback and its later reversal both appear in settlement while the ledger "
            "booked only the original chargeback. Negative amounts on both rows are why "
            "settlement_line imposes no sign constraint."
        ),
        distinguishing_fields=("two opposing signed rows", "ledger holds only the debit"),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="chargeback",
                amount=money(-amount, USD),
                currency=USD,
                value_date=on(7),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"chargeback raised for {merchant}",
            ),
            BuiltLine(
                psp_reference=f"{reference}-rev",
                merchant_reference=merchant,
                transaction_type="chargeback_reversal",
                amount=money(amount, USD),
                currency=USD,
                value_date=on(9),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="dispute resolved in merchant favour",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_CHARGEBACKS,
                amount=money(-amount, USD),
                currency=USD,
                booked_at=at(days=7, hours=4),
                description="chargeback debit",
            ),
        ),
    )


def _fx_rounding(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    presentment_units = draw.integer("presentment", 40_000, 900_000)
    presentment = money(presentment_units, JPY)
    # The rate the PSP recorded, exactly as stated. A string in the file; a Decimal here only
    # so the settled amount it implies can be constructed.
    rate = decimal.Decimal(f"0.00{draw.integer('rate', 610, 690)}")
    # Converting the recorded rate into a settlement amount is *fixture construction*, not a
    # conversion policy (§3 lists that as a non-goal and nothing in this system computes a
    # rate). The quantisation is explicit and audited, which base.py permits — what it forbids
    # is a silent one at the persistence boundary.
    #
    # Drawing the settled amount independently, as this scenario originally did, produced a
    # row whose own three numbers disagreed by two orders of magnitude: a corpus asserting
    # that JPY 683,880 at 0.00658 settled as EUR 40.77. An FX *rounding* scenario whose
    # arithmetic is wrong by 100x is not a rounding scenario.
    settled = (presentment * rate).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    # The ledger booked its own conversion and landed a minor unit or two away. That gap is
    # the whole point of the scenario.
    ledger_amount = settled + decimal.Decimal("0.01") * draw.integer("ledger_drift", 1, 3)
    return BuiltScenario(
        scenario_id="SC-007-fx-rounding",
        kind=ScenarioKind.FX_ROUNDING,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.FX_ROUNDING,
        awkwardness=(Awkwardness.FOREIGN_PRESENTMENT_CURRENCY,),
        why_it_exists=(
            "Presented in JPY and settled in EUR at a rate the PSP recorded, with the "
            "settlement amount consistent with that rate to the minor unit. The ledger booked "
            "its own conversion and landed one to three minor units away, so the two differ "
            "by a rounding artefact rather than by an economic amount. It is the case that "
            "forces every amount to carry an explicit currency, and the reason the FX rate is "
            "a recorded input (§3) rather than something this system computes."
        ),
        distinguishing_fields=(
            "presentment currency differs from settlement currency",
            "fx_rate is populated and consistent with the settled amount",
            "the ledger differs by one to three minor units",
        ),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=settled,
                currency=EUR,
                value_date=on(8),
                presentment_amount=presentment,
                presentment_currency=JPY,
                # Recorded exactly as the PSP stated it. A string, not a number: a rate is
                # not money and must not borrow money's four-decimal ceiling.
                fx_rate=str(rate),
                memo=f"cross-currency capture {merchant}",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=ledger_amount,
                currency=EUR,
                booked_at=at(days=8, hours=2),
                description=f"fx converted capture {merchant}",
            ),
        ),
    )


def _cross_period_refund(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    amount = draw.integer("amount", 4_000, 90_000)
    return BuiltScenario(
        scenario_id="SC-008-cross-period-refund",
        kind=ScenarioKind.CROSS_PERIOD_REFUND,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.CROSS_PERIOD_REFUND,
        awkwardness=(Awkwardness.CROSS_PERIOD_DATES,),
        why_it_exists=(
            "A refund settles in the month after the capture it reverses, so the two fall in "
            "different accounting periods. Named explicitly by the plan as an awkward case, "
            "and the reason adjustment carries a period column that OPEN-4 must give rules "
            "for. It is also what makes the corpus span more than one settlement batch."
        ),
        distinguishing_fields=("refund value_date falls in the month after the capture",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=money(amount, EUR),
                currency=EUR,
                value_date=on(25),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"capture {merchant}",
            ),
            BuiltLine(
                psp_reference=f"{reference}-rfnd",
                merchant_reference=merchant,
                transaction_type="refund",
                amount=money(-amount, EUR),
                currency=EUR,
                value_date=on(37),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="customer returned order",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=money(amount, EUR),
                currency=EUR,
                booked_at=at(days=25, hours=3),
                description=f"capture {merchant}",
            ),
        ),
    )


def _unclassified(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    return BuiltScenario(
        scenario_id="SC-009-unclassified",
        kind=ScenarioKind.UNCLASSIFIED,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.UNCLASSIFIED,
        awkwardness=(Awkwardness.NO_LEDGER_COUNTERPART, Awkwardness.UNINFORMATIVE_MEMO),
        why_it_exists=(
            "A movement with no ledger counterpart, no merchant reference and a memo that "
            "explains nothing. The taxonomy needs a genuine bottom case, and FR-4's "
            "'unclassified' is only honest if something actually lands there. It is also the "
            "case where the model should abstain rather than guess (M3.3)."
        ),
        distinguishing_fields=("no ledger entry exists at all",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=None,
                transaction_type="adjustment",
                amount=money(draw.integer("amount", 100, 40_000), BHD),
                currency=BHD,
                value_date=on(10),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="misc",
            ),
        ),
        entries=(),
    )


# ======================================================================================
# Awkwardness the plan names explicitly
# ======================================================================================


def _missing_merchant_reference(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    amount = money(draw.integer("amount", 1_000, 200_000), EUR)
    return BuiltScenario(
        scenario_id="SC-010-missing-merchant-reference",
        kind=ScenarioKind.MISSING_MERCHANT_REFERENCE,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.UNCLASSIFIED,
        awkwardness=(Awkwardness.MISSING_MERCHANT_REFERENCE,),
        why_it_exists=(
            "The PSP passed no merchant reference through, and the ledger entry that would "
            "otherwise match is for a different amount. Named by the plan as an awkward case, "
            "and the reason settlement_line.merchant_reference is nullable."
        ),
        distinguishing_fields=("merchant_reference is empty in the file",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=None,
                transaction_type="capture",
                amount=amount,
                currency=EUR,
                value_date=on(11),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="capture",
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_SETTLEMENT_CLEARING,
                amount=amount + money(draw.integer("gap", 500, 4_000), EUR),
                currency=EUR,
                booked_at=at(days=11, hours=2),
                description="clearing account movement",
            ),
        ),
    )


def _ambiguous_memo(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    authorised = draw.integer("authorised", 30_000, 250_000)
    captured = authorised - draw.integer("shortfall", 900, 12_000)
    memo = draw.choice(
        "memo",
        (
            "adj per ops - see thread",
            "as discussed, partial",
            "corrected on request",
            "see ticket",
        ),
    )
    return BuiltScenario(
        scenario_id="SC-011-ambiguous-memo",
        kind=ScenarioKind.AMBIGUOUS_MEMO,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.PARTIAL_CAPTURE,
        awkwardness=(Awkwardness.AMBIGUOUS_MEMO,),
        why_it_exists=(
            "Structurally identical to SC-004, but the only free-text evidence is a memo that "
            "gestures at a conversation nobody recorded. Named by the plan as an awkward "
            "case. It is the pair that makes M3's evaluation meaningful: the same underlying "
            "condition with and without usable evidence, so a drop in proposal quality can be "
            "attributed to evidence quality rather than to the condition."
        ),
        distinguishing_fields=("same shape as SC-004; memo is unusable",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=money(captured, EUR),
                currency=EUR,
                value_date=on(12),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=memo,
            ),
        ),
        entries=(
            BuiltEntry(
                external_ref=_ledger_reference(draw, "gl") + suffix,
                account_code=ACCOUNT_REVENUE,
                amount=money(authorised, EUR),
                currency=EUR,
                booked_at=at(days=12, hours=1),
                description=f"authorised amount {merchant}",
            ),
        ),
    )


def _repeated_psp_reference(draw: Draw, suffix: str) -> BuiltScenario:
    reference = _psp_reference(draw, "psp") + suffix
    merchant = _merchant_reference(draw, "merchant")
    amount = money(draw.integer("amount", 2_000, 150_000), EUR)
    return BuiltScenario(
        scenario_id="SC-012-repeated-psp-reference",
        kind=ScenarioKind.REPEATED_PSP_REFERENCE,
        intent=MatchIntent.RESIDUAL,
        intended_classification=ExceptionClassification.UNCLASSIFIED,
        awkwardness=(Awkwardness.REPEATED_PSP_REFERENCE, Awkwardness.NO_LEDGER_COUNTERPART),
        why_it_exists=(
            "One psp_reference appears on two rows of the same file with different amounts. "
            "The schema permits it — settlement_line is unique on (batch, line_number), not "
            "on the PSP's reference — and that is correct, because the PSP's reference is "
            "their data and not a key this system can rely on. M2.1 and M2.2 both need a case "
            "that proves nothing here silently assumes reference uniqueness."
        ),
        distinguishing_fields=("two rows share one psp_reference with differing amounts",),
        lines=(
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=amount,
                currency=EUR,
                value_date=on(13),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo=f"capture {merchant}",
            ),
            BuiltLine(
                psp_reference=reference,
                merchant_reference=merchant,
                transaction_type="capture",
                amount=amount + money(draw.integer("delta", 100, 5_000), EUR),
                currency=EUR,
                value_date=on(13),
                presentment_amount=None,
                presentment_currency=None,
                fx_rate=None,
                memo="duplicate reference from psp export",
            ),
        ),
        entries=(),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """A builder plus the weight it carries in the ``bulk`` profile's declared mix."""

    scenario_id: str
    kind: ScenarioKind
    build: Builder
    weight: int


#: The catalogue, in a fixed order. Order is part of the determinism contract: it fixes the
#: sequence scenarios are built in, the remainder allocation in the bulk mix, and therefore
#: the bytes of the output.
#:
#: The weights are a **declared design parameter of the synthetic corpus**, not an empirical
#: claim about real settlement feeds — nobody here has measured a real one. They encode the
#: only property §1 actually asserts: deterministic matching clears the great majority, and a
#: modest residual remains. At the default bulk size of 200 instances the counts equal the
#: weights exactly.
CATALOGUE: Final[tuple[CatalogueEntry, ...]] = (
    CatalogueEntry("SC-001-exact-match", ScenarioKind.EXACT_MATCH, _exact_match, 147),
    CatalogueEntry(
        "SC-002-reference-mismatch", ScenarioKind.REFERENCE_MISMATCH, _reference_mismatch, 16
    ),
    CatalogueEntry(
        "SC-003-near-amount-difference",
        ScenarioKind.NEAR_AMOUNT_DIFFERENCE,
        _near_amount_difference,
        12,
    ),
    CatalogueEntry("SC-004-partial-capture", ScenarioKind.PARTIAL_CAPTURE, _partial_capture, 6),
    CatalogueEntry("SC-005-fee-split", ScenarioKind.FEE_SPLIT, _fee_split, 4),
    CatalogueEntry(
        "SC-006-chargeback-reversal", ScenarioKind.CHARGEBACK_REVERSAL, _chargeback_reversal, 4
    ),
    CatalogueEntry("SC-007-fx-rounding", ScenarioKind.FX_ROUNDING, _fx_rounding, 4),
    CatalogueEntry(
        "SC-008-cross-period-refund", ScenarioKind.CROSS_PERIOD_REFUND, _cross_period_refund, 2
    ),
    CatalogueEntry("SC-009-unclassified", ScenarioKind.UNCLASSIFIED, _unclassified, 2),
    CatalogueEntry(
        "SC-010-missing-merchant-reference",
        ScenarioKind.MISSING_MERCHANT_REFERENCE,
        _missing_merchant_reference,
        1,
    ),
    CatalogueEntry("SC-011-ambiguous-memo", ScenarioKind.AMBIGUOUS_MEMO, _ambiguous_memo, 1),
    CatalogueEntry(
        "SC-012-repeated-psp-reference",
        ScenarioKind.REPEATED_PSP_REFERENCE,
        _repeated_psp_reference,
        1,
    ),
)

TOTAL_WEIGHT: Final = sum(entry.weight for entry in CATALOGUE)
