"""The deterministic money path — increment M2.4.

An exception's structured facts, an approved treatment code and an explicit ledger context in;
the financial instruction they imply out, or a closed reason why none can be produced. It answers
exactly one question:

    *Given a treatment somebody approved, what does this exception instruct the ledger to do?*

It does not decide the treatment, obtain the approval, or post anything. There is no model
here and none can be: this package exists **before** any model layer in the codebase (ADR-003),
so the containment argument is a fact about the build order rather than a claim added afterwards.

**A model can never reach an amount.** The calculator takes three arguments — facts, a member of a
four-value enum, and system-owned configuration — and none of them has a field free text could
arrive in. The one value a model will ever influence is the treatment code, and a treatment selects
an *account and a period*, never a number: the amount is always the settlement movement's own,
unchanged, sign included. There is no arithmetic for a hallucination to enter.

**It refuses more than it prices, on purpose.** ``fee_split`` is a movement split across rows whose
whole the calculator cannot see; ``unclassified`` is a residual nobody could name; ``escalate`` is
the outcome that says pricing was never appropriate. Each is a closed :class:`NonCalculable` reason,
because §7 requires an unpriceable treatment to escalate and calls guessing a defect rather than a
fallback.

**Nothing is persisted.** M2.4's deliverable is a pure function; no ``adjustment`` row can exist
before an approval authorises one, and approvals are M5.

* :mod:`.policy` — account mapping and period assignment (OPEN-4, resolved in ADR-047).
* :mod:`.calculator` — the pure function, its inputs, and the reasons it can refuse.

Usable without anything running::

    from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, compute_adjustment

    result = compute_adjustment(facts, TreatmentCode.REBOOK, DEMO_LEDGER_CONTEXT)
"""

from typing import Final

from ledger_exception_control_plane.money.calculator import (
    ROUNDING,
    AdjustmentInstruction,
    CalculationResult,
    ExceptionFacts,
    NonCalculable,
    compute_adjustment,
)
from ledger_exception_control_plane.money.policy import (
    ACCOUNT_CHARGEBACKS,
    ACCOUNT_PSP_FEES,
    ACCOUNT_REVENUE,
    ACCOUNT_SETTLEMENT_CLEARING,
    ACCOUNT_WRITE_OFFS,
    DEMO_ACCOUNT_POLICY,
    AccountPolicy,
    AmbiguousAccountPolicyError,
    LedgerContext,
    account_policy,
    is_period,
    period_of,
)

#: A ledger context for the fictional organisation the corpus models. **Demo configuration.**
#:
#: EUR books, everything from 2026-06 open. Declared here rather than assembled at each call site
#: so the examples, the fixture evaluation and the tests all price against one configuration, and
#: so a reader can see the whole of what the calculator depends on in one object.
DEMO_LEDGER_CONTEXT: Final = LedgerContext(
    version="demo-2026-06",
    functional_currency="EUR",
    earliest_open_period="2026-06",
    accounts=DEMO_ACCOUNT_POLICY,
)

__all__ = [
    "ACCOUNT_CHARGEBACKS",
    "ACCOUNT_PSP_FEES",
    "ACCOUNT_REVENUE",
    "ACCOUNT_SETTLEMENT_CLEARING",
    "ACCOUNT_WRITE_OFFS",
    "DEMO_ACCOUNT_POLICY",
    "DEMO_LEDGER_CONTEXT",
    "ROUNDING",
    "AccountPolicy",
    "AdjustmentInstruction",
    "AmbiguousAccountPolicyError",
    "CalculationResult",
    "ExceptionFacts",
    "LedgerContext",
    "NonCalculable",
    "account_policy",
    "compute_adjustment",
    "is_period",
    "period_of",
]
