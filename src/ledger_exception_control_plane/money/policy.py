"""Account mapping and accounting-period policy — OPEN-4, resolved in ADR-047.

OPEN-4 required two things to be settled before the calculator could exist: how a treatment maps to
a ledger account, and how a period is assigned for cross-period cases. Its constraint was that both
must be **configuration, not code**, and deterministic.

So they are configuration. :class:`AccountPolicy` is a closed table keyed by the two structured
values a decision is made from — the exception's classification and the approved treatment — and the
calculator consults it rather than containing it. Nothing in the arithmetic knows an account code
exists.

**What can be priced is therefore configuration too**, which is the property that made this
shape worth choosing. A combination with no configured account is not calculable, and adding one
later changes a table rather than a formula. The omissions in :data:`DEMO_ACCOUNT_POLICY` are
deliberate and each is recorded there.

**The account codes are synthetic demo configuration.** This project has no real chart of
accounts and inventing one that looked authoritative would be worse than saying so. Four of the
five codes are the fictional chart the M1.3 corpus already uses — it was built with "enough to
make account selection a real decision for M2.4" — and the fifth is declared here for the same
fictional organisation. A real deployment replaces the whole table; nothing outside it needs to
change, which is what makes the decision narrow and reversible.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import types
from collections.abc import Mapping, Sequence
from typing import Final

from ledger_exception_control_plane.classification.taxonomy import accounting_period
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode

#: Ledger account codes. **Synthetic demo configuration**, not a real chart of accounts.
#:
#: The first four are the fictional chart the committed corpus was built around. ``WRITE_OFFS``
#: is declared here because writing a residual off has to land somewhere and the corpus had no
#: reason to model an expense account it never populated. Declaring it openly in the one place
#: accounts are declared is the alternative to a code appearing inside a formula.
ACCOUNT_SETTLEMENT_CLEARING: Final = "2100"
ACCOUNT_REVENUE: Final = "4100"
ACCOUNT_CHARGEBACKS: Final = "4900"
ACCOUNT_PSP_FEES: Final = "6200"
ACCOUNT_WRITE_OFFS: Final = "6900"

#: The shape ``adjustment.account_code`` accepts. Checked when a policy is built, so a malformed
#: code is refused where it is configured rather than when something tries to post it.
_ACCOUNT_CODE: Final = re.compile(r"^[0-9]{4}$")

#: The shape ``adjustment.period`` accepts (``YYYY-MM``), mirrored from the column's own constraint.
_PERIOD: Final = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

#: An ISO 4217 alphabetic code, mirrored from ``currency_format_constraint``.
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")


class AmbiguousAccountPolicyError(ValueError):
    """Two rules configured for one (classification, treatment) pair.

    Raised when the policy is *built*, never during a calculation. A mapping that answers one
    question twice has no deterministic answer, and the moment to find that out is at configuration
    rather than halfway through pricing a financial instruction. A plain ``dict`` literal would have
    silently kept the last of the two, which is why the policy is built from a sequence of rules.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class AccountPolicy:
    """Which ledger account an approved treatment posts to, for a given exception class.

    Frozen and passed explicitly — never read from ambient configuration — for the reason
    :class:`~..matching.policy.TolerancePolicy` is: a financial decision has to be reconstructable
    later, so the policy that produced it is an argument rather than something the code looks up.
    """

    #: Keyed by the two structured facts, and by nothing else. No free text reaches this table, so
    #: no account can be selected by matching a string against prose.
    rules: Mapping[tuple[ExceptionClassification, TreatmentCode], str]

    def __post_init__(self) -> None:
        for (classification, treatment), account in self.rules.items():
            if not isinstance(classification, ExceptionClassification):
                raise TypeError(f"policy key is not a classification: {classification!r}")
            if not isinstance(treatment, TreatmentCode):
                raise TypeError(f"policy key is not a treatment: {treatment!r}")
            if treatment is TreatmentCode.ESCALATE:
                # `adjustment` forbids an escalated treatment outright (§6.2), so an account
                # configured for one could never be used and its presence would imply otherwise.
                raise ValueError("escalate is never posted and cannot be mapped to an account")
            if not _ACCOUNT_CODE.fullmatch(account):
                raise ValueError(f"not a valid account code: {account!r}")
        # Frozen the dataclass may be, but the mapping it was handed is not, and a reviewer walked
        # straight through the gap: assigning into ``DEMO_ACCOUNT_POLICY.rules`` after construction
        # produced an instruction posting to ``NOT-AN-ACCOUNT``. Every check above is an *entry*
        # check, so a live mapping makes them advisory. Taking a private snapshot behind a read-only
        # view makes them invariants — which matters more here than usual, because this is the only
        # place account-code shape is enforced at all (``adjustment.account_code`` has no database
        # constraint behind it).
        object.__setattr__(self, "rules", types.MappingProxyType(dict(self.rules)))

    def account_for(
        self, classification: ExceptionClassification, treatment: TreatmentCode
    ) -> str | None:
        """The configured account, or ``None`` when this combination is not configured.

        ``None`` means *not priceable*, and that is the whole mechanism: the calculator has no
        fallback account and no default, so a combination nobody has decided an account for cannot
        produce a financial instruction. Fail-closed by absence rather than by a branch.
        """
        return self.rules.get((classification, treatment))


