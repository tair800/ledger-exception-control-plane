"""The matching rules and the tolerance policy — OPEN-2, resolved in ADR-042.

`IMPLEMENTATION_PLAN.md` §2.2 requires "configurable tolerance bands" and FR-3 requires the rule
that matched a line to be recorded. Neither states a threshold, and nobody on this project has
measured a real settlement feed, so the numbers below are a **declared project decision** rather
than an empirical finding. They are stated here in one place, typed and frozen, so that changing
them is a visible act rather than an edit to a constant buried in a comparison.

**Why the band is narrow, and why that is the safe direction.**

In this system a tolerance match means the difference is *dropped*. There is no compensating
posting: the line is marked matched, the residual never becomes an exception, and the few cents
never reach the ledger. A difference that becomes a residual, by contrast, is eventually booked —
that is the entire path the rest of this project builds. So the two errors are not symmetric:

* **Band too tight** — an immaterial difference becomes a residual. Cost: an analyst looks at it.
* **Band too loose** — a real difference is absorbed and never booked. Cost: the ledger stays
  wrong by that amount, permanently, and nobody is ever shown it.

The second is the failure this project exists to prevent, so the default sits at the noise floor:
**one minor unit of the currency's own precision.** That is exactly one rounding at the precision
the source itself uses. Two independent roundings can compound to two units, and a band of two
would absorb them — but it would also absorb a genuine two-cent shortfall, and there is no evidence
here to justify preferring that. One unit is the narrowest band that still absorbs a real artefact.

**A currency with no declared band gets no tolerance at all.** Not a fallback default: an unknown
currency means nobody has decided what is immaterial in it, and the safe reading of "undecided" is
"exact match only". Fail-closed.

**Consequence, measured rather than asserted.** The committed *canonical* corpus clears nothing by
tolerance: it holds one instance of each condition, and both of its near misses happen to differ by
two minor units. The *bulk* profile does exercise the band — its near-miss instances draw a drift of
one to three units, so roughly a third of them land inside it. Measured at 200 instances: 81.9% of
lines cleared with no model call, 169 exactly and 7 by tolerance. That the canonical corpus does not
happen to reach the band is a property of two drawn values, not a reason to widen it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
from collections.abc import Mapping
from typing import Final


class MatchRule(enum.StrEnum):
    """The closed set of ways a line may be matched. Persisted in ``match_result.rule_id``.

    Stable identifiers, not prose: a stored rule is machine state that a later increment, a report
    or an auditor reads, and "matched because the amount was close enough" is not something a test
    can assert on.

    There is deliberately no reference-based rule. ``ledger_entry`` holds the ledger's own
    ``external_ref`` and a free-text ``description``; neither carries the PSP's reference, so a
    reference rule would have to search description text — fuzzy matching on free text, which this
    project does not do and which M2.1 deliberately preserved references *in order to avoid*.
    """

    EXACT_AMOUNT = "exact_amount"
    AMOUNT_WITHIN_TOLERANCE = "amount_within_tolerance"


@dataclasses.dataclass(frozen=True, slots=True)
class TolerancePolicy:
    """The bands. Frozen, typed, and passed explicitly — never read from ambient configuration.

    A matching decision has to be reconstructable later, so the policy that produced it is an
    argument to the matcher rather than something it looks up. Two dimensions, both stated:

    ``amount`` maps an ISO currency code to the **largest absolute difference that may be
    absorbed**, inclusive. Per-currency rather than a single number, because "one minor unit" is
    0.01 in EUR, 1 in JPY and 0.001 in BHD — a single figure would be three different policies
    wearing one value.

    ``value_date_window_days`` is a **hard eligibility filter**, not a band: a candidate outside it
    is not considered at all, by either rule. It exists because ``settlement_line.value_date`` is a
    date the PSP states and ``ledger_entry.booked_at`` is a timestamp another system wrote;
    requiring them to fall on the same calendar day is a stronger claim than the two clocks
    support.
    """

    amount: Mapping[str, decimal.Decimal]
    value_date_window_days: int

    def __post_init__(self) -> None:
        if self.value_date_window_days < 0:
            raise ValueError("value_date_window_days must not be negative")
        for currency, band in self.amount.items():
            if band < 0:
                raise ValueError(f"tolerance band for {currency} must not be negative")

    def band(self, currency: str) -> decimal.Decimal | None:
        """The band for a currency, or ``None`` when none is declared.

        ``None`` means "no tolerance", not "zero tolerance" — the distinction matters because a
        band of zero would still admit the tolerance *rule* with a zero difference, and a match
        recorded under ``amount_within_tolerance`` that absorbed nothing would misrepresent itself.
        """
        return self.amount.get(currency)

    def within_window(self, value_date: dt.date, booked_on: dt.date) -> bool:
        """Whether two dates are close enough for the pair to be considered at all."""
        return abs((value_date - booked_on).days) <= self.value_date_window_days


#: One minor unit of each currency the corpus uses. The module docstring says why one, not two.
#:
#: JPY has no minor unit, so its band is a whole yen — the smallest difference the currency can
#: express. BHD uses three decimal places. Writing these as literals rather than deriving them from
#: a table of minor digits keeps the policy readable as *money*: a reviewer can see what is being
#: absorbed without holding ISO 4217 in their head.
DEFAULT_POLICY: Final = TolerancePolicy(
    amount={
        "EUR": decimal.Decimal("0.01"),
        "USD": decimal.Decimal("0.01"),
        "GBP": decimal.Decimal("0.01"),
        "JPY": decimal.Decimal("1"),
        "BHD": decimal.Decimal("0.001"),
    },
    value_date_window_days=1,
)

#: Exact-only. Not a variant anyone is asked to run — it exists so a test can prove the tolerance
#: rule is what admits a near miss, rather than something else in the pipeline.
EXACT_ONLY_POLICY: Final = TolerancePolicy(amount={}, value_date_window_days=1)
