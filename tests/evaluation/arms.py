"""§20's three-arm comparison: deterministic matcher, LLM-as-matcher, shipped hybrid (M6.3).

§20 asks for three arms *"on the same set, reporting accuracy, USD per 1,000 lines and p95 per
arm"*, and adds the sentence that decides how this module is built: *"the expected result is that
the LLM matcher loses on all three; if it does not, the result is published unchanged."* A harness
that can only produce the expected answer has not tested anything, so nothing here is arranged to
make the model lose — and nothing here produces a number for a run that did not happen.

**What is measured today, and what is not.**

===========================  ====================================  ==========================
Arm                          Accuracy                              USD/1k lines and p95
===========================  ====================================  ==========================
deterministic matcher        **measured** (``no_model``)            **measured** (p95), 0 (USD)
LLM-as-matcher               NOT MEASURED — requires live capture   NOT MEASURED
shipped hybrid               deterministic half **measured**        NOT MEASURED
===========================  ====================================  ==========================

The reason is the same in every cell that says ``NOT MEASURED``: the committed cassettes are
*synthesised*, and a figure computed from them is a fact about this harness rather than about a
model. `CLAUDE.md` §10 forbids inventing a metric, so the cell says what is missing instead of
carrying a number that would be quoted without its caveat. :class:`Figure` has no way to express a
value without an origin, which is what stops the omission being filled in later by accident.

**Cost is computed from provider usage fields or it is not computed.** The committed cassettes carry
no ``usage`` block — deliberately, and a test in the M3.4 suite asserts the absence — because a
synthesised recording saying ``{"input_tokens": 0}`` reads as "this call was free" rather than
"nobody measured this call", and a fabricated zero ends up inside a published cost figure. There is
therefore no token count to price, and no cost number for either model-dependent arm.

**The deterministic arm's accuracy is pair precision, and that is a deliberate choice over a
per-line rate.** The corpus labels each *scenario* with a match intent, not each line with "should
this have matched", and the difference matters: several residual-intent scenarios contain a line
that legitimately corresponds to their own ledger entry — a chargeback and its later reversal
against one ledger debit — so scoring every residual-intent line as "should not match" would
understate the matcher against a truth the corpus never asserted. What the corpus does assert,
per pair, is whether both sides came from the same constructed scenario. So the accuracy reported
is **correct pairs / pairs produced**, over every decision the arm actually made, and recall on the
matchable set is reported beside it because precision alone can be bought by matching nothing.

**p95 is defined here rather than assumed.** The matcher is one pure call over the whole corpus, so
there is no per-line latency to sample without instrumenting a shipped module. What is sampled
instead is stated in the table's own notes: the full-corpus ``match`` call is run
:data:`LATENCY_RUNS` times in process, each run's duration divided by the line count, and the 95th
percentile of those per-line figures reported. It is wall clock on the machine that ran it, which
is why the table is generated on demand rather than committed and drift-checked.
"""

from __future__ import annotations

import dataclasses
import enum
import statistics
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from ledger_exception_control_plane.db.control import EvidenceKind
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import MatchIntent, Profile
from ledger_exception_control_plane.llm.cassette import load_cassette
from ledger_exception_control_plane.llm.evidence import (
    CandidateEntryFact,
    ExceptionSubject,
    evidence_id_for,
)
from ledger_exception_control_plane.llm.port import ProviderId
from ledger_exception_control_plane.llm.schema import TreatmentProposal
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    match,
)
from tests.cassette_builder import PROVIDERS
from tests.evaluation.golden import GOLDEN_INSTANCES, GOLDEN_PROFILE, GOLDEN_SEED
from tests.evaluation.replay import REPLAY_CASSETTE, origin_of
from tests.evaluation.scorer import CassetteOrigin
from tests.test_matching_precision import measure as measure_matcher_precision

__all__ = [
    "ARM_ORDER",
    "LATENCY_RUNS",
    "NOT_MEASURED",
    "Arm",
    "ArmResult",
    "Comparison",
    "Figure",
    "cited_entry_ids",
    "compare_arms",
    "grade_llm_pairings",
    "measure_deterministic_arm",
    "measure_hybrid_arm",
    "measure_llm_matcher_arm",
    "render_comparison",
]

#: The exact string every unmeasured cell carries. One spelling, so a reader scanning the table and
#: a test asserting the table cannot disagree about what "absent" looks like.
NOT_MEASURED: Final = "NOT MEASURED"

