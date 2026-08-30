"""The classification rule set — OPEN-3, resolved in ADR-045.

FR-4 names a closed taxonomy: partial capture, fee split, chargeback reversal, FX rounding,
cross-period refund, unclassified. This module decides which of those a deterministic classifier
can actually *prove* from the data the system persists, and states the evidence for each in one
place so a rule can be read without reading the code that applies it.

**Three classes are reachable; two are not, for the same structural reason.**

``partial_capture`` and ``fx_rounding`` are both statements about a settlement line's relationship
to *one particular ledger entry* — "captured less than the entry authorised", "differs from the
entry's own conversion by a rounding artefact". Proving either requires knowing which entry, and no
deterministic key links a settlement line to a ledger entry. ``ledger_entry`` carries the ledger's
own ``external_ref`` and a free-text ``description``; neither holds the PSP's reference. Amount,
currency and date are what M2.2 already matches on, and where they identify an entry uniquely M2.2
has *already consumed it* — a line is residual precisely because they did not. At corpus volume the
gap is not marginal: a residual line typically shares its currency and date window with hundreds of
unconsumed entries. The only remaining route is substring-matching the ledger description, which
M2.2 refused for the same reason (see ``matching.policy.MatchRule``) and which this increment is
forbidden to introduce.

So both classes stay declared and unassigned. That is the honest outcome, not a coverage failure: a
classifier that named one of them would be asserting a cause it cannot evidence, and a control
record that says ``fx_rounding`` where no currency conversion is visible anywhere in the data is
worse than one that says ``unclassified``.

**What is provable is the relationship between settlement lines themselves.** The merchant's own
reference is an exact key the PSP passes through, it needs no fuzzy comparison, and each line
carries whether M2.2 reconciled it. That supports three rules — a reversal of a booked debit, a
reversal of a booked credit across a period boundary, and a movement the PSP split across rows —
and each is corroborated rather than guessed.

**This module introduces no tolerance.** Every monetary comparison here is exact ``Decimal``
equality. The system has one tolerance policy, it lives in :mod:`..matching.policy`, and a second
one hidden in classification would be a band nobody had decided.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from ledger_exception_control_plane.db.control import ExceptionClassification


class ClassificationRule(enum.StrEnum):
    """The closed set of ways a residual may be classified. Persisted in ``exception.rule_id``.

    The identifiers name the **evidence**, not the class, and deliberately so. ``fee_split`` says
    what was concluded; ``deductions_split_across_rows`` says what was observed, which is what an
    analyst reviewing the decision — or an auditor asking why it was taken — actually needs.

    ``NO_RULE_MATCHED`` is a first-class member rather than an absence. A residual nothing could
    explain is a decision the system took, and a row recording ``unclassified`` with no rule id
    would be indistinguishable from a row written before the classifier existed.
    """

    REVERSAL_OF_BOOKED_DEBIT = "reversal_of_booked_debit"
    REVERSAL_OF_BOOKED_CREDIT_ACROSS_PERIODS = "reversal_of_booked_credit_across_periods"
    DEDUCTIONS_SPLIT_ACROSS_ROWS = "deductions_split_across_rows"
    NO_RULE_MATCHED = "no_rule_matched"


#: What each rule concludes. A total mapping over the enum, checked by test, so a rule cannot be
#: added without stating which class it assigns.
RULE_CLASSIFICATION: Final[dict[ClassificationRule, ExceptionClassification]] = {
    ClassificationRule.REVERSAL_OF_BOOKED_DEBIT: ExceptionClassification.CHARGEBACK_REVERSAL,
    ClassificationRule.REVERSAL_OF_BOOKED_CREDIT_ACROSS_PERIODS: (
        ExceptionClassification.CROSS_PERIOD_REFUND
    ),
    ClassificationRule.DEDUCTIONS_SPLIT_ACROSS_ROWS: ExceptionClassification.FEE_SPLIT,
    ClassificationRule.NO_RULE_MATCHED: ExceptionClassification.UNCLASSIFIED,
}

#: Precedence, highest first, declared rather than implied by the order two ``if`` branches happen
#: to be written in.
#:
#: **It is currently never exercised, and that is the stronger position.** The rule set is pairwise
#: disjoint: the two reversal rules differ by the sign of the subject, and the group rule requires
#: *zero* booked exact offsets where both reversal rules require exactly one. No line can satisfy
#: two of them, so the outcome cannot depend on rule order at all. A test sweeps the shapes that
#: could plausibly collide and asserts at most one rule fires.
#:
#: That disjointness replaced a real defect rather than describing an original design. The rule set
#: did admit one overlap — a line both reversing a booked movement and sitting in a group with a
#: smaller deduction — and this list resolved it correctly. What it could not resolve is a
#: higher-priority rule that *declines*: precedence orders the rules that fire, so a reversal rule
#: examining a line and then declining left it to be settled by the group rule. An in-period refund,
#: which the taxonomy deliberately has no class for, came back ``fee_split`` as soon as the order
#: carried one more unmatched credit. The fix is in :func:`~..engine._deductions_split_across_rows`:
#: a line the reversal family has a claim on is not available to the group rule, whatever the family
#: concludes. Same lesson as ADR-043 in M2.2 — an unresolved higher-priority claim must never be
#: settled by a lower-priority rule.
#:
#: The list stays because a fourth rule may not be disjoint from the other three, and the order it
#: would then need should be a decision on record rather than one made in a hurry.
RULE_PRECEDENCE: Final[tuple[ClassificationRule, ...]] = (
    ClassificationRule.REVERSAL_OF_BOOKED_DEBIT,
    ClassificationRule.REVERSAL_OF_BOOKED_CREDIT_ACROSS_PERIODS,
    ClassificationRule.DEDUCTIONS_SPLIT_ACROSS_ROWS,
)

#: The rule set's revision, persisted on every exception it classifies.
#:
#: Classification is deterministic *for a given rule set*, so the outcome alone does not explain
#: itself: a row saying ``fee_split`` with no ruleset version says what was decided and nothing
#: about what would decide it the same way again. Bumped by hand whenever a rule's evidence,
#: precedence or fallback behaviour changes — never derived from a clock, a commit or a file hash,
#: all of which would move without a decision having been taken.
CLASSIFIER_VERSION: Final = "residual-r1"


def accounting_period(value_date: dt.date) -> str:
    """The accounting period a business date falls in, as ``YYYY-MM``.

    Calendar months, matching the format ``adjustment.period`` already commits to. The input is a
    **business date from the settlement file** — never the clock, never the current month: a
    classification that changed with the day it was re-run would not be a classification.

    This answers "did these two movements fall in different periods", which is all
    :data:`ClassificationRule.REVERSAL_OF_BOOKED_CREDIT_ACROSS_PERIODS` needs. It does **not**
    assign a posting period; which period an adjustment is booked into is OPEN-4's decision and
    M2.4's to make.
    """
    return f"{value_date.year:04d}-{value_date.month:02d}"