def account_policy(
    rules: Sequence[tuple[ExceptionClassification, TreatmentCode, str]],
) -> AccountPolicy:
    """Build a policy from a sequence of rules, refusing a pair configured twice.

    A sequence rather than a mapping literal, deliberately. ``dict`` resolves a duplicate key by
    keeping whichever rule was written last, which for an account mapping means a silent choice
    between two accounts — exactly the kind of decision this project does not let a data structure
    make. Here it raises.
    """
    collected: dict[tuple[ExceptionClassification, TreatmentCode], str] = {}
    for classification, treatment, account in rules:
        key = (classification, treatment)
        if key in collected:
            raise AmbiguousAccountPolicyError(
                f"{classification.value} + {treatment.value} is mapped to both "
                f"{collected[key]} and {account}"
            )
        collected[key] = account
    return AccountPolicy(rules=collected)


#: The demo mapping. Synthetic configuration for a fictional organisation.
#:
#: **The absences are the interesting part, and each is deliberate.**
#:
#: ``fee_split`` is absent for every treatment. A fee split is one economic movement the PSP
#: reported across several rows, and the ledger booked its *net*. Pricing one of those rows in
#: isolation would post part of a movement whose whole the calculator cannot see, and would
#: double-count what the ledger already carries. The correct treatment is a two-legged
#: reclassification, which ``adjustment`` cannot express — it holds one signed amount against one
#: account — so there is nothing here to configure rather than a value nobody has chosen.
#:
#: ``unclassified`` is absent for every treatment. The system could not say what the residual is, so
#: it cannot say which account restates it. An account configured here would be a guess wearing
#: configuration's clothes.
#:
#: ``partial_capture`` and ``fx_rounding`` are absent because no exception can carry them: both are
#: claims about a residual's relationship to one particular ledger entry and M2.3 assigns neither
#: (ADR-045). Configuring an account for a class nothing produces would assert a capability that
#: does not exist.
#:
#: ``ACCRUE`` and ``REBOOK`` share an account within a class, and only the period differs. That is
#: what the two treatments *are* here: the same restatement, recognised either when it settled or in
#: the period it economically belongs to.
DEMO_ACCOUNT_POLICY: Final = account_policy(
    [
        # A chargeback reversal restates a credit against the account the chargeback was booked to.
        (
            ExceptionClassification.CHARGEBACK_REVERSAL,
            TreatmentCode.REBOOK,
            ACCOUNT_CHARGEBACKS,
        ),
        (
            ExceptionClassification.CHARGEBACK_REVERSAL,
            TreatmentCode.ACCRUE,
            ACCOUNT_CHARGEBACKS,
        ),
        (
            ExceptionClassification.CHARGEBACK_REVERSAL,
            TreatmentCode.WRITE_OFF,
            ACCOUNT_WRITE_OFFS,
        ),
        # A refund reduces revenue, whichever period it is recognised in.
        (
            ExceptionClassification.CROSS_PERIOD_REFUND,
            TreatmentCode.REBOOK,
            ACCOUNT_REVENUE,
        ),
        (
            ExceptionClassification.CROSS_PERIOD_REFUND,
            TreatmentCode.ACCRUE,
            ACCOUNT_REVENUE,
        ),
        (
            ExceptionClassification.CROSS_PERIOD_REFUND,
            TreatmentCode.WRITE_OFF,
            ACCOUNT_WRITE_OFFS,
        ),
    ]
)


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerContext:
    """The deterministic accounting context a calculation is performed against (§6.2).

    Everything the calculator needs beyond the exception and the treatment, and nothing else. It is
    **system-owned**: every field is a structured value some other part of the system decided, none
    of it originates in a model, and there is no field free text could arrive in.

    ``version`` exists because §12.1 binds the ledger-context version into the instruction payload
    hash: if account mapping or period configuration changes between a first attempt and a re-send,
    the instruction is genuinely different and must produce a different operation identifier. That
    identifier is M4's to derive; carrying the version on the result is what makes it derivable.
    """

    #: Identifies this configuration. Changed by hand when any field below changes.
    version: str

    #: The currency the ledger keeps its books in. An adjustment in any other currency needs a rate,
    #: and no approved deterministic rate source exists (§3 lists a conversion policy engine as a
    #: non-goal), so the calculator refuses rather than converting.
    functional_currency: str

    #: The earliest period still open for posting, ``YYYY-MM``. A target period before it is closed,
    #: and the calculator refuses: no approved policy says where a movement belonging to a closed
    #: period should go instead, and "the next open period" is a decision nobody has taken.
    earliest_open_period: str

    accounts: AccountPolicy

    def __post_init__(self) -> None:
        if not is_currency(self.functional_currency):
            raise ValueError(f"not an ISO 4217 code: {self.functional_currency!r}")
        if not _PERIOD.fullmatch(self.earliest_open_period):
            raise ValueError(f"not a YYYY-MM period: {self.earliest_open_period!r}")
        if not self.version:
            raise ValueError("a ledger context must identify its version")

    def is_open(self, period: str) -> bool:
        """Whether a period is open for posting.

        String comparison, and it is correct rather than lucky: ``YYYY-MM`` is zero-padded and
        fixed-width, so lexicographic order is chronological order. Asserted by test at a year
        boundary, because that is the comparison a reader is entitled to doubt.
        """
        return period >= self.earliest_open_period


