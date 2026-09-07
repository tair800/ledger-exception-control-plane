"""M6.3 — the three-arm comparison, and the numbers it refuses to produce.

§20 wants deterministic matcher, LLM-as-matcher and shipped hybrid *"on the same set, reporting
accuracy, USD per 1,000 lines and p95 per arm"*, with the result *"published unchanged"* even if the
model wins. Two of those arms cannot be measured from this repository, because the committed
cassettes are synthesised and carry no token usage. So the interesting tests here are the negative
ones: that the harness says ``NOT MEASURED`` rather than producing something, and that it is
structurally unable to produce a number without saying where it came from.

The arm that *is* measured is measured properly, and its accuracy is defined rather than assumed —
pair precision, because the corpus labels scenarios with a match intent and not lines with "should
this have matched", and several residual-intent scenarios legitimately contain a line that does
correspond to their own ledger entry.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import uuid

import pytest

from ledger_exception_control_plane.db.control import (
    ConfidenceBand,
    EvidenceKind,
    ExceptionClassification,
    TreatmentCode,
)
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.llm.cassette import Cassette, Origin, load_cassette
from ledger_exception_control_plane.llm.evidence import (
    CandidateEntryFact,
    EvidenceItem,
    ExceptionSubject,
    assemble_evidence,
    evidence_id_for,
)
from ledger_exception_control_plane.llm.port import ProviderId
from ledger_exception_control_plane.llm.schema import EvidenceRef, TreatmentProposal
from ledger_exception_control_plane.matching import DEFAULT_POLICY
from tests.cassette_builder import corpus_subjects
from tests.evaluation import __main__ as cli
from tests.evaluation.arms import (
    ARM_INSTANCES,
    ARM_ORDER,
    ARM_PROFILE,
    ARM_SEED,
    NOT_MEASURED,
    Arm,
    Figure,
    cited_entry_ids,
    compare_arms,
    grade_llm_pairings,
    measure_deterministic_arm,
    measure_hybrid_arm,
    measure_llm_matcher_arm,
    render_comparison,
)
from tests.evaluation.golden import GOLDEN_INSTANCES, GOLDEN_PROFILE, GOLDEN_SEED
from tests.evaluation.replay import REPLAY_CASSETTE, origin_of
from tests.evaluation.scorer import CassetteOrigin

#: A corpus small enough to run three times in the default suite. The published table uses the
#: golden set's own size; a separate test asserts that the defaults are that size.
FAST = {"profile": Profile.BULK, "instances": 200, "runs": 3}


# ======================================================================================
# A figure cannot exist without provenance
# ======================================================================================


def test_a_figure_with_a_value_must_declare_where_it_came_from() -> None:
    """**The structural guard, and the reason this class exists at all.**

    The failure mode is a number reaching a published table because a default was easier than a
    decision. There is no default: a value without an origin is a construction error.
    """
    with pytest.raises(ValueError, match="declare where it came from"):
        Figure(unit="%", value=0.99)

    assert Figure(unit="%", value=0.99, origin=CassetteOrigin.NO_MODEL).is_measured


def test_a_figure_with_no_value_must_say_why() -> None:
    """An empty cell is a question a reader will answer for themselves, usually generously."""
    with pytest.raises(ValueError, match="must say why"):
        Figure(unit="%")

    absent = Figure(unit="%", why_absent="requires live capture")
    assert absent.is_measured is False
    assert absent.render() == NOT_MEASURED


def test_a_figure_cannot_both_carry_a_value_and_explain_its_absence() -> None:
    """A cell that does both would render as the value and read as measured."""
    with pytest.raises(ValueError, match="cannot both"):
        Figure(
            unit="%",
            value=0.5,
            origin=CassetteOrigin.NO_MODEL,
            why_absent="requires live capture",
        )


def test_a_figure_is_frozen() -> None:
    """A published measurement that later code can edit is not a measurement."""
    figure = Figure(unit="%", value=1.0, origin=CassetteOrigin.NO_MODEL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        figure.value = 0.0  # type: ignore[misc]


# ======================================================================================
# Arm 1 — measured, and its accuracy is defined
# ======================================================================================


def test_the_deterministic_arm_is_measured_and_carries_no_model_origin() -> None:
    result = measure_deterministic_arm(**FAST)  # type: ignore[arg-type]

    assert result.arm is Arm.DETERMINISTIC
    assert result.is_fully_measured
    for figure in (result.accuracy, result.usd_per_1000_lines, result.p95):
        assert figure.origin is CassetteOrigin.NO_MODEL, "no model was involved; say so"
    assert result.lines == 215


def test_the_deterministic_arms_accuracy_is_pair_precision_and_is_one() -> None:
    """The matcher produces no cross-scenario pair, so its precision is 1.0 — and that is the
    figure §20's accuracy column carries for this arm, with recall reported beside it.

    Asserted as a value rather than as a range, because M2.2's own suite asserts zero false matches
    as a count at four corpus sizes. If that ever stops being true this fails here too, which is
    the right place for a comparison table to notice.
    """
    result = measure_deterministic_arm(**FAST)  # type: ignore[arg-type]

    assert result.accuracy.value == 1.0
    assert result.accuracy.render() == "100.0%"
    assert any("pair precision" in note for note in result.notes)
    assert any("Recall on the matchable set" in note for note in result.notes), (
        "precision alone can be bought by matching nothing; the recall must be beside it"
    )


def test_the_deterministic_arms_cost_is_zero_and_says_why_it_is_zero() -> None:
    """Zero because no request is issued, not because somebody measured a bill of zero.

    The distinction matters: a reader who takes this as a measured total cost would also take it as
    a claim that running the system is free, which nothing here measured.
    """
    result = measure_deterministic_arm(**FAST)  # type: ignore[arg-type]

    assert result.usd_per_1000_lines.value == 0.0
    assert any(
        "structural, not measured" in note and "not claimed to" in note for note in result.notes
    )


def test_the_deterministic_arms_p95_is_positive_and_its_method_is_stated() -> None:
    """A latency figure whose method is not stated is not reproducible, whatever its value."""
    result = measure_deterministic_arm(**FAST)  # type: ignore[arg-type]

    assert result.p95.value is not None and result.p95.value > 0
    assert result.p95.unit == "us/line"
    assert any("95th percentile of per-line wall clock" in note for note in result.notes)
    assert any("does not include ingestion, persistence or HTTP" in note for note in result.notes)


# ======================================================================================
# Arm 2 — NOT MEASURED, in every cell, for stated reasons
# ======================================================================================


def test_the_llm_matcher_arm_reports_no_number_at_all() -> None:
    """**The arm §20 most wants a number for, and the one this repository cannot supply.**"""
    result = measure_llm_matcher_arm()

    assert result.arm is Arm.LLM_AS_MATCHER
    assert result.is_fully_measured is False
    for figure in (result.accuracy, result.usd_per_1000_lines, result.p95):
        assert figure.value is None
        assert figure.origin is None
        assert figure.render() == NOT_MEASURED
        assert figure.why_absent


def test_the_llm_matcher_arms_absences_each_name_their_own_reason() -> None:
    """Three cells, three different reasons. One blanket "not measured" would hide two of them."""
    result = measure_llm_matcher_arm()

    assert "requires live capture" in (result.accuracy.why_absent or "")
    assert "usage fields" in (result.usd_per_1000_lines.why_absent or "")
    assert "never estimated" in (result.usd_per_1000_lines.why_absent or "")
    assert "round trip" in (result.p95.why_absent or "")


def test_the_committed_cassettes_carry_no_usage_block_so_cost_cannot_be_computed() -> None:
    """The reason the cost cell is empty, asserted against the artefact rather than described.

    A synthesised recording carrying ``{"input_tokens": 0}`` would read as "this call was free"
    rather than "nobody measured this call", and a fabricated zero would end up inside a published
    cost figure.
    """
    cassette = load_cassette(REPLAY_CASSETTE)
    for interaction in cassette.interactions:
        assert "usage" not in interaction.response
        assert not any("token" in key.lower() for key in interaction.response), (
            f"{interaction.cassette_id[:12]} carries something token-shaped"
        )


def test_the_synthesised_cassettes_cite_nothing_the_arm_could_grade() -> None:
    """**Why NOT MEASURED is the honest answer rather than a cautious one.**

    The cassette builder cites ``evidence_ids[:1]``, which is the remittance-reference item — the
    subject's own settlement line, never a candidate ledger entry. So there is not merely a weak
    pairing to grade here, there is no pairing at all, and a zero computed from it would be a
    fabricated measurement of a model that never answered.
    """
    subject, candidates = corpus_subjects()[0]
    pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
    first = pack[0]
    assert first.kind is EvidenceKind.REMITTANCE_REFERENCE

    proposal = _proposal_citing(str(first.evidence_id))
    assert cited_entry_ids(subject, candidates, proposal) == ()


def test_the_arms_pairing_path_resolves_a_cited_entry() -> None:
    """The code path §20 asks for, exercised without a model.

    A citation of a ``candidate_ledger_entry`` item resolves back to that entry's id, which is what
    makes a model's citation readable as a match assertion in a contract that gives it no field for
    one.
    """
    subject, candidates, entry_items = _first_subject_offered_a_candidate()

    cited = cited_entry_ids(subject, candidates, _proposal_citing(str(entry_items[0].evidence_id)))
    assert len(cited) == 1
    assert f"ledger_entry:{cited[0]}" == entry_items[0].source_ref


def test_a_citation_of_something_not_in_the_pack_resolves_to_nothing() -> None:
    """Resolved against the candidates actually offered, so a stray reference is dropped rather
    than mapped onto a plausible entry.

    The alternative — treating any unknown id as a pairing with the nearest candidate — is how a
    harness starts scoring a model on answers it did not give.
    """
    subject, candidates = corpus_subjects()[0]
    foreign = evidence_id_for(uuid.uuid4(), EvidenceKind.CANDIDATE_LEDGER_ENTRY, str(uuid.uuid4()))
    assert cited_entry_ids(subject, candidates, _proposal_citing(str(foreign))) == ()


def test_the_pairing_grader_uses_the_same_truth_as_the_deterministic_arm() -> None:
    """Same definition of a correct pair on both arms, or the table compares two things.

    Pure and takes the truth as an argument, so it can be exercised exactly and cannot quietly
    become the thing that produces a number for a run that did not happen.
    """
    line, right, wrong = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scenario_of_line = {line: "SC-001-exact-match"}
    scenario_of_entry = {right: "SC-001-exact-match", wrong: "SC-002-reference-mismatch"}

    assert grade_llm_pairings({line: [right]}, scenario_of_line, scenario_of_entry) == (1, 1)
    assert grade_llm_pairings({line: [wrong]}, scenario_of_line, scenario_of_entry) == (1, 0)
    assert grade_llm_pairings({line: [right, wrong]}, scenario_of_line, scenario_of_entry) == (2, 1)
    assert grade_llm_pairings({line: []}, scenario_of_line, scenario_of_entry) == (0, 0)
    assert grade_llm_pairings({}, scenario_of_line, scenario_of_entry) == (0, 0)


def test_the_arms_origin_is_read_from_the_cassette_rather_than_hard_coded() -> None:
    """The day a real capture is committed, the arm has to notice.

    Proved with an in-memory cassette that declares ``captured`` — never a committed file, because
    a committed file claiming a capture nobody made would be the fabrication this whole module is
    built against.
    """
    real = load_cassette(REPLAY_CASSETTE)
    assert origin_of(real, ProviderId.ANTHROPIC) is CassetteOrigin.SYNTHESISED

    pretend_capture = Cassette(
        interactions=tuple(
            dataclasses.replace(item, origin=Origin.CAPTURED) for item in real.interactions
        )
    )
    assert origin_of(pretend_capture, ProviderId.ANTHROPIC) is CassetteOrigin.CAPTURED


def test_a_mixed_cassette_is_refused_rather_than_described_by_the_flattering_half() -> None:
    """One score cannot describe one captured response among twelve synthesised ones."""
    real = load_cassette(REPLAY_CASSETTE)
    anthropic = [i for i in real.interactions if i.provider is ProviderId.ANTHROPIC]
    mixed = Cassette(
        interactions=(
            dataclasses.replace(anthropic[0], origin=Origin.CAPTURED),
            *anthropic[1:],
        )
    )
    with pytest.raises(ValueError, match="mixes"):
        origin_of(mixed, ProviderId.ANTHROPIC)


# ======================================================================================
# Arm 3 — the shipped hybrid: half measured, and the halves are not added together
# ======================================================================================


def test_the_hybrid_reuses_the_deterministic_accuracy_rather_than_re_measuring_it() -> None:
    """The same matcher on the same corpus is one measurement, reported twice.

    Identity rather than equality, because two independently computed figures could differ in the
    last decimal and a reader would look for meaning in the difference.
    """
    deterministic = measure_deterministic_arm(**FAST)  # type: ignore[arg-type]
    hybrid = measure_hybrid_arm(deterministic=deterministic)

    assert hybrid.accuracy is deterministic.accuracy


def test_the_hybrids_cost_and_latency_are_not_measured_and_do_not_count_only_the_free_half() -> (
    None
):
    """**The most tempting fabrication in the table**, refused explicitly.

    The deterministic half of this arm costs nothing to run and is fast. Reporting those as the
    hybrid's cost and p95 would produce a flattering pair of numbers that describe half the system.
    """
    hybrid = measure_hybrid_arm(deterministic=measure_deterministic_arm(**FAST))  # type: ignore[arg-type]

    assert hybrid.usd_per_1000_lines.render() == NOT_MEASURED
    assert hybrid.p95.render() == NOT_MEASURED
    assert "would read as the total" in (hybrid.usd_per_1000_lines.why_absent or "")
    assert "provider round trip" in (hybrid.p95.why_absent or "")
    assert any(
        f"Treatment-proposal accuracy on the residual: {NOT_MEASURED}" in note
        for note in hybrid.notes
    )


def test_the_hybrid_row_states_that_the_model_never_touches_an_amount() -> None:
    """The permanent constraint, restated where a reader of the table will see it.

    Not decoration: this row is the one somebody quotes as "the system's accuracy", and the first
    question that invites is what the model was allowed to decide.
    """
    hybrid = measure_hybrid_arm(deterministic=measure_deterministic_arm(**FAST))  # type: ignore[arg-type]
    assert any("never" in note and "amount" in note for note in hybrid.notes)


# ======================================================================================
# The rendered table
# ======================================================================================


def test_the_table_has_all_three_arms_in_order_and_no_fabricated_number() -> None:
    comparison = compare_arms(**FAST)  # type: ignore[arg-type]
    rendered = render_comparison(comparison)

    assert tuple(result.arm for result in comparison.arms) == ARM_ORDER
    for arm in ARM_ORDER:
        assert f"| {arm.value} |" in rendered

    assert rendered.count(NOT_MEASURED) >= 5, "five cells have no run behind them"
    assert "TODO" not in rendered and "TBD" not in rendered
    assert "|  |" not in rendered, "an empty cell reads as zero"


def test_every_measured_cell_in_the_table_carries_no_model_as_its_origin() -> None:
    """Today, every number in the table comes from code with no model in it. Asserted, so the day
    a captured figure appears the change is deliberate and reviewed."""
    for result in compare_arms(**FAST).arms:  # type: ignore[arg-type]
        for figure in (result.accuracy, result.usd_per_1000_lines, result.p95):
            if figure.is_measured:
                assert figure.origin is CassetteOrigin.NO_MODEL


def test_the_table_explains_what_not_measured_means() -> None:
    """A reader who thinks the cell is an omission will fill it in from somewhere else."""
    rendered = render_comparison(compare_arms(**FAST))  # type: ignore[arg-type]
    assert "not a placeholder for a number somebody forgot" in rendered
    assert "usage fields" in rendered


def test_the_measured_accuracy_and_cost_are_identical_across_runs() -> None:
    """The reproducible half of the table, asserted as reproducible.

    Latency deliberately excluded: it is wall clock, it legitimately differs between runs, and that
    is why the table is generated on demand rather than committed and drift-checked the way the
    §19 results table is.
    """
    first, second = compare_arms(**FAST), compare_arms(**FAST)  # type: ignore[arg-type]

    for left, right in zip(first.arms, second.arms, strict=True):
        assert left.accuracy.value == right.accuracy.value
        assert left.usd_per_1000_lines.value == right.usd_per_1000_lines.value
        assert left.notes == right.notes


def test_the_published_table_runs_over_the_same_set_the_golden_labels_describe() -> None:
    """§20 says "on the same set", and the defaults are what makes that literally true."""
    assert ARM_SEED == GOLDEN_SEED
    assert ARM_PROFILE is GOLDEN_PROFILE
    assert ARM_INSTANCES == GOLDEN_INSTANCES


def test_the_cli_renders_the_table() -> None:
    assert cli.main(["compare"]) == 0


# ======================================================================================
# live-eval is gated, and it prints no value
# ======================================================================================


def test_live_eval_refuses_without_the_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command that can spend money is never the default."""
    monkeypatch.delenv(cli.LIVE_EVAL_OPT_IN, raising=False)
    assert cli.main(["live-eval"]) == 1

    stderr = capsys.readouterr().err
    assert cli.LIVE_EVAL_OPT_IN in stderr
    assert "is not set to 1" in stderr


