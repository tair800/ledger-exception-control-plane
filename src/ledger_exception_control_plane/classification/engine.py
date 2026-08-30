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
    MovementType,
    accounting_period,
)
from ledger_exception_control_plane.db.control import ExceptionClassification


@dataclasses.dataclass(frozen=True, slots=True)
class SettlementMovement:
    """A settlement line as the classifier sees it. Deliberately not the ORM row.

    Seven fields, and none of them from the ledger: no entry, no account, no description, and no
    free text of any kind. ``matched`` is the whole of what M2.2 concluded about this line, which
    is all any rule needs — whether the ledger already carries this movement.

    ``movement`` is what the PSP *declared* this row to be, reduced to a closed vocabulary. It is
    the field that makes the taxonomy provable rather than guessed: every substantive class in FR-4
    names a kind of movement, and without the kind a classifier can only read the sign of the
    amount — which cannot tell a chargeback reversal from a fee reversal or an operational
    correction. ``None`` means the file stated nothing, the row predates the column, or the type is
    one this system does not recognise; all three mean *no evidence*, and no rule fires on it.
    """

    id: uuid.UUID
    merchant_reference: str | None
    movement: MovementType | None
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

    def booked_offsets_of_type(self, kind: MovementType) -> tuple[SettlementMovement, ...]:
        """Reconciled movements of a **declared kind** that this one exactly reverses.

        Three conditions, all load-bearing. ``matched`` says the ledger carries the movement being
        reversed — without it the pair is two unreconciled rows and there is nothing booked to
        reverse. ``kind`` says what that booked movement *was*, which is the evidence this module
        previously did not have and could not infer: a credit reversing a booked debit is equally a
        chargeback reversal, a fee reversal or a correction, and the sign cannot separate them.

        Exact ``Decimal`` equality, not a band. A reversal that does not restore the original amount
        is not evidence of a reversal, and the one tolerance policy in this system belongs to
        matching (ADR-042) — reusing it here would apply a band to a question nobody decided it for.
        """
        return tuple(
            movement
            for movement in self.related
            if movement.matched
            and movement.movement is kind
            and movement.amount == -self.subject.amount
        )

    def any_booked_offset(self) -> bool:
        """Whether *any* reconciled movement on this order exactly reverses the subject.

        Deliberately blind to the declared kind, unlike the method above. It answers "does the
        reversal family have a claim on this line", which is what keeps an unresolved reversal
        question away from the group rule — see :func:`_fees_deducted_from_a_capture`.
        """
        return any(
            movement.matched and movement.amount == -self.subject.amount
            for movement in self.related
        )

    def unreconciled_group(self) -> tuple[SettlementMovement, ...]:
        """The subject together with every related movement the ledger has not reconciled."""
        return (self.subject, *(m for m in self.related if not m.matched))


def _reversal_of_booked_chargeback(evidence: _Evidence) -> bool:
    """A declared chargeback reversal that restores a chargeback the ledger already booked.

    Three pieces of evidence, and the class is asserted only when all three agree:

    * the PSP declared **this** row a ``chargeback_reversal``;
    * exactly one movement on the same order is a declared ``chargeback`` the ledger reconciled;
    * that chargeback is the exact negation of this credit.

    **Direction alone used to be enough here, and that was the defect.** A positive residual
    reversing a booked debit was called a chargeback reversal whatever either row actually was, so
    an ordinary refund reversal and an operational correction received the same class — two false
    statements in a control record that a treatment, an approval and a posting all rest on. Proven
    by ingesting three credits identical in sign, amount, currency and date, differing only in
    declared type: all three came back ``chargeback_reversal``.

    Requiring *exactly one* corroborating chargeback is the discipline M2.2 applies to candidate
    entries. Two make the reversal unprovable, not twice as likely.
    """
    if evidence.subject.movement is not MovementType.CHARGEBACK_REVERSAL:
        return False
    if evidence.subject.amount <= 0:
        return False
    return len(evidence.booked_offsets_of_type(MovementType.CHARGEBACK)) == 1


def _refund_of_booked_capture_across_periods(evidence: _Evidence) -> bool:
    """A declared refund reversing a booked capture, settling in a later accounting period.

    The same correction applies as above, for the same reason: a debit reversing a booked credit is
    equally a refund, a chargeback, a clawback or a correction, and direction cannot tell them
    apart. The PSP's declaration does, on both sides — this row is a ``refund`` and the movement it
    reverses is a ``capture``.

    The period test is what makes this the taxonomy's ``cross_period_refund`` rather than a refund
    in general. The taxonomy has no class for a refund settling in its own period, so one falls to
    the fallback rather than borrowing this label. Periods are calendar months read from the two
    business dates; no clock is consulted, and no posting period is assigned here.
    """
    if evidence.subject.movement is not MovementType.REFUND:
        return False
    if evidence.subject.amount >= 0:
        return False
    offsets = evidence.booked_offsets_of_type(MovementType.CAPTURE)
    if len(offsets) != 1:
        return False
    return accounting_period(offsets[0].value_date) != accounting_period(
        evidence.subject.value_date
    )


def _fees_deducted_from_a_capture(evidence: _Evidence) -> bool:
    """A capture and its fees, reported on separate rows, none of which reconciled.

    A fee split is a gross amount reduced by deductions the ledger booked as a single net entry, so
    no individual row equals any individual entry — which is why every row of it survives matching.
    The evidence is now declarative rather than shape-based: the PSP says this row is a ``capture``
    or a ``fee``, and the same order carries at least one unreconciled row of each.

    The magnitude test is kept as corroboration rather than as the rule. It is what a deduction *is*
    — a fee comes out of a capture and cannot exceed it — and it catches a group whose declared
    types agree but whose numbers do not.

    **A line the reversal family has a claim on is not available here**, whether or not that family
    reached a conclusion. Precedence orders the rules that *fire*, so a higher-priority rule that
    examines a line and then declines would otherwise leave it to be settled by this one: an
    unresolved claim settled by a weaker rule, which is the defect ADR-043 corrected for matching
    tiers and this guard corrects for classification rules.
    """
    if evidence.subject.movement not in {MovementType.CAPTURE, MovementType.FEE}:
        return False
    if evidence.any_booked_offset():
        return False

    group = evidence.unreconciled_group()
    kinds = {m.movement for m in group}
    if not {MovementType.CAPTURE, MovementType.FEE} <= kinds:
        return False

    inflows = [m.amount for m in group if m.movement is MovementType.CAPTURE and m.amount > 0]
    deductions = [-m.amount for m in group if m.movement is MovementType.FEE and m.amount < 0]
    if not inflows or not deductions:
        return False
    return sum(deductions) < max(inflows)


#: The rules themselves, keyed by their stable identifier. Applied in
#: :data:`~..taxonomy.RULE_PRECEDENCE`, which is declared separately so precedence is a statement
#: rather than a consequence of dictionary order.
RULES: dict[ClassificationRule, Callable[[_Evidence], bool]] = {
    ClassificationRule.REVERSAL_OF_BOOKED_CHARGEBACK: _reversal_of_booked_chargeback,
    ClassificationRule.REFUND_OF_BOOKED_CAPTURE_ACROSS_PERIODS: (
        _refund_of_booked_capture_across_periods
    ),
    ClassificationRule.FEES_DEDUCTED_FROM_A_CAPTURE: _fees_deducted_from_a_capture,
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
    (see :func:`_fees_deducted_from_a_capture`), so the rule set is now built to have no overlap
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
