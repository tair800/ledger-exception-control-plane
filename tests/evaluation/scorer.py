"""The scorer: what a set of proposals got right, and what the number is worth (M6.1).

`PROJECT_SPEC.md` §20: *"Scorer reports treatment-proposal accuracy, abstention rate, and confusion
across treatment codes."* All three are here. So are three things §20 does not name, each because
the run this harness can actually perform would otherwise be reported misleadingly.

**1. Accuracy is reported beside the baseline that makes it readable.** 214 of the golden set's 250
labels are ``ESCALATE``, and that imbalance is structural rather than a sampling artefact: only two
of the four reachable classes are priceable at all, so for the other two the correct action is to
refer the case to a human. A model that answers ``ESCALATE`` to every exception is therefore
**85.6% accurate while deciding nothing**. Reporting that figure alone would be the most flattering
number in the repository and the least informative, so :attr:`Score.majority_baseline` is computed
from the labels themselves and reported with it, along with the lift over it. A negative lift is a
model that is worse than a constant.

**2. The priceable classes are scored separately.** They are the only records where the proposal
changes what happens: a wrong answer there is a wrong financial instruction, while a wrong answer on
a fee split is a case a human looks at either way. :attr:`Score.accuracy_on_priceable` is the figure
a reader should care about, and it is computed over 36 of the 250 records.

**3. Abstention is split by whether escalating was correct.** §20 asks for one rate, and one rate is
uninterpretable on this set: abstaining on a fee split is right and abstaining on a chargeback
reversal is a refusal to do the job. The two are counted separately, and the second is the one worth
alarming about.

**The origin of the responses travels with every number.** The committed cassettes are
*synthesised*: their recorded bodies say "Not produced by a model." A score computed over them
measures this harness — that the pipeline runs, that the scorer arithmetic is right, that the gate
can fail — and says nothing whatever about a model's judgement.
:attr:`Score.measures_a_model` is ``False`` for such a run and :meth:`Score.headline` refuses to
describe it as model accuracy. `CLAUDE.md` §10 forbids inventing a metric; presenting a synthesised
run as an evaluation result would be exactly that.
"""

from __future__ import annotations

import collections
import dataclasses
import enum
from collections.abc import Iterable, Mapping
from typing import Final

from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.money import DEMO_ACCOUNT_POLICY
from tests.evaluation.golden import GoldenRecord, GoldenSet, classification_of

__all__ = [
    "CassetteOrigin",
    "Proposal",
    "Score",
    "score",
]


class CassetteOrigin(enum.StrEnum):
    """Where the responses a score was computed over came from.

    Not a boolean, because the third case matters: a run with **no** model in it at all — the
    deterministic arm of §20's three-arm comparison — is a real measurement of a real thing, and
    calling it "not captured" would lump it in with the synthesised cassettes it has nothing in
    common with.
    """

    #: Recorded from a live provider call. A score over these measures a model.
    CAPTURED = "captured"

    #: Written by the cassette builder, never sent anywhere. A score over these measures the
    #: harness. **This is what the repository currently commits.**
    SYNTHESISED = "synthesised"

    #: No model was involved. A score over these measures deterministic code, and means what it
    #: says.
    NO_MODEL = "no_model"


#: The origins whose scores are statements about a model's judgement.
MEASURES_A_MODEL: Final[frozenset[CassetteOrigin]] = frozenset({CassetteOrigin.CAPTURED})


@dataclasses.dataclass(frozen=True, slots=True)
class Proposal:
    """One answer to be graded, reduced to the two fields grading needs.

    Deliberately not ``TreatmentProposal``: the rationale, the confidence band and the evidence
    citations are provenance for humans and play no part in a score. Passing the full model object
    in would invite a future scorer to branch on free text, which §6.1 forbids outright.
    """

    exception_id: str
    treatment: TreatmentCode

    #: Whether the model declined to answer. §6.1 requires an abstaining proposal to carry
    #: ``ESCALATE``, so this is not derivable from the treatment: an ``ESCALATE`` that was *chosen*
    #: and one that was a refusal to choose are different answers with the same code.
    abstained: bool = False


def _is_priceable(record: GoldenRecord) -> bool:
    """Whether any treatment could post money for this record's class.

    Asked of the account policy rather than listed, for the same reason the label module asks it:
    configuring an account for ``fee_split`` tomorrow moves records into this group, and the
    scorer's headline figure should move with it.
    """
    classification = classification_of(record)
    return any(
        DEMO_ACCOUNT_POLICY.account_for(classification, treatment) is not None
        for treatment in TreatmentCode
        if treatment is not TreatmentCode.ESCALATE
    )