def test_live_eval_refuses_even_with_the_opt_in_because_there_is_no_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**The second refusal, and the honest one.**

    Nothing under ``llm/`` imports an HTTP client and no transport that speaks HTTP exists in this
    repository. That is the property that makes every other evaluation command provably offline, so
    the opt-in being set does not make a capture possible — it only records that somebody intends
    one.
    """
    monkeypatch.setenv(cli.LIVE_EVAL_OPT_IN, "1")
    assert cli.main(["live-eval"]) == 1

    stderr = capsys.readouterr().err
    assert "no transport that speaks HTTP" in stderr
    assert "necessary and not sufficient" in stderr


def test_live_eval_names_the_variables_and_prints_no_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Names only. A tool that echoed a credential to explain itself would be the leak.

    The stand-in values are **assembled at run time rather than written as literals**. They are
    fake, but a committed string matching the scrubber's own ``sk-ant-`` shape is a false positive
    waiting to happen in any repository secret scan — and a repository whose secret scan cries wolf
    is one where the real hit gets waved through.
    """
    fake_anthropic = "sk-" + "ant-" + "n" * 24
    fake_openai = "sk-" + "n" * 28

    monkeypatch.setenv(cli.LIVE_EVAL_OPT_IN, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_anthropic)
    monkeypatch.setenv("OPENAI_API_KEY", fake_openai)

    cli.main(["live-eval"])
    stderr = capsys.readouterr().err

    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CASSETTE_CAPTURE"):
        assert name in stderr
    assert fake_anthropic not in stderr
    assert fake_openai not in stderr
    assert "sk-" not in stderr, "nothing credential-shaped may reach the output at all"


