"""The CI evaluation gate: recompute the offline replay, compare it with what is committed (M6.2).

`PROJECT_SPEC.md` §20 asks for a *"CI gate [that] fails the build on regression against a committed
threshold"*, and `IMPLEMENTATION_PLAN.md` 6.2 wants it *"proven to fail, not merely to exist"*. This
module is the comparison; :mod:`tests.evaluation.replay` produces the run it compares.

**What it is a gate on, stated before the numbers.** The committed cassettes are synthesised and
their treatments are assigned round-robin by position, so agreement with the golden labels is
arithmetic rather than judgement (see :mod:`tests.evaluation.replay`). This gate therefore protects
the **reproduction**, not the model:

- the evidence a subject assembles, and its order;
- the prompt built from it, and therefore the request fingerprint that decides a cassette match;
- each adapter's parsing of its own vendor's response envelope;
- the golden set's labels and identifiers, via the digest of the rendered canonical set;
- the scorer's arithmetic, via every figure it reports.

A change in any of those moves at least one number here and the gate says which. **No figure in the
baseline is a quality threshold, and OPEN-6 — what accuracy and abstention thresholds should fail a
build — stays open**, because it cannot be answered before a real capture exists. A threshold set
against synthesised responses would be a number invented to look like a gate.

**Why an exact comparison rather than a tolerance.** Everything upstream is deterministic: a seeded
generator, a pure assembler, a fingerprint match, a fixed file. There is no sampling here, so any
difference at all is a change in behaviour, and a tolerance band would only decide how much
behaviour change is allowed to pass unmentioned. ``--update`` exists for when the change is
intended, and it rewrites the file that review then sees.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from collections.abc import Mapping, Sequence
from typing import Any, Final

from tests.evaluation.golden import GOLDEN_SCHEMA_VERSION, GoldenSet, render_golden_set
from tests.evaluation.replay import (
    REPLAY_INSTANCES,
    REPLAY_PROFILE,
    ProviderReplay,
    cassette_digest,
    replay_all,
    replay_golden_set,
    score_replay,
)
from tests.evaluation.scorer import Score

__all__ = [
    "BASELINE_PATH",
    "BASELINE_VERSION",
    "Baseline",
    "compare",
    "load_baseline",
    "measure",
    "render_baseline",
]

#: Bumped when the *shape* of the baseline document changes. A file at another version is refused
#: rather than partially compared, because a comparison that silently skipped a missing key would
#: turn a removed metric into a passing gate.
BASELINE_VERSION: Final = "1"

#: The committed artefact.
BASELINE_PATH: Final = (
    pathlib.Path(__file__).resolve().parents[1] / "golden" / "replay-baseline.json"
)

#: The header written into the file. In the artefact rather than in this docstring, because the
#: person who quotes a number out of the baseline will be reading the file, not this module.
_WHAT_THIS_IS: Final = (
    "REPRODUCTION BASELINE, NOT A MODEL MEASUREMENT. Every figure below was computed by "
    "replaying tests/cassettes/canonical-corpus.json through the shipped proposal path. Those "
    "cassettes are synthesised: no model produced them, and their treatments are assigned "
    "round-robin by the exception's position in sorted-id order. The agreement rate here is "
    "arithmetic over two orderings and says nothing whatever about a model's judgement. What this "
    "file protects is reproducibility - evidence assembly, prompt construction, request "
    "fingerprinting, response parsing, the golden labels and the scorer's arithmetic. Do not "
    "quote 'accuracy' from it, and do not treat any number in it as a quality threshold; the "
    "accuracy and abstention thresholds a build should fail on are OPEN-6 and cannot be chosen "
    "before a real capture exists."
)


@dataclasses.dataclass(frozen=True, slots=True)
class Baseline:
    """The committed document, and the provenance needed to re-derive it."""

    baseline_version: str
    golden_schema_version: str
    golden_profile: str
    golden_instances: int
    golden_records: int

    #: SHA-256 of the *rendered* canonical golden set, so a changed label or identifier moves it.
    golden_digest: str

    #: SHA-256 of the committed cassette, so a re-recorded file is named as the cause.
    cassette_digest: str

    #: ``provider -> metric -> value``.
    providers: Mapping[str, Mapping[str, Any]]


def _confusion(score: Score) -> list[dict[str, Any]]:
    """The confusion matrix as a list of rows.

    A list rather than a mapping, because JSON has no tuple key and ``"rebook->escalate"`` would be
    a key that has to be parsed back. Sorted, so the file is diffable.
    """
    return [
        {"expected": expected, "proposed": proposed, "count": count}
        for (expected, proposed), count in sorted(score.confusion.items())
    ]


def _metrics(run: ProviderReplay, score: Score) -> dict[str, Any]:
    """Every figure the scorer reports, plus what produced them.

    All of it, deliberately. A baseline that recorded only accuracy would pass while the abstention
    split inverted, and the abstention split is the figure §20 asks for that a single accuracy
    number is least able to stand in for.
    """
    return {
        "model_id": run.model_id,
        "model_version": run.model_version,
        "response_origin": run.origin.value,
        "measures_a_model": score.measures_a_model,
        "scored": score.scored,
        "correct": score.correct,
        "accuracy": round(score.accuracy, 6),
        "majority_label": score.majority_label,
        "majority_baseline": round(score.majority_baseline, 6),
        "lift_over_baseline": round(score.lift_over_baseline, 6),
        "priceable": score.priceable,
        "correct_on_priceable": score.correct_on_priceable,
        "accuracy_on_priceable": round(score.accuracy_on_priceable, 6),
        "abstentions": score.abstentions,
        "abstained_where_escalation_was_correct": score.abstained_where_escalation_was_correct,
        "abstained_where_a_treatment_was_available": (
            score.abstained_where_a_treatment_was_available
        ),
        "unanswered": len(score.unanswered),
        "unknown_ids": len(score.unknown_ids),
        "distinct_recordings_served": len(set(run.served)),
        "calls_served": len(run.served),
        "confusion": _confusion(score),
    }


def measure() -> Baseline:
    """Replay the committed cassette and reduce the run to the baseline document."""
    golden: GoldenSet = replay_golden_set()
    runs = replay_all()

    return Baseline(
        baseline_version=BASELINE_VERSION,
        golden_schema_version=GOLDEN_SCHEMA_VERSION,
        golden_profile=REPLAY_PROFILE.value,
        golden_instances=REPLAY_INSTANCES,
        golden_records=len(golden.records),
        golden_digest=hashlib.sha256(render_golden_set(golden).encode("utf-8")).hexdigest(),
        cassette_digest=cassette_digest(),
        providers={
            run.provider.value: _metrics(run, score_replay(run, golden))
            for run in sorted(runs, key=lambda r: r.provider.value)
        },
    )


def render_baseline(baseline: Baseline) -> str:
    """The committed file's exact bytes. Indented, because a person has to read it in review."""
    document = {
        "what_this_is": _WHAT_THIS_IS,
        "baseline_version": baseline.baseline_version,
        "golden": {
            "schema_version": baseline.golden_schema_version,
            "profile": baseline.golden_profile,
            "instances": baseline.golden_instances,
            "records": baseline.golden_records,
            "sha256": baseline.golden_digest,
        },
        "cassette": {
            "path": "tests/cassettes/canonical-corpus.json",
            "sha256": baseline.cassette_digest,
        },
        "providers": baseline.providers,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def load_baseline(path: pathlib.Path = BASELINE_PATH) -> Baseline:
    """Read the committed baseline, refusing anything this version does not understand."""
    document = json.loads(path.read_text(encoding="utf-8"))
    version = document.get("baseline_version")
    if version != BASELINE_VERSION:
        raise ValueError(
            f"{path.name} is baseline version {version!r} and this code reads "
            f"{BASELINE_VERSION!r}; regenerate it with `--update` rather than migrating it"
        )
    golden = document["golden"]
    return Baseline(
        baseline_version=version,
        golden_schema_version=golden["schema_version"],
        golden_profile=golden["profile"],
        golden_instances=golden["instances"],
        golden_records=golden["records"],
        golden_digest=golden["sha256"],
        cassette_digest=document["cassette"]["sha256"],
        providers=document["providers"],
    )


def _differences_in(prefix: str, expected: Any, produced: Any) -> list[str]:
    """Every leaf on which two documents disagree, named by path.

    Recursive rather than a whole-document equality check: a gate whose failure message is "the
    JSON differs" makes a reader diff two files by eye, and the thing they need to know is which
    metric moved and by how much.
    """
    if isinstance(expected, Mapping) and isinstance(produced, Mapping):
        differences: list[str] = []
        for key in sorted(set(expected) | set(produced)):
            if key not in expected:
                differences.append(
                    f"{prefix}{key}: absent from the baseline, produced {produced[key]!r}"
                )
            elif key not in produced:
                differences.append(
                    f"{prefix}{key}: in the baseline as {expected[key]!r}, not produced"
                )
            else:
                differences.extend(_differences_in(f"{prefix}{key}.", expected[key], produced[key]))
        return differences

    both_are_lists = (
        isinstance(expected, Sequence)
        and isinstance(produced, Sequence)
        and not isinstance(expected, str)
        and not isinstance(produced, str)
    )
    if both_are_lists:
        if len(expected) != len(produced):
            return [
                f"{prefix[:-1]}: {len(expected)} row(s) in the baseline, {len(produced)} produced"
            ]
        differences = []
        for index, (left, right) in enumerate(zip(expected, produced, strict=True)):
            differences.extend(_differences_in(f"{prefix}{index}.", left, right))
        return differences

    if expected != produced:
        return [f"{prefix[:-1]}: baseline {expected!r}, produced {produced!r}"]
    return []


def compare(baseline: Baseline, produced: Baseline) -> list[str]:
    """What changed, as one line per changed value. Empty means the gate passes.

    Compared through the rendered documents rather than field by field, so a field added to
    :class:`Baseline` is covered by the gate the moment it is written rather than when somebody
    remembers to extend this function.
    """
    return _differences_in(
        "",
        json.loads(render_baseline(baseline)),
        json.loads(render_baseline(produced)),
    )
