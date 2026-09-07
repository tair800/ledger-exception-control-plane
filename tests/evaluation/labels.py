"""What the correct treatment is for a classified residual, and why (M6.1).

This module is the **label declaration**: the one place that says, for each condition the classifier
can reach, which member of the closed treatment set is the right answer. Everything else in the
evaluation harness reads it.

**It derives labels from production facts, never from the corpus's own construction metadata.** The
scenario id and the intended classification are not inputs here. That matters because the golden set
grades a *model's* proposal against what the *system's* deterministic layers concluded, and a label
read off the answer key would grade the model against a fact it was never shown. The corpus decides
which cases exist; this module decides what the right answer is for the case as classified.

**Only four conditions are reachable, and only two of them are priceable at all.** That asymmetry is
the whole shape of the golden set, and it comes from two committed tables rather than from a view
taken here:

===========================  ===========================================  =======================
Classification               What ``DEMO_ACCOUNT_POLICY`` configures      Correct treatment
===========================  ===========================================  =======================
``chargeback_reversal``      ``REBOOK``/``ACCRUE`` -> 4900, ``WRITE_OFF``  ``REBOOK``
``cross_period_refund``      ``REBOOK``/``ACCRUE`` -> 4100, ``WRITE_OFF``  ``ACCRUE`` or ``REBOOK``
``fee_split``                **nothing, deliberately**                     ``ESCALATE``
``unclassified``             **nothing, deliberately**                     ``ESCALATE``
===========================  ===========================================  =======================

``partial_capture`` and ``fx_rounding`` are members of the taxonomy that **no exception can carry**:
both are claims about a residual's relationship to one particular ledger entry, and M2.3 assigns
neither (ADR-045). They get no label, and a test asserts the corpus produces none — because a label
for a class nothing produces is a row of the golden set that can never be scored.

**Two of the four correct answers are ``ESCALATE``, and that is the interesting part.** §20 asks the
scorer to report an abstention rate, and on this corpus a *high* rate is correct rather than
disappointing: for a fee split and for an unexplained residual, referring the case to a human is the
right action, and the account policy's silence is what says so. Any harness reporting one
undifferentiated abstention number would make that uninterpretable, which is why
:mod:`tests.evaluation.scorer` splits it by whether escalation was in fact correct.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Final

from ledger_exception_control_plane.classification.taxonomy import (
    RULE_CLASSIFICATION,
    ClassificationRule,
)
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.money import DEMO_ACCOUNT_POLICY

__all__ = [
    "REACHABLE_CLASSIFICATIONS",
    "UNREACHABLE_CLASSIFICATIONS",
    "Label",
    "LabelRule",
    "LabelSource",
    "label_for",
]


class LabelSource(enum.StrEnum):
    """Where a golden label came from. Recorded on every record, never inferred.

    §20 requires *"a human-labelled hold-out slice"*, so the difference between a label this module
    derived and a label a person confirmed has to survive into the artefact. A single boolean would
    have done; an enum is used because a third source (a second reviewer, an auditor) is a plausible
    addition and a boolean would have to be replaced rather than extended.
    """

    #: Produced by :func:`label_for` from the classification and the account policy. Reviewable by
    #: reading this module, and reproducible by running the generator.
    DERIVED = "derived"

    #: Confirmed or corrected by a person, recorded with who and when. **No record in the committed
    #: golden set carries this yet** — see the hold-out slice's own note.
    HUMAN = "human"


class LabelRule(enum.StrEnum):
    """Which clause of this module produced a label. Carried on the record, so a disputed label
    can be argued with by name instead of by re-deriving it."""

    #: A reversal is recognised when it occurs, in the period it settled.
    REVERSAL_RECOGNISED_WHEN_IT_OCCURS = "reversal_recognised_when_it_occurs"

    #: A refund of a prior-period capture belongs to the period of the capture it reverses, and
    #: that period is known.
    REFUND_ACCRUES_TO_THE_PERIOD_IT_REVERSES = "refund_accrues_to_the_period_it_reverses"

    #: The same refund, with no counterpart period established: there is nothing to accrue into.
    REFUND_WITHOUT_A_COUNTERPART_PERIOD = "refund_without_a_counterpart_period"

    #: The account policy configures nothing for this class, deliberately.
    NOTHING_IS_CONFIGURED_FOR_THIS_CLASS = "nothing_is_configured_for_this_class"


@dataclasses.dataclass(frozen=True, slots=True)
class Label:
    """The correct answer for one classified residual, with the clause that decided it."""

    treatment: TreatmentCode
    rule: LabelRule
    source: LabelSource

    #: One sentence a reviewer can disagree with. Written per clause, not per record.
    why: str

    @property
    def escalation_is_correct(self) -> bool:
        """Whether referring this case to a human is the right action.

        Read by the scorer to split the abstention rate. Derived rather than stored, because a
        record carrying both could disagree with itself.
        """
        return self.treatment is TreatmentCode.ESCALATE


#: The classes an exception can actually carry, taken from the rule table rather than listed here.
#:
#: Derived from ``RULE_CLASSIFICATION`` so that adding a classification rule extends this set
#: automatically and immediately fails :func:`label_for` with a named error, rather than silently
#: producing an unlabelled row.
REACHABLE_CLASSIFICATIONS: Final[frozenset[ExceptionClassification]] = frozenset(
    RULE_CLASSIFICATION[rule] for rule in ClassificationRule
)

#: The taxonomy members no exception can carry. Named, because a label for one would be unscorable.
UNREACHABLE_CLASSIFICATIONS: Final[frozenset[ExceptionClassification]] = (
    frozenset(ExceptionClassification) - REACHABLE_CLASSIFICATIONS
)


def _nothing_is_priceable(classification: ExceptionClassification) -> bool:
    """Whether the account policy configures no treatment at all for this class.

    Asked of the policy rather than hard-coded, so the two cannot drift: configuring an account for
    ``fee_split`` tomorrow changes the correct answer here, and it should.
    """
    return all(
        DEMO_ACCOUNT_POLICY.account_for(classification, treatment) is None
        for treatment in TreatmentCode
        if treatment is not TreatmentCode.ESCALATE
    )


def label_for(classification: ExceptionClassification, *, originating_period: str | None) -> Label:
    """The correct treatment for one classified residual.

    ``originating_period`` is the period of the movement this one reverses, where the classification
    established exactly one counterpart. It is the same fact M2.4 requires as an input and does not
    look up, and it is the only thing that distinguishes the two cross-period-refund labels — which
    is why the label is computed per exception rather than per class.

    Raises for a class no exception can carry, and for one this module has no clause for. Both are
    refusals rather than fallbacks: a golden set with an unlabelled row would score every model
    identically on it, and a default label would be this module guessing on an accountant's behalf.
    """
    if classification in UNREACHABLE_CLASSIFICATIONS:
        raise ValueError(
            f"{classification.value} is in the taxonomy but no exception can carry it "
            "(ADR-045), so a golden label for it could never be scored"
        )

    if _nothing_is_priceable(classification):
        return Label(
            treatment=TreatmentCode.ESCALATE,
            rule=LabelRule.NOTHING_IS_CONFIGURED_FOR_THIS_CLASS,
            source=LabelSource.DERIVED,
            why=(
                f"the account policy configures no account for {classification.value} under any "
                "treatment, deliberately, so there is nothing to post and the correct action is to "
                "refer the case to a human"
            ),
        )

    if classification is ExceptionClassification.CHARGEBACK_REVERSAL:
        return Label(
            treatment=TreatmentCode.REBOOK,
            rule=LabelRule.REVERSAL_RECOGNISED_WHEN_IT_OCCURS,
            source=LabelSource.DERIVED,
            why=(
                "a chargeback reversal is an event of the period it settled in, not of the period "
                "of the chargeback it reverses, so the movement the ledger is missing is posted "
                "where it occurred rather than accrued backwards"
            ),
        )

    if classification is ExceptionClassification.CROSS_PERIOD_REFUND:
        if originating_period is None:
            return Label(
                treatment=TreatmentCode.REBOOK,
                rule=LabelRule.REFUND_WITHOUT_A_COUNTERPART_PERIOD,
                source=LabelSource.DERIVED,
                why=(
                    "no single counterpart movement was established, so no originating period is "
                    "known; ACCRUE would have nothing to accrue into and refuses, which leaves "
                    "recognising the movement in the period it settled"
                ),
            )
        return Label(
            treatment=TreatmentCode.ACCRUE,
            rule=LabelRule.REFUND_ACCRUES_TO_THE_PERIOD_IT_REVERSES,
            source=LabelSource.DERIVED,
            why=(
                "the refund reverses a capture recognised in an earlier period and that period is "
                f"known ({originating_period}), so the reduction in revenue belongs there rather "
                "than in the period the refund happened to settle"
            ),
        )

    raise ValueError(  # pragma: no cover - every reachable class is handled above
        f"{classification.value} is reachable but this module declares no label clause for it; "
        "add one rather than letting the golden set default"
    )
