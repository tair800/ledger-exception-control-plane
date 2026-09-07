"""M6.2 — the CI evaluation gate, and the proof that it can fail.

`IMPLEMENTATION_PLAN.md` 6.2 sets the exit criterion in one line: *"the gate is proven to fail, not
merely to exist"*, and §20 repeats it — *"the regression gate is itself verified by injecting a
deliberate regression and confirming CI fails"*. A gate nobody has seen go red is a gate on trust,
and this repository has already been bitten twice by tests that could not fail (see
``tests/test_cassette_harness.py``'s own note). So the falsifiability tests here are the point of
the module, not an appendix to it: each one plants a change that a working gate must catch, and
asserts a non-zero exit.

**What the gate is a gate on.** Not model quality. The committed cassettes are synthesised and
``stand_in_answer`` assigns treatments round-robin by position, so agreement with the golden labels
is arithmetic. The gate protects the *reproduction*: evidence assembly, prompt construction, request
fingerprinting, response parsing, the golden labels, and the scorer's arithmetic. A test below
asserts that the artefact says so in its own text, because whoever quotes a number out of that file
will not read this docstring.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

import pytest

from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.llm.cassette import Origin, load_cassette
from ledger_exception_control_plane.llm.port import ProviderId
from tests.cassette_builder import stand_in_answer
from tests.evaluation import __main__ as cli
from tests.evaluation.gate import (
    BASELINE_PATH,
    BASELINE_VERSION,
    compare,
    load_baseline,
    measure,
    render_baseline,
)
from tests.evaluation.replay import (
    REPLAY_CASSETTE,
    REPLAY_INSTANCES,
    REPLAY_PROFILE,
    origin_of,
    replay_all,
    replay_golden_set,
    score_replay,
)
from tests.evaluation.scorer import CassetteOrigin

#: Thirteen exceptions in the canonical corpus, on two providers. The same constant the cassette
#: harness pins, restated so a corpus change fails here with a message about the corpus.
CORPUS_EXCEPTIONS = 13


# ======================================================================================
# The replay drives the shipped path
# ======================================================================================


def test_the_replay_answers_every_exception_from_its_own_recording() -> None:
    """One call, one recording, no repeats — on both providers.

    The falsifiability property the cassette harness had to learn the hard way: a transport that
    ignored the fingerprint and served one answer to everything would produce thirteen proposals
    and a plausible score. Asserting the *distinct* recordings served is what rules that out.
    """
    runs = replay_all()
    assert len(runs) == 2
    assert {run.provider for run in runs} == {ProviderId.ANTHROPIC, ProviderId.OPENAI}

    for run in runs:
        assert len(run.proposals) == CORPUS_EXCEPTIONS
        assert len(run.served) == CORPUS_EXCEPTIONS, "a call was answered twice, or not at all"
        assert len(set(run.served)) == CORPUS_EXCEPTIONS, (
            "one recording answered more than one call; the fingerprint is not discriminating"
        )
        assert len({p.exception_id for p in run.proposals}) == CORPUS_EXCEPTIONS


def test_every_replayed_proposal_joins_to_a_golden_record() -> None:
    """**The identity property, at the point where it matters.**

    The scorer reports ``unanswered`` and ``unknown_ids`` rather than silently dropping either, so a
    disjoint key space produces a complete-looking run over an empty intersection. This asserts the
    intersection is total, and the gate records both counts as zero so a regression there is caught
    by the gate as well as by this test.
    """
    golden = replay_golden_set()
    labelled = {record.exception_id for record in golden.records}
    assert len(labelled) == CORPUS_EXCEPTIONS

    for run in replay_all():
        result = score_replay(run, golden)
        assert result.unanswered == ()
        assert result.unknown_ids == ()
        assert result.scored == CORPUS_EXCEPTIONS
        assert result.is_complete


def test_the_run_is_reported_as_synthesised_and_refuses_to_claim_a_model() -> None:
    """The origin is read off the cassette, and it governs the wording of every headline.

    Not a constant in this harness: on the day a real capture is committed the reports have to
    re-describe themselves, and a hard-coded ``SYNTHESISED`` would make them lie in the other
    direction.
    """
    cassette = load_cassette(REPLAY_CASSETTE)
    assert {item.origin for item in cassette.interactions} == {Origin.SYNTHESISED}

    for run in replay_all():
        assert run.origin is CassetteOrigin.SYNTHESISED
        assert origin_of(cassette, run.provider) is CassetteOrigin.SYNTHESISED

        result = score_replay(run)
        assert result.measures_a_model is False
        assert "THIS IS NOT A MODEL MEASUREMENT" in result.headline()
        assert "accuracy" not in result.headline()


def test_the_cassette_treatments_are_positional_which_is_why_this_is_not_a_measurement() -> None:
    """**The honest framing, asserted against the builder rather than asserted in prose.**

    ``stand_in_answer`` assigns ``TreatmentCode[position % 4]``. So the agreement rate the gate
    records is a function of two orderings — the exceptions' sorted ids and the enum's declaration
    order — and contains no judgement at all. If this ever stops being true, the wording in the
    baseline and in the gate's docstrings has to be revisited, and this test is what forces that.
    """
    treatments = list(TreatmentCode)
    for position in range(len(treatments) * 3):
        answer = stand_in_answer(position, ["e1"])
        assert answer["treatment"] == treatments[position % len(treatments)].value
        assert answer["rationale"] == "Synthesised stand-in answer. Not produced by a model."


# ======================================================================================
# The committed baseline
# ======================================================================================


def test_the_gate_passes_on_the_committed_baseline() -> None:
    """The green case, through the same comparison the CLI runs."""
    assert compare(load_baseline(), measure()) == []
    assert cli.main(["gate"]) == 0


def test_the_committed_baseline_is_byte_identical_to_what_the_code_renders() -> None:
    """A committed artefact and its generator, the same check the corpus and cassette get."""
    assert BASELINE_PATH.read_bytes() == render_baseline(measure()).encode("utf-8")


def test_the_baseline_says_in_its_own_text_that_it_is_not_a_model_measurement() -> None:
    """**The wording is load-bearing, so it is asserted.**

    Whoever lifts a figure out of this file will read the file. ADR-060 made the same argument
    about ``Score.headline()`` and the reason has not changed: a caveat that lives only in a
    docstring does not travel with the number.
    """
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    header = document["what_this_is"]

    assert "NOT A MODEL MEASUREMENT" in header
    assert "synthesised" in header
    assert "round-robin" in header
    assert "do not treat any number in it as a quality threshold" in header
    assert "OPEN-6" in header, "the unresolved threshold decision must be named, not implied"

    assert document["baseline_version"] == BASELINE_VERSION
    assert document["golden"]["profile"] == REPLAY_PROFILE.value
    assert document["golden"]["instances"] == REPLAY_INSTANCES
    assert document["golden"]["records"] == CORPUS_EXCEPTIONS
    for metrics in document["providers"].values():
        assert metrics["measures_a_model"] is False
        assert metrics["response_origin"] == "synthesised"


def test_the_baseline_records_every_figure_the_scorer_reports() -> None:
    """A baseline that recorded accuracy alone would pass while the abstention split inverted.

    §20 asks for three figures and ADR-060 added three more; all six have to be in the artefact or
    the gate is only watching one of them.
    """
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    required = {
        "accuracy",
        "accuracy_on_priceable",
        "abstentions",
        "abstained_where_escalation_was_correct",
        "abstained_where_a_treatment_was_available",
        "confusion",
        "majority_baseline",
        "lift_over_baseline",
        "priceable",
        "correct_on_priceable",
        "unanswered",
        "unknown_ids",
    }
    for provider, metrics in document["providers"].items():
        missing = required - set(metrics)
        assert not missing, f"{provider} is missing {sorted(missing)} from the baseline"
        assert metrics["confusion"], "an empty confusion matrix is not a matrix"


# ======================================================================================
# Falsifiability — the gate has to be able to go red
# ======================================================================================


def _mutated_baseline_file(tmp_path: pathlib.Path, mutate: Any) -> pathlib.Path:
    """Write a copy of the committed baseline with one value changed.

    The file is mutated rather than the code, and in a temporary directory, so a falsifiability
    test can never leave the committed artefact wrong — which would turn a proof that the gate
    works into a red build for the next person.
    """
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    mutate(document)
    path = tmp_path / "replay-baseline.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("what", "mutate"),
    [
        (
            "a single correct answer removed",
            lambda d: d["providers"]["anthropic"].update(
                correct=d["providers"]["anthropic"]["correct"] - 1
            ),
        ),
        (
            "an accuracy nudged in the sixth decimal",
            lambda d: d["providers"]["openai"].update(accuracy=0.384616),
        ),
        (
            "one confusion cell moved",
            lambda d: d["providers"]["anthropic"]["confusion"][0].update(count=99),
        ),
        (
            "a confusion row dropped",
            lambda d: d["providers"]["openai"]["confusion"].pop(),
        ),
        (
            "the abstention split inverted",
            lambda d: d["providers"]["anthropic"].update(
                abstained_where_escalation_was_correct=0,
                abstained_where_a_treatment_was_available=2,
            ),
        ),
        (
            "the golden set's digest changed",
            lambda d: d["golden"].update(sha256="0" * 64),
        ),
        (
            "the cassette's digest changed",
            lambda d: d["cassette"].update(sha256="0" * 64),
        ),
        (
            "a model version rewritten",
            lambda d: d["providers"]["openai"].update(model_version="2027-01-01"),
        ),
        (
            "a synthesised run relabelled as captured",
            lambda d: d["providers"]["anthropic"].update(
                response_origin="captured", measures_a_model=True
            ),
        ),
        (
            "a whole provider dropped",
            lambda d: d["providers"].pop("openai"),
        ),
    ],
)
def test_the_gate_fails_on_an_injected_regression(
    tmp_path: pathlib.Path, what: str, mutate: Any
) -> None:
    """**The exit criterion for 6.2.** Ten deliberate regressions, ten non-zero exits.

    In memory and against a temporary file, so the proof needs no CI run and cannot be skipped by
    someone who does not want to break the build to see it work. Each case is a way the first
    version of a gate like this could have been green and worthless: comparing accuracy only,
    rounding before comparing, ignoring the confusion matrix, ignoring provenance, or iterating the
    *produced* providers rather than the union.
    """
    path = _mutated_baseline_file(tmp_path, mutate)
    assert cli._gate(path, update=False) == 1, f"the gate did not notice: {what}"

    differences = compare(load_baseline(path), measure())
    assert differences, f"compare() did not notice: {what}"


def test_the_gate_reports_what_changed_rather_than_that_something_changed(
    tmp_path: pathlib.Path,
) -> None:
    """A failure message naming the metric, not "the JSON differs".

    The difference between a gate somebody fixes and a gate somebody deletes.
    """
    path = _mutated_baseline_file(tmp_path, lambda d: d["providers"]["anthropic"].update(correct=1))
    differences = compare(load_baseline(path), measure())

    assert len(differences) == 1
    assert differences[0].startswith("providers.anthropic.correct:")
    assert "baseline 1" in differences[0]


def test_the_gate_refuses_a_baseline_from_another_version(tmp_path: pathlib.Path) -> None:
    """Refused, not partially compared: a comparison that skipped an unrecognised document would
    turn a removed metric into a passing gate."""
    path = _mutated_baseline_file(tmp_path, lambda d: d.update(baseline_version="99"))
    with pytest.raises(ValueError, match="baseline version"):
        load_baseline(path)


def test_the_gate_reports_a_missing_baseline_rather_than_creating_one(
    tmp_path: pathlib.Path,
) -> None:
    """An absent baseline is a red build with an instruction, never an implicit ``--update``.

    A gate that wrote its own baseline on first run would pass on every machine, forever.
    """
    assert cli._gate(tmp_path / "absent.json", update=False) == 1


def test_the_update_path_writes_a_baseline_the_gate_then_accepts(tmp_path: pathlib.Path) -> None:
    """``--update`` is the deliberate door, and what it writes is what the gate reads."""
    path = tmp_path / "replay-baseline.json"
    assert cli._gate(path, update=True) == 0
    assert cli._gate(path, update=False) == 0
    assert path.read_bytes() == BASELINE_PATH.read_bytes(), (
        "an --update in a temporary directory produced different bytes from the committed file; "
        "the run is not reproducible"
    )


def test_the_replay_is_deterministic_across_runs() -> None:
    """Two runs, byte-identical documents. Nothing here samples anything."""
    assert render_baseline(measure()) == render_baseline(measure())


def test_the_baseline_dataclass_is_frozen_so_a_measurement_cannot_be_edited_after_the_fact() -> (
    None
):
    """Provenance that later code can edit is not provenance — the same rule the proposal contract
    applies to a model's answer."""
    baseline = load_baseline()
    with pytest.raises(dataclasses.FrozenInstanceError):
        baseline.cassette_digest = "0" * 64  # type: ignore[misc]
