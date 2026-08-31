"""The deterministic adjustment calculator — increment M2.4.

One pure function. Given an exception's structured facts, an approved treatment code and an explicit
ledger context, it produces the financial instruction those three imply, or says it cannot.

**No model exists in this codebase yet, and that is the point** (ADR-003). The containment argument
is not "the calculator does not read model output"; it is that when the calculator was written there
was no model output to read, and the guards below lock that in before M3 can arrive. Containment
claimed afterwards is weaker than containment that was never possible to violate.

**The parameters are the firewall.** §6.2 fixes the signature at three arguments, and every one
of them is a closed structured type: an exception's facts, a value from a closed enum, and
system-owned accounting configuration. There is no ``rationale``, no ``confidence``, no free text,
and no dict or JSON blob through which any of those could arrive. A hallucinated amount is not
unlikely here — it is unrepresentable, because the only value in this call a model will ever
influence is one member of a four-value enum, and that member selects an account and a period
rather than a number.

**Rounding is declared and never applied.** Every amount this module produces is a settlement
line's own amount, which the ingestion boundary already constrained to four decimal places
(ADR-020), so no supported formula can produce a value needing to be rounded. The quantum and the
rounding mode are still recorded on every result, because §7 requires them alongside the result
and because a future formula that does need rounding should inherit one declared rule rather than
choose its own. An amount that arrives outside the money contract is **refused, not rounded**:
inventing a rounding rule to make a number fit the schema is the defect ADR-020 prevents.

**This module persists nothing.** ``IMPLEMENTATION_PLAN.md`` §2.4 asks for a pure function; writing
``adjustment`` rows is not in it, and no row can be written before an approval exists to authorise
one, which is M5. Nothing here touches a database, a socket, a clock or a random source.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import uuid
from typing import Final

from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_QUANTUM,
    within_money_scale,
)
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.money.policy import LedgerContext, is_period, period_of

#: The rounding mode every monetary result declares. Never applied today — see the module docstring.
#:
#: Half-up rather than banker's rounding, stated as a decision rather than inherited from Python's
#: default. ``ROUND_HALF_EVEN`` is the better choice for a long series of independent roundings
#: because it does not accumulate bias; an adjustment is a single restatement a human approved, and
#: half-up is what an accountant checking one by hand will expect. When a formula first needs it,
#: that is the argument to revisit.
ROUNDING: Final = decimal.ROUND_HALF_UP


class NonCalculable(enum.StrEnum):
    """Why an instruction could not be priced. Closed, and deliberately without free text.

    §7 is explicit that a treatment which cannot be priced deterministically escalates, and that
    guessing is a defect rather than a fallback. These are the reasons a calculation stops, each one
    a genuine blocker in *this* increment — nothing here describes a workflow, an approval or a
    manual-review state, which belong to increments that do not exist yet.

    A closed enum rather than a message because a reason is machine state: something downstream will
    branch on it, a queue will group by it, and an operator will filter on it. A sentence cannot be
    any of those, and a sentence is also somewhere free text could start to accumulate.
    """

    TREATMENT_NOT_RECOGNISED = "treatment_not_recognised"
    TREATMENT_IS_ESCALATE = "treatment_is_escalate"
    NO_ACCOUNT_MAPPED = "no_account_mapped"
    NO_ORIGINATING_PERIOD = "no_originating_period"
    PERIOD_MALFORMED = "period_malformed"
    PERIOD_CLOSED = "period_closed"
    CURRENCY_NOT_FUNCTIONAL = "currency_not_functional"
    AMOUNT_IS_ZERO = "amount_is_zero"
    AMOUNT_OUTSIDE_MONEY_CONTRACT = "amount_outside_money_contract"


@dataclasses.dataclass(frozen=True, slots=True)
class ExceptionFacts:
    """An exception as the calculator sees it. Deliberately not the ORM row.

    Six fields, all structured, none of them text a human or a model wrote. There is no ``memo``, no
    ``rationale``, no ``description`` — the calculator cannot be handed prose, so no amount it
    produces can have come from prose.

    ``originating_period`` is the period the movement being reversed was recognised in, where the
    classification established one. It is a *fact supplied by the caller*, not a lookup: this module
    performs no I/O, and the relationship it comes from was proved deterministically by M2.3. It is
    ``None`` when no such counterpart exists, and :data:`TreatmentCode.ACCRUE` then has no period to
    accrue into and refuses.

    ``exception_id`` is carried for provenance and takes part in no arithmetic — asserted by a test
    that changes it and expects an identical amount.
    """

    exception_id: uuid.UUID
    classification: ExceptionClassification
    amount: decimal.Decimal
    currency: str
    value_date: dt.date
    originating_period: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class AdjustmentInstruction:
    """A priced adjustment: what to post, where, and in which period.

    **One signed amount against one account**, because that is what ``adjustment`` holds. Direction
    is carried by the sign rather than by a debit/credit pair, so the instruction model cannot
    express a two-legged posting — and a case that needs one is refused rather than approximated.

    Everything §12.1 will hash into an operation identifier is here: treatment, amount, currency,
    account, period and the ledger-context version they were derived under. Deriving that identifier
    is M4's, not this module's.
    """

    exception_id: uuid.UUID
    treatment: TreatmentCode
    amount: decimal.Decimal
    currency: str
    account_code: str
    period: str

    #: The quantisation the result conforms to, recorded alongside it as §7 requires.
    quantum: decimal.Decimal
    rounding: str

    #: Which configuration produced the account and the period (§12.1).
    ledger_context_version: str


#: The result of a calculation: priced, or explicitly not.
#:
#: A union rather than an optional, so a caller cannot mistake "could not be priced" for "priced at
#: nothing" — and cannot reach the amount without first having handled the other case. §6.2 writes
#: the return type as ``Money``, which says what a *successful* calculation yields; §7 says an
#: unpriceable treatment escalates. Both hold here.
CalculationResult = AdjustmentInstruction | NonCalculable


def compute_adjustment(
    exception: ExceptionFacts,
    treatment: TreatmentCode,
    ledger_ctx: LedgerContext,
) -> CalculationResult:
    """Price one approved treatment for one exception, or refuse.

    Pure and total: same inputs, same result; no I/O, no clock, no randomness; every input path ends
    in a value rather than an exception. The checks below run in a fixed order so that a case which
    fails two of them always reports the same reason — a refusal that varied with evaluation order
    would be a poor thing to route an operator by. The order itself is: a value outside the closed
    vocabulary, then escalate, then whether the combination is priceable at all, then the values,
    then the period.

    **The amount is the settlement line's own amount, unchanged, including its sign.** That is the
    single formula, and it is deliberately the only one. An exception *is* a settlement movement the
    ledger does not carry, so restating it is restating that amount; the treatment chooses where it
    lands and in which period, never how much it is. There is no arithmetic to get wrong, no
    intermediate to round, and no way for a treatment to change a number — which is the property
    that makes the containment claim structural rather than procedural.
    """
    # The treatment must be one of the four, and this is checked at runtime even though the
    # signature already says so.
    #
    # ``TreatmentCode`` is a ``StrEnum``, so a member compares and hashes equal to its own value —
    # which means a bare ``"rebook"`` string found its way through a dict keyed by members and
    # **obtained a priced instruction**. Worse, ``"escalate"`` slipped past the identity check
    # below, so the one treatment that must never be priced stopped being recognised as itself.
    # mypy rejects both, and mypy is not in the room when M3.2 parses a model's JSON response: at
    # that boundary a treatment arrives as text, and text is exactly what this refuses.
    #
    # Identity against the members rather than ``isinstance``, because the weaker check was the
    # first version of this guard and a reviewer broke it too: ``str.__new__(TreatmentCode,
    # "accrue")`` is an instance of the class without being any member of it, and it was priced —
    # into the *rebook* period, because the branches below compare by identity while the account
    # table resolves by equality, so the two halves disagreed about what they had been handed.
    # Identity is the only test they agree on, and every honest way of obtaining a treatment
    # (the member, ``TreatmentCode(value)``, ``TreatmentCode[name]``, copy, pickle) returns the
    # singleton, which is what makes it safe.
    #
    # A refusal rather than a raised error, because the calculator is total (§2.4).
    if not any(treatment is member for member in TreatmentCode):
        return NonCalculable.TREATMENT_NOT_RECOGNISED

    # Escalate is not a pricing failure, it is the outcome that says pricing was never appropriate,
    # and `adjustment` refuses a row for one outright. Checked first because no later question about
    # accounts or periods is meaningful for it.
    if treatment is TreatmentCode.ESCALATE:
        return NonCalculable.TREATMENT_IS_ESCALATE

    # Whether this *combination* is priceable at all, before anything about its values.
    #
    # The order matters and was chosen from evidence rather than taste. Checking the currency first
    # reported `currency_not_functional` for a GBP residual nobody could classify — true, and the
    # wrong thing to hand an operator, who would chase an exchange rate when the real blocker is
    # that the system cannot say what the movement is. Checking the account first never lies in the
    # other direction either: a combination that *is* mapped falls through to the value checks and
    # reports whichever of them actually stopped it.
    account = ledger_ctx.accounts.account_for(exception.classification, treatment)
    if account is None:
        return NonCalculable.NO_ACCOUNT_MAPPED

    # No conversion, ever. §3 lists a currency-conversion policy engine as a non-goal and no
    # deterministic rate source is approved, so an adjustment in a currency the books are not kept
    # in cannot be priced. The presentment and FX columns the settlement format carries are *not*
    # consulted: a rate the PSP recorded for its own conversion is not this ledger's rate, and
    # using it as one would be inventing an FX policy at the point of posting.
    if exception.currency != ledger_ctx.functional_currency:
        return NonCalculable.CURRENCY_NOT_FUNCTIONAL

    # A zero adjustment instructs the ledger to do nothing while carrying the full weight of an
    # approved financial instruction. Refused rather than posted: whatever the residual was, it was
    # not this.
    if exception.amount == 0:
        return NonCalculable.AMOUNT_IS_ZERO

    if not _within_money_contract(exception.amount):
        return NonCalculable.AMOUNT_OUTSIDE_MONEY_CONTRACT

    period = _period_for(exception, treatment)
    if period is None:
        return NonCalculable.NO_ORIGINATING_PERIOD

    # The shape is checked whichever branch produced it, and the check earns its place.
    #
    # ``originating_period`` is the one field a caller *derives* rather than reads, so it is the one
    # that can arrive malformed — and it flows straight into ``adjustment.period``, which the column
    # constrains to ``YYYY-MM``. Unchecked, ``"2026-13"`` produced a financial instruction with a
    # month that does not exist, and the failure surfaced at persistence in a later increment
    # rather than here. Worse, an empty string compared as "closed" and refused for a reason
    # that was not the real one.
    #
    # Refused rather than raised, because the plan's exit criterion is a calculator that is pure and
    # *total*: a malformed input must not be able to end a batch run.
    if not is_period(period):
        return NonCalculable.PERIOD_MALFORMED

    if not ledger_ctx.is_open(period):
        return NonCalculable.PERIOD_CLOSED

    return AdjustmentInstruction(
        exception_id=exception.exception_id,
        treatment=treatment,
        amount=exception.amount,
        currency=exception.currency,
        account_code=account,
        period=period,
        quantum=MONEY_QUANTUM,
        rounding=ROUNDING,
        ledger_context_version=ledger_ctx.version,
    )


def _period_for(exception: ExceptionFacts, treatment: TreatmentCode) -> str | None:
    """Which accounting period this treatment posts into, or ``None`` if it cannot be determined.

    Two rules, and the difference between them is the whole of what ``ACCRUE`` means here:

    * **REBOOK** and **WRITE_OFF** recognise the movement when it settled, so the period is the one
      the settlement line's own value date falls in.
    * **ACCRUE** recognises it in the period it economically belongs to — the period of the movement
      it reverses. That is the cross-period rule OPEN-4 required, and it is why the counterpart's
      period is an input: without one there is nothing to accrue *into*, and the calculator refuses
      rather than quietly falling back to the settlement date, which would make ``ACCRUE`` and
      ``REBOOK`` produce the same instruction while claiming to be different treatments.

    No clock, in either branch. Both periods come from business dates the settlement file stated.
    """
    if treatment is TreatmentCode.ACCRUE:
        return exception.originating_period
    return period_of(exception.value_date)


def _within_money_contract(amount: decimal.Decimal) -> bool:
    """Whether a value is storable exactly, under the same rule the database enforces (ADR-020).

    Value-based rather than representation-based: ``120.450000`` passes because four decimal
    places hold it exactly, while ``1.23456`` does not. Mirrored from ``money_scale_constraint``
    so a calculation cannot produce something the column would reject — and checked *here*, in
    the calculator, rather than left to PostgreSQL, because a database that rounds a computed
    value on the way in is the failure this project spent M1.1 removing.

    **Read off the digits, not computed.** The first version scaled by ``10**4`` and asked whether
    the result was integral, which is the right *question* asked through the wrong instrument:
    ``scaleb`` is a context operation and rounds to the context's precision, 28 significant digits
    by default. An amount with 29 decimal places therefore scaled to something integral and was
    **priced** — the guard rounded the evidence away before inspecting it, which is the same class
    of mistake as letting the database round a value on the way in. ``copy_abs`` is used below for
    the same reason: plain ``abs()`` is context-aware too.

    ``as_tuple`` is exact and reads nothing from the ambient context, so the answer cannot depend on
    what some caller elsewhere in the process set ``decimal.getcontext().prec`` to. A test pins that
    by re-running the whole calculation under a deliberately tiny precision.
    """
    if amount.is_nan() or amount.is_infinite():
        return False
    if not within_money_scale(amount):
        return False
    return amount.copy_abs() < MONEY_MAGNITUDE_EXCLUSIVE_BOUND