@dataclasses.dataclass(frozen=True, slots=True)
class Score:
    """What a set of proposals got right, and what that is worth knowing."""

    origin: CassetteOrigin
    scored: int
    correct: int

    #: The most common label's share of the set: the accuracy of answering it every time.
    majority_baseline: float
    majority_label: str

    #: Records whose class has a configured account, and the accuracy over just those.
    priceable: int
    correct_on_priceable: int

    abstentions: int
    abstained_where_escalation_was_correct: int
    abstained_where_a_treatment_was_available: int

    #: ``(expected, proposed) -> count``, over every treatment pair that occurred.
    confusion: Mapping[tuple[str, str], int]

    #: Golden records that no proposal answered, and proposals for records not in the set. Both are
    #: reported rather than ignored: a run that silently skipped half the set would otherwise score
    #: perfectly on the half it managed.
    unanswered: tuple[str, ...]
    unknown_ids: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        return self.correct / self.scored if self.scored else 0.0

    @property
    def accuracy_on_priceable(self) -> float:
        """The figure that matters: accuracy where the answer changes what happens."""
        return self.correct_on_priceable / self.priceable if self.priceable else 0.0

    @property
    def lift_over_baseline(self) -> float:
        """Accuracy minus the constant-answer baseline. Negative means worse than a constant."""
        return self.accuracy - self.majority_baseline

    @property
    def abstention_rate(self) -> float:
        return self.abstentions / self.scored if self.scored else 0.0

    @property
    def measures_a_model(self) -> bool:
        return self.origin in MEASURES_A_MODEL

    @property
    def is_complete(self) -> bool:
        """Whether every golden record was answered and nothing extra was submitted."""
        return not self.unanswered and not self.unknown_ids

    def headline(self) -> str:
        """One line, phrased according to what the run can support.

        The wording is the point. A synthesised run gets a sentence that cannot be quoted as an
        evaluation result, because someone will quote whatever this returns.
        """
        if self.origin is CassetteOrigin.SYNTHESISED:
            return (
                f"harness check over {self.scored} synthesised responses: "
                f"{self.accuracy:.1%} agreement with the golden labels. "
                "THIS IS NOT A MODEL MEASUREMENT — the responses were written by the cassette "
                "builder, not produced by a model."
            )
        if self.origin is CassetteOrigin.NO_MODEL:
            return (
                f"deterministic arm over {self.scored} exceptions: {self.accuracy:.1%} accuracy "
                f"({self.accuracy_on_priceable:.1%} on the {self.priceable} priceable), against a "
                f"{self.majority_baseline:.1%} constant-answer baseline. No model was involved."
            )
        return (
            f"treatment-proposal accuracy over {self.scored} exceptions: {self.accuracy:.1%} "
            f"({self.accuracy_on_priceable:.1%} on the {self.priceable} priceable), "
            f"{self.lift_over_baseline:+.1%} against a {self.majority_baseline:.1%} "
            f"constant-answer baseline; abstained {self.abstention_rate:.1%}, of which "
            f"{self.abstained_where_a_treatment_was_available} declined a priceable case."
        )


def score(golden: GoldenSet, proposals: Iterable[Proposal], *, origin: CassetteOrigin) -> Score:
    """Grade proposals against the golden set.

    ``origin`` is required and has no default. A default would be guessed by every caller that
    forgot it, and the one thing this scorer must never do is present a synthesised run as an
    evaluation result — so the caller states where the responses came from or does not get a score.
    """
    by_id = {record.exception_id: record for record in golden.records}
    submitted = {proposal.exception_id: proposal for proposal in proposals}

    labels = [record.expected_treatment for record in golden.records]
    majority_label, majority_count = (
        collections.Counter(labels).most_common(1)[0] if labels else ("", 0)
    )

    correct = priceable = correct_on_priceable = 0
    abstentions = abstained_right = abstained_wrong = 0
    confusion: collections.Counter[tuple[str, str]] = collections.Counter()

    for exception_id, record in by_id.items():
        proposal = submitted.get(exception_id)
        if proposal is None:
            continue

        answered = proposal.treatment.value
        confusion[(record.expected_treatment, answered)] += 1
        right = answered == record.expected_treatment
        correct += right

        if _is_priceable(record):
            priceable += 1
            correct_on_priceable += right

        if proposal.abstained:
            abstentions += 1
            if record.escalation_is_correct:
                abstained_right += 1
            else:
                abstained_wrong += 1

    return Score(
        origin=origin,
        scored=len(by_id.keys() & submitted.keys()),
        correct=correct,
        majority_baseline=majority_count / len(labels) if labels else 0.0,
        majority_label=majority_label,
        priceable=priceable,
        correct_on_priceable=correct_on_priceable,
        abstentions=abstentions,
        abstained_where_escalation_was_correct=abstained_right,
        abstained_where_a_treatment_was_available=abstained_wrong,
        confusion=dict(sorted(confusion.items())),
        unanswered=tuple(sorted(by_id.keys() - submitted.keys())),
        unknown_ids=tuple(sorted(submitted.keys() - by_id.keys())),
    )