def is_currency(value: str) -> bool:
    """Whether a string is a well-formed ISO 4217 alphabetic code.

    Extracted from :class:`LedgerContext`'s own check rather than written afresh, so the rule keeps
    exactly one declaration. 4.1 needs it at the persistence boundary: the ``adjustment`` column
    does carry a currency-format constraint, but reaching it means an ``IntegrityError`` mid-flush,
    which deactivates the caller's transaction — a refusal one step earlier is the same answer
    without the collateral damage.
    """
    return bool(_CURRENCY.fullmatch(value))


def is_account_code(value: str) -> bool:
    """Whether a string is a well-formed ledger account code.

    The companion to :func:`is_period`, and added for the same reason one increment later: the
    ``adjustment`` column that stores an account code is a bare ``String(64)`` with **no check
    constraint at all**, so unlike the period there is no database backstop. An adversarial review
    of 4.1 walked an arbitrary 48-character string straight into a priced, identified, persisted
    instruction.

    The rule is stated once, here, beside the policy that enforces it on the way in — a second
    declaration elsewhere would be free to drift from this one, which is the failure this project
    keeps a single ``TreatmentCode`` declaration to avoid.
    """
    return bool(_ACCOUNT_CODE.fullmatch(value))


def is_period(value: str) -> bool:
    """Whether a string is a well-formed ``YYYY-MM`` accounting period.

    The same shape ``adjustment.period`` constrains, checked in the calculator so a malformed period
    cannot reach a financial instruction and surface at persistence in a later increment. Rejects a
    month of ``00`` or ``13`` as firmly as it rejects prose.
    """
    return bool(_PERIOD.fullmatch(value))


def period_of(value_date: dt.date) -> str:
    """The accounting period a business date falls in, as ``YYYY-MM``.

    Re-exported from the classifier rather than redefined. "Which period is this date in" is one
    decision and the project takes it once; a second copy here would let two increments drift on
    what a period means, and the drift would be invisible until a cross-period adjustment posted
    to the wrong month.

    No clock is involved anywhere: the input is a business date the settlement file stated.
    """
    return accounting_period(value_date)