def _first_subject_offered_a_candidate() -> tuple[
    ExceptionSubject, list[CandidateEntryFact], list[EvidenceItem]
]:
    """The first corpus exception whose pack actually offers a ledger entry to cite.

    Searched rather than assumed: the assembler admits only candidates in the subject's own
    currency and inside the evidence window, so several exceptions are offered none at all — and a
    test written against ``corpus_subjects()[0]`` asserted a vacuous truth about the one exception
    that has nothing to cite.
    """
    for subject, candidates in corpus_subjects():
        pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
        entries = [item for item in pack if item.kind is EvidenceKind.CANDIDATE_LEDGER_ENTRY]
        if entries:
            return subject, list(candidates), entries
    raise AssertionError("no corpus exception is offered a candidate entry at all")


def _proposal_citing(evidence_id: str) -> TreatmentProposal:
    """A minimal valid proposal citing one evidence item. Not a model's opinion, and unused as one.

    Built here rather than replayed, because what is under test is the resolution of a citation and
    a replayed proposal would drag the cassette's own choices into it.
    """
    return TreatmentProposal(
        treatment=TreatmentCode.REBOOK,
        confidence=ConfidenceBand.MEDIUM,
        rationale="constructed in a test to exercise citation resolution",
        evidence_refs=(EvidenceRef(evidence_id=evidence_id),),
        abstained=False,
    )


def test_the_subject_helper_matches_the_shipped_subject_shape() -> None:
    """A guard on this module: the helpers above must describe the real types.

    ``ExceptionSubject`` and ``CandidateEntryFact`` are constructed nowhere in this file — the real
    ones come from ``corpus_subjects`` — and this asserts the shapes referenced here are the shipped
    ones rather than a local approximation that could drift.
    """
    subject, candidates = corpus_subjects()[0]
    assert isinstance(subject, ExceptionSubject)
    assert subject.classification in ExceptionClassification
    assert isinstance(subject.amount, decimal.Decimal)
    assert isinstance(subject.value_date, dt.date)
    assert candidates and isinstance(candidates[0], CandidateEntryFact)