#: How many in-process runs of the full-corpus ``match`` call the p95 is taken over.
#:
#: Twenty rather than three, because a 95th percentile over three samples is the maximum with extra
#: steps; and rather than two hundred, because this runs in the default suite.
LATENCY_RUNS: Final = 20

#: The corpus the arms are compared on: the same seed, profile and size the golden set uses, so
#: "on the same set" is literally true rather than approximately true.
ARM_SEED: Final = GOLDEN_SEED
ARM_PROFILE: Final = GOLDEN_PROFILE
ARM_INSTANCES: Final = GOLDEN_INSTANCES


class Arm(enum.StrEnum):
    """The three arms §20 names. Closed, and in the order the table prints them."""

    DETERMINISTIC = "deterministic matcher"
    LLM_AS_MATCHER = "LLM-as-matcher"
    HYBRID = "shipped hybrid"


ARM_ORDER: Final = (Arm.DETERMINISTIC, Arm.LLM_AS_MATCHER, Arm.HYBRID)


@dataclasses.dataclass(frozen=True, slots=True)
class Figure:
    """One cell: either a number with its origin, or a stated reason there is none.

    **A value cannot be expressed without an origin**, and an absence cannot be expressed without a
    reason. That is the whole design of this class: the failure mode it exists to prevent is a
    number appearing in a published table because a default was easier than a decision.
    """

    unit: str
    value: float | None = None
    origin: CassetteOrigin | None = None
    why_absent: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and not self.why_absent:
            raise ValueError("a figure with no value must say why it has none")
        if self.value is not None and self.origin is None:
            raise ValueError(
                "a figure with a value must declare where it came from; an unattributed number "
                "in a published table is exactly what CLAUDE.md section 10 forbids"
            )
        if self.value is not None and self.why_absent:
            raise ValueError("a figure cannot both carry a value and explain its absence")

    @property
    def is_measured(self) -> bool:
        return self.value is not None

    def render(self) -> str:
        """The cell's text. ``NOT MEASURED`` where there is no number, never a blank or a dash."""
        if self.value is None:
            return NOT_MEASURED
        if self.unit == "%":
            return f"{self.value:.1%}"
        if self.unit == "USD":
            return f"{self.value:.4f}"
        return f"{self.value:.1f} {self.unit}"


