"""The classification decision, as a pure function. No database, no clock, no randomness.

Given the residual lines and the settlement movements around them, this returns the same
classification every time — and the same classification regardless of the order the inputs arrive
in, because every rule is expressed over *sets* rather than over a walk.

**The classifier cannot see a ledger entry.** :class:`SettlementMovement` carries six fields, none
of which comes from the ledger, so "run a second matcher" is not something a rule here can express
however tempting it looks. That containment is structural, not a promise: M2.2 is the only code in
this system that pairs a settlement line with a ledger entry, and nothing here can consume one,
release one, or write a ``match_result``. What a residual *did* get from the ledger is compressed
into one boolean per movement — whether M2.2 reconciled it — which is the only ledger fact any rule
below needs.

It cannot see free text either. No memo, no description, no PSP reference: the relationships it
reasons about are keyed on the merchant's own reference, compared exactly, with no canonicalisation
and no substring test.

**Ambiguity refuses rather than picks.** Where a rule needs a corroborating movement it requires
*exactly one*, the same discipline M2.2 applies to candidate entries. Two matched movements that
both exactly offset a residual do not make the classification twice as likely; they make it
unprovable, and the line falls to the fallback.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import decimal
import uuid
from collections.abc import Callable, Iterable, Sequence

from ledger_exception_control_plane.classification.taxonomy import (
    RULE_CLASSIFICATION,
    RULE_PRECEDENCE,
    ClassificationRule,
    accounting_period,
)
from ledger_exception_control_plane.db.control import ExceptionClassification


@dataclasses.dataclass(frozen=True, slots=True)
class SettlementMovement:
    """A settlement line as the classifier sees it. Deliberately not the ORM row.

    Six fields, and none of them from the ledger: no entry, no account, no description, and no
    free text of any kind. ``matched`` is the whole of what M2.2 concluded about this line, which
    is all any rule needs — whether the ledger already carries this movement.
    """

    id: uuid.UUID
    merchant_reference: str | None
    amount: decimal.Decimal
    currency: str
    value_date: dt.date
    matched: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Classification:
    """One classified residual, with the rule that reached it."""

    line_id: uuid.UUID
    classification: ExceptionClassification
    rule_id: ClassificationRule


@dataclasses.dataclass(frozen=True, slots=True)
class _Evidence:
    """One residual and the movements that may explain it.

    ``related`` is every *other* movement sharing the subject's merchant reference and currency.
    Same currency because this system performs no conversion, so two amounts in different
    currencies cannot offset each other — they are incomparable, not merely unequal. No date bound:
    a refund settling two months after its capture is still that capture's refund, and bounding the
    relation would silently unclassify exactly the cross-period case the taxonomy names.
    """

    subject: SettlementMovement
    related: tuple[SettlementMovement, ...]

    def exact_offsets_already_booked(self) -> tuple[SettlementMovement, ...]:
        """Related movements the ledger has *already* reconciled that this one exactly reverses.

        Exact ``Decimal`` equality, not a band. A reversal that does not restore the original
        amount is not evidence of a reversal, and the one tolerance policy in this system belongs
        to matching (ADR-042) — reusing it here would apply a band to a question nobody decided it
        for.
        """
        return tuple(
            movement
            for movement in self.related
            if movement.matched and movement.amount == -self.subject.amount
        )

    def unreconciled_group(self) -> tuple[SettlementMovement, ...]:
        """The subject together with every related movement the ledger has not reconciled."""
        return (self.subject, *(m for m in self.related if not m.matched))


def _reversal_of_booked_debit(evidence: _Evidence) -> bool:
    """A credit that exactly reverses a debit the ledger already carries.

    The taxonomy's class for the reversal of a booked movement is ``chargeback_reversal``, and this
    is the evidence available for it: the merchant's order carries an earlier negative movement
    that M2.2 reconciled, this residual restores exactly that amount, and the ledger has nothing
    matching the restoration.

    **What this cannot prove, stated plainly.** That the original debit was a *chargeback* rather
    than a fee reversal or a correction. The PSP declares a transaction type on every row and M2.1
    normalises it, but ``settlement_line`` has no column for it, so the declaration is validated
    and discarded before anything can read it. Within a closed taxonomy whose only reversal class
    is this one, mapping here is the sanctioned fallback (ADR-045); the limitation is recorded
    rather than papered over.
    """
    if evidence.subject.amount <= 0:
        return False
    return len(evidence.exact_offsets_already_booked()) == 1


def _reversal_of_booked_credit_across_periods(evidence: _Evidence) -> bool:
    """A debit that exactly reverses a credit the ledger already carries, in a later period.

    The direction is the discriminator and it is an accounting fact, not a corpus artefact: a
    capture is a credit and its refund a debit, while a chargeback is a debit and its reversal a
    credit. Both rules require the counterpart to have been *booked*, so neither fires on a pair
    the ledger never saw.

    The period test is what makes this the taxonomy's ``cross_period_refund`` rather than a refund
    in general — and the taxonomy has no class for a refund that settles in its own period, so one
    falls to the fallback rather than borrowing this label. Periods are calendar months read from
    the two business dates; no clock is consulted, and no posting period is assigned here.
    """
    if evidence.subject.amount >= 0:
        return False
    offsets = evidence.exact_offsets_already_booked()
    if len(offsets) != 1:
        return False
    return accounting_period(offsets[0].value_date) != accounting_period(
        evidence.subject.value_date
    )


def _deductions_split_across_rows(evidence: _Evidence) -> bool:
    """One economic movement the PSP reported across several rows, none of which reconciled.

    A fee split is a gross amount reduced by deductions that the ledger booked as a single net
    entry, so no individual row equals any individual entry — which is exactly why every row of it
    survives matching. The observable shape is a group of unreconciled movements on one order
    carrying both a credit and debits, where **the debits together are strictly smaller than the
    largest credit**.

    That last condition is doing real work rather than tidying. It is what a deduction *is*: a fee
    comes out of a capture and cannot exceed it. Without it the rule would also fire on a
    chargeback and its reversal when neither reconciled — equal and opposite, which is an offset
    and not a deduction — and would label a reversal pair a fee split.

    **A line the reversal family has a claim on is not available to this rule**, whether or not
    that family reached a conclusion. This is the same defect M2.2 corrected in ADR-043, in a new
    place: precedence orders the rules that *fire*, so a higher-priority rule that examines a line
    and then declines leaves it to be settled by a lower-priority one — which is an unresolved
    claim being resolved by a weaker rule, exactly what precedence exists to prevent.

    It is reachable and it was reached. A full refund of an already-booked capture *in the same
    period* is the case the taxonomy has no class for, and the period test declines it correctly;
    but with one further unmatched credit on the same order, the group shape then matched and the
    refund was named ``fee_split``. Adding an unrelated row to an order changed the class of the
    refund, and a customer refund was labelled a PSP deduction. Two booked offsets — ambiguous
    reversal evidence — fell through the same way. Both now stop here.

    Note it is the *evidence* that blocks, not the verdict: any exact offset already booked means
    this line is a reversal question, and a reversal question the system cannot answer is
    ``unclassified``, never a fee split.
    """
    if evidence.exact_offsets_already_booked():
        return False
    group = evidence.unreconciled_group()
    inflows = [m.amount for m in group if m.amount > 0]
    deductions = [-m.amount for m in group if m.amount < 0]
    if not inflows or not deductions:
        return False
    return sum(deductions) < max(inflows)


#: The rules themselves, keyed by their stable identifier. Applied in
#: :data:`~..taxonomy.RULE_PRECEDENCE`, which is declared separately so precedence is a statement
#: rather than a consequence of dictionary order.
RULES: dict[ClassificationRule, Callable[[_Evidence], bool]] = {
    ClassificationRule.REVERSAL_OF_BOOKED_DEBIT: _reversal_of_booked_debit,
    ClassificationRule.REVERSAL_OF_BOOKED_CREDIT_ACROSS_PERIODS: (
        _reversal_of_booked_credit_across_periods
    ),
    ClassificationRule.DEDUCTIONS_SPLIT_ACROSS_ROWS: _deductions_split_across_rows,
}


def _relate(
    subjects: Sequence[SettlementMovement], context: Sequence[SettlementMovement]
) -> list[_Evidence]:
    """Index the context by the key the rules reason over, then bind each subject to its group.

    The key is ``(merchant_reference, currency)`` and a ``None`` reference is **not** a key. Two
    lines the PSP passed no reference for are not related to each other by that absence, and
    grouping them would manufacture a relationship out of missing data — which is the one thing a
    control system must never do with a null.
    """
    by_key: collections.defaultdict[tuple[str, str], list[SettlementMovement]] = (
        collections.defaultdict(list)
    )
    seen: set[uuid.UUID] = set()
    for movement in context:
        if movement.id in seen:
            continue
        seen.add(movement.id)
        if movement.merchant_reference is not None:
            by_key[(movement.merchant_reference, movement.currency)].append(movement)

    evidence: list[_Evidence] = []
    for subject in subjects:
        related: tuple[SettlementMovement, ...] = ()
        if subject.merchant_reference is not None:
            key = (subject.merchant_reference, subject.currency)
            related = tuple(m for m in by_key.get(key, ()) if m.id != subject.id)
        evidence.append(_Evidence(subject=subject, related=related))
    return evidence


def classify(
    subjects: Iterable[SettlementMovement],
    context: Iterable[SettlementMovement],
) -> tuple[Classification, ...]:
    """Classify each residual line, or record that no rule could.

    ``subjects`` are the residual lines to classify; ``context`` is every settlement movement that
    might explain one, matched or not. The two may overlap freely — subjects are folded into the
    context here rather than at the call site, so a caller cannot produce a wrong answer by
    forgetting to include them.

    Every rule is evaluated for every subject, and the winner is the highest-precedence rule that
    fired. Evaluating all of them rather than returning at the first hit is what makes precedence
    inspectable: a rule cannot win by being written earlier in the file.

    Today no line can satisfy two rules, so the declared order decides nothing — a test sweeps the
    shapes that could collide and asserts it. That is deliberate: precedence orders the rules that
    *fire*, which is exactly the hole a rule declining on its last condition used to fall through
    (see :func:`_deductions_split_across_rows`), so the rule set is now built to have no overlap
    rather than to resolve one.

    Output order follows the subject order the caller supplied. The *decisions* do not depend on
    it — each subject is decided against a set — and a test asserts that by permuting the input.
    """
    subject_list = list(subjects)
    combined = [*context, *subject_list]

    results: list[Classification] = []
    for evidence in _relate(subject_list, combined):
        fired = [rule for rule in RULE_PRECEDENCE if RULES[rule](evidence)]
        winner = fired[0] if fired else ClassificationRule.NO_RULE_MATCHED
        results.append(
            Classification(
                line_id=evidence.subject.id,
                classification=RULE_CLASSIFICATION[winner],
                rule_id=winner,
            )
        )
    return tuple(results)