@dataclasses.dataclass(frozen=True, slots=True)
class ArmResult:
    """One arm's three figures, plus the notes a reader needs to interpret them."""

    arm: Arm
    accuracy: Figure
    usd_per_1000_lines: Figure
    p95: Figure

    #: What this arm actually did, in one sentence.
    what_it_does: str

    #: Everything a reader needs that does not fit in a cell: the accuracy's definition, the
    #: secondary figures, and the reason for any absence.
    notes: tuple[str, ...]

    #: Settlement lines this arm was run over, or ``0`` for an arm that was not run. Carried on the
    #: result so the comparison does not have to regenerate a corpus to state its own size.
    lines: int = 0

    @property
    def is_fully_measured(self) -> bool:
        return all(
            figure.is_measured for figure in (self.accuracy, self.usd_per_1000_lines, self.p95)
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Comparison:
    """The three arms, and the corpus they were run over."""

    seed: int
    profile: str
    instances: int
    lines: int
    arms: tuple[ArmResult, ...]


# ======================================================================================
# Arm 1 — the deterministic matcher. Measured.
# ======================================================================================


def _matcher_inputs(
    profile: Profile, instances: int
) -> tuple[list[CandidateLine], list[CandidateEntry]]:
    """The matcher's inputs, built once so the timing loop measures ``match`` and nothing else.

    Corpus rows, in their own identifier namespace, and no construction metadata: this half of the
    arm is a latency measurement and has no business reading a label.
    """
    corpus = generate(ARM_SEED, profile, instances)
    lines = [
        CandidateLine(row.id, row.line_number, row.amount, row.currency, row.value_date)
        for batch in corpus.corpus.batches
        for row in batch.lines
    ]
    entries = [
        CandidateEntry(e.id, e.external_ref, e.amount, e.currency, e.booked_at.date())
        for e in corpus.corpus.ledger_entries
    ]
    return lines, entries


def _p95_microseconds_per_line(
    lines: Sequence[CandidateLine], entries: Sequence[CandidateEntry], runs: int
) -> float:
    """p95 of per-line matcher wall clock, in microseconds, over ``runs`` full-corpus calls."""
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        match(list(lines), list(entries), DEFAULT_POLICY)
        elapsed = time.perf_counter() - started
        samples.append(elapsed / len(lines) * 1_000_000)
    return statistics.quantiles(sorted(samples), n=20)[-1] if len(samples) > 1 else samples[0]


def measure_deterministic_arm(
    *, profile: Profile = ARM_PROFILE, instances: int = ARM_INSTANCES, runs: int = LATENCY_RUNS
) -> ArmResult:
    """Run the shipped matcher over the corpus and grade every pair it produced.

    Accuracy is delegated to :func:`tests.test_matching_precision.measure`, which is the module
    that owns grading the matcher against the corpus's construction intent. Reimplementing it here
    would have produced a second, subtly different definition of a correct pair — and the one that
    already exists is the one the M2.2 increment's claims rest on.
    """
    precision = measure_matcher_precision(profile, instances)
    lines, entries = _matcher_inputs(profile, instances)

    matchable = sum(
        count
        for scenario, count in precision.lines_by_scenario.items()
        if precision.intent_by_scenario[scenario] is MatchIntent.MATCHED
    )
    matchable_cleared = sum(
        precision.matched_by_scenario.get(scenario, 0)
        for scenario, intent in precision.intent_by_scenario.items()
        if intent is MatchIntent.MATCHED
    )
    recall = matchable_cleared / matchable if matchable else 0.0

    return ArmResult(
        arm=Arm.DETERMINISTIC,
        accuracy=Figure(
            unit="%",
            value=precision.correct / precision.matched if precision.matched else 0.0,
            origin=CassetteOrigin.NO_MODEL,
        ),
        usd_per_1000_lines=Figure(unit="USD", value=0.0, origin=CassetteOrigin.NO_MODEL),
        p95=Figure(
            unit="us/line",
            value=_p95_microseconds_per_line(lines, entries, runs),
            origin=CassetteOrigin.NO_MODEL,
        ),
        what_it_does=(
            "Pairs settlement lines with ledger entries by exact amount, then within the "
            "configured tolerance band, refusing anything mutually ambiguous. No model."
        ),
        notes=(
            f"Accuracy is pair precision: {precision.correct} correct of {precision.matched} "
            f"pairs produced over {precision.eligible} lines, with "
            f"{len(precision.false_matches)} cross-scenario pair(s).",
            f"Recall on the matchable set: {matchable_cleared} of {matchable} lines in "
            f"scenarios the corpus built to match ({recall:.1%}). Reported beside precision "
            "because precision alone can be bought by matching nothing.",
            f"{precision.ambiguous} line(s) refused as ambiguous and {precision.unmatched} left "
            "unmatched. A refusal is a decision, not a miss.",
            "USD is 0 because this arm issues no provider request. That is structural, not "
            "measured: compute and database cost are not measured here and are not claimed to "
            "be zero.",
            f"p95 is the 95th percentile of per-line wall clock over {runs} in-process runs of "
            "the full-corpus match call, on the machine that generated this table. It is not a "
            "service-level latency and does not include ingestion, persistence or HTTP.",
        ),
        lines=precision.eligible,
    )


# ======================================================================================
# Arm 2 — LLM-as-matcher. The code path exists; the number requires a live capture.
# ======================================================================================


def cited_entry_ids(
    subject: ExceptionSubject,
    candidates: Sequence[CandidateEntryFact],
    proposal: TreatmentProposal,
) -> tuple[uuid.UUID, ...]:
    """The ledger entries a proposal cited, resolved back from its evidence references.

    **This is what "LLM-as-matcher" means concretely in this system**, and it needed a decision
    rather than a new model call. §6.1 gives a proposal no field in which to name a ledger entry —
    deliberately, because a field like that is one step from a field naming an account. What it
    does have is ``evidence_refs``, and a cited ``candidate_ledger_entry`` item *is* an assertion
    that this entry explains this line. So the arm reads the citation as the pairing, which means
    it grades the shipped contract rather than a hypothetical wider one.

    Resolved by recomputing ``evidence_id_for`` over the candidates the subject was actually
    offered, so a reference to something that was not in the pack resolves to nothing rather than
    to a plausible guess.
    """
    lookup = {
        str(
            evidence_id_for(
                subject.exception_id, EvidenceKind.CANDIDATE_LEDGER_ENTRY, str(entry.entry_id)
            )
        ): entry.entry_id
        for entry in candidates
    }
    return tuple(
        lookup[reference.evidence_id]
        for reference in proposal.evidence_refs
        if reference.evidence_id in lookup
    )


def grade_llm_pairings(
    pairings: Mapping[uuid.UUID, Sequence[uuid.UUID]],
    scenario_of_line: Mapping[uuid.UUID, str],
    scenario_of_entry: Mapping[uuid.UUID, str],
) -> tuple[int, int]:
    """``(pairs asserted, pairs correct)`` for a set of model-asserted pairings.

    Graded the same way the deterministic arm is: a pair is correct exactly when both sides came
    from the same constructed scenario. Same definition, so the two arms' accuracy figures are
    comparable — which is the only reason a three-arm table is worth printing.

    Pure, and takes the truth as an argument rather than reaching for it, so the grading can be
    exercised without a corpus and cannot quietly become the thing that produces a number for a
    run that did not happen.
    """
    asserted = correct = 0
    for line_id, entry_ids in pairings.items():
        for entry_id in entry_ids:
            asserted += 1
            if scenario_of_line.get(line_id) == scenario_of_entry.get(entry_id):
                correct += 1
    return asserted, correct


def measure_llm_matcher_arm() -> ArmResult:
    """The LLM-as-matcher arm. **Reports no number until a captured cassette exists.**

    The path is built: :func:`cited_entry_ids` resolves a proposal's citations into pairings and
    :func:`grade_llm_pairings` grades them against the same truth the deterministic arm is graded
    against. What is missing is a run worth grading. The committed cassettes are synthesised and
    their citations are ``evidence_ids[:1]`` — the first item of the pack, which is the remittance
    reference rather than any candidate entry — so grading them would measure the cassette
    builder's slice syntax.

    Cost is absent for a second, independent reason: cost is computed from provider usage fields
    and the cassettes carry none. Both reasons are stated in the notes, because either one alone
    would leave a reader wondering whether the other had been overlooked.
    """
    origin = origin_of(load_cassette(REPLAY_CASSETTE), _any_provider())
    absent = (
        f"requires live capture. The committed cassettes are {origin.value}, and a pairing "
        "graded from them would measure the cassette builder rather than a model."
    )
    return ArmResult(
        arm=Arm.LLM_AS_MATCHER,
        accuracy=Figure(unit="%", why_absent=absent),
        usd_per_1000_lines=Figure(
            unit="USD",
            why_absent=(
                "cost is computed from provider usage fields and never estimated. The committed "
                "cassettes carry no usage block, deliberately, so there is no token count to "
                "price and therefore no number."
            ),
        ),
        p95=Figure(
            unit="us/line",
            why_absent=(
                "a replayed cassette returns in microseconds and a provider round trip does not. "
                "Reporting replay latency as this arm's p95 would be the most misleading number "
                "available."
            ),
        ),
        what_it_does=(
            "Asks the model, per residual line, which unconsumed ledger entry explains it, and "
            "reads the cited candidate_ledger_entry evidence item as the asserted pairing. §6.1 "
            "gives a proposal no field for a ledger entry, so the citation is the channel."
        ),
        notes=(
            absent,
            "The arm's code path is implemented and unit-tested: cited_entry_ids resolves "
            "citations to entries, grade_llm_pairings grades them against the same "
            "same-scenario truth the deterministic arm is graded against.",
            "§20 expects this arm to lose on all three figures. That expectation is written down "
            "and is not enforced anywhere in this harness: if a capture shows otherwise, the "
            "result is published unchanged.",
        ),
    )


# ======================================================================================
# Arm 3 — the shipped hybrid. Deterministic half measured, model half not.
# ======================================================================================


def measure_hybrid_arm(
    *,
    deterministic: ArmResult | None = None,
    profile: Profile = ARM_PROFILE,
    instances: int = ARM_INSTANCES,
    runs: int = LATENCY_RUNS,
) -> ArmResult:
    """The arm the system actually ships: the matcher clears the bulk, the model treats the rest.

    Its pairing accuracy **is** the deterministic arm's, because it is the same matcher on the same
    corpus — reported as the same *object* rather than re-measured, so the two cannot drift apart
    and a reader is not invited to read a difference into sampling noise. What the hybrid adds is
    the treatment proposal on the residual, and that is the half no number exists for.

    ``deterministic`` is accepted so the comparison can pass in the result it already has. Without
    it this would re-run the whole matcher benchmark, and the second run's timing would legitimately
    differ from the first while describing the same code.
    """
    deterministic = deterministic or measure_deterministic_arm(
        profile=profile, instances=instances, runs=runs
    )
    origin = origin_of(load_cassette(REPLAY_CASSETTE), _any_provider())

    return ArmResult(
        arm=Arm.HYBRID,
        accuracy=deterministic.accuracy,
        usd_per_1000_lines=Figure(
            unit="USD",
            why_absent=(
                "the deterministic half issues no provider request; the treatment-proposal half "
                "does, and its cost is computed from usage fields the committed cassettes do not "
                "carry. A total that counted only the free half would read as the total."
            ),
        ),
        p95=Figure(
            unit="us/line",
            why_absent=(
                "end to end this arm includes a provider round trip, so the matcher's p95 is not "
                "this arm's p95. The matcher half is measured and reported in the deterministic "
                "row above."
            ),
        ),
        what_it_does=(
            "What the system ships: the deterministic matcher clears what it can, the residual is "
            "classified, and the model proposes a treatment code for it — never an amount — for a "
            "human to approve."
        ),
        notes=(
            "Pairing accuracy is the deterministic arm's figure, unchanged: the hybrid uses the "
            "same matcher on the same corpus, so re-measuring it would only invite a reader to "
            "read meaning into two numbers that describe one thing.",
            f"Treatment-proposal accuracy on the residual: {NOT_MEASURED}. The committed "
            f"cassettes are {origin.value}; the offline replay of that path is gated for "
            "reproduction by tests/golden/replay-baseline.json, which is not a quality "
            "measurement and does not become one by being cited here.",
            "The model's channel into the money path is a four-member treatment enum. It never "
            "computes, alters or encodes an amount, so no figure in this row is a claim about a "
            "monetary value the model produced.",
        ),
    )


def _any_provider() -> ProviderId:
    """The provider whose declared origin describes the committed cassette.

    The file holds both, recorded from the same builder in the same run, so either answers the
    question "was this captured or synthesised". Taking the first in the builder's own order rather
    than naming one here, so adding a third provider does not silently pin this to a stale pair.
    """
    return PROVIDERS[0][0]


# ======================================================================================
# The comparison and its rendering
# ======================================================================================


def compare_arms(
    *, profile: Profile = ARM_PROFILE, instances: int = ARM_INSTANCES, runs: int = LATENCY_RUNS
) -> Comparison:
    """All three arms, on one corpus.

    The matcher benchmark runs once and the hybrid is handed the result. Running it twice would
    produce two timings for one piece of code and invite a reader to compare them.
    """
    deterministic = measure_deterministic_arm(profile=profile, instances=instances, runs=runs)
    return Comparison(
        seed=ARM_SEED,
        profile=profile.value,
        instances=instances,
        lines=deterministic.lines,
        arms=(
            deterministic,
            measure_llm_matcher_arm(),
            measure_hybrid_arm(deterministic=deterministic),
        ),
    )


def render_comparison(comparison: Comparison) -> str:
    """The markdown §20 asks to be published. ``NOT MEASURED`` in every cell without a run.

    Written as markdown rather than as a printed table because it is meant to be pasted into the
    README beside the §19 results, and a table that has to be reformatted to be published is a
    table that gets retyped — at which point the ``NOT MEASURED`` cells are the first thing to go.
    """
    header = [
        "| Arm | Accuracy | USD / 1,000 lines | p95 | Origin |",
        "|---|---|---|---|---|",
    ]
    rows = []
    for result in comparison.arms:
        origins = {
            figure.origin.value
            for figure in (result.accuracy, result.usd_per_1000_lines, result.p95)
            if figure.origin is not None
        }
        rows.append(
            f"| {result.arm.value} | {result.accuracy.render()} | "
            f"{result.usd_per_1000_lines.render()} | {result.p95.render()} | "
            f"{'/'.join(sorted(origins)) if origins else NOT_MEASURED} |"
        )

    notes = []
    for result in comparison.arms:
        notes.append(f"**{result.arm.value}.** {result.what_it_does}")
        notes.extend(f"- {note}" for note in result.notes)
        notes.append("")

    return (
        "\n".join(
            [
                f"Corpus: seed {comparison.seed}, profile `{comparison.profile}`, "
                f"{comparison.instances} scenario instances, {comparison.lines} settlement lines. "
                "Regenerate with `make eval-compare`.",
                "",
                *header,
                *rows,
                "",
                f"`{NOT_MEASURED}` is not a placeholder for a number somebody forgot. Cost is "
                "computed from provider usage fields or not at all, and the committed cassettes "
                "are synthesised and carry none — so the two model-dependent arms have no cost, "
                "no latency and no accuracy that would mean anything. A live capture is the only "
                "thing that fills those cells, and it needs a credential this repository does "
                "not hold.",
                "",
                *notes,
            ]
        ).rstrip()
        + "\n"
    )
