"""The evaluation CLI: generate, verify, score and gate (M6.1, M6.2).

``generate``
    Rebuild ``tests/golden/treatment-golden.jsonl`` from the seeded generator and write it.

``verify``
    Rebuild it and compare with what is committed, byte for byte. Fails with the regeneration
    command in the message, because that is what someone reading a red build needs. The unit suite
    asserts the same property; this exists so CI can say it in one line.

``score``
    Grade a file of proposals against the committed golden set. The origin of the responses is a
    **required** argument with no default, because the one thing this harness must never do is
    present a synthesised run as an evaluation result.

``gate``
    Replay the committed cassette through the shipped proposal path, recompute every figure the
    scorer reports, and fail on **any** difference from ``tests/golden/replay-baseline.json``.
    ``--update`` rewrites the baseline deliberately. This is a *reproduction* gate: the cassettes
    are synthesised, so no number it compares is a statement about a model (see
    :mod:`tests.evaluation.gate`).

Every command is offline by construction: no HTTP client is in the dependency graph of any module
they import, and none of them takes a credential.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from ledger_exception_control_plane.db.control import TreatmentCode
from tests.evaluation.gate import (
    BASELINE_PATH,
    compare,
    load_baseline,
    measure,
    render_baseline,
)
from tests.evaluation.golden import (
    GOLDEN_PATH,
    build_golden_set,
    load_golden_set,
    render_golden_set,
)
from tests.evaluation.scorer import CassetteOrigin, Proposal, score


def _generate(path: pathlib.Path) -> int:
    golden = build_golden_set()
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``newline="\n"`` explicitly, the same as the cassette builder. Without it, Python's text mode
    # translates every newline to the platform's, so regenerating this file on Windows produced
    # 251 CRLFs where CI produces 251 LFs — a file that is *not* byte-identical across platforms
    # while every drift check still passed, because `read_text` translates them back. "Same seed,
    # same bytes" has to mean the bytes on disk.
    path.write_text(render_golden_set(golden), encoding="utf-8", newline="\n")
    print(
        f"wrote {len(golden.records)} records to {path.name} "
        f"({len(golden.hold_out)} held out, {len(golden.human_labelled)} human-labelled)"
    )
    for classification, count in golden.by_classification.items():
        print(f"  {classification:22s} {count:4d}")
    return 0


def _verify(path: pathlib.Path) -> int:
    if not path.is_file():
        print(f"{path} does not exist; run `make golden` to generate it", file=sys.stderr)
        return 1
    produced = render_golden_set(build_golden_set())
    if path.read_text(encoding="utf-8") != produced:
        print(
            f"{path.name} has drifted from what the generator produces; run `make golden` and "
            "review the diff",
            file=sys.stderr,
        )
        return 1
    golden = load_golden_set(path)
    print(
        f"the committed golden set matches its generator: {len(golden.records)} records, "
        f"{len(golden.hold_out)} held out, {len(golden.human_labelled)} human-labelled"
    )
    return 0


def _score(proposals_path: pathlib.Path, origin: CassetteOrigin) -> int:
    golden = load_golden_set()
    submitted = [
        Proposal(
            exception_id=row["exception_id"],
            treatment=TreatmentCode(row["treatment"]),
            abstained=bool(row.get("abstained", False)),
        )
        for row in (
            json.loads(line)
            for line in proposals_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    ]
    result = score(golden, submitted, origin=origin)

    print(result.headline())
    print()
    print(f"  scored                  {result.scored}")
    print(f"  correct                 {result.correct}")
    print(f"  accuracy                {result.accuracy:.1%}")
    print(f"  constant-answer baseline{result.majority_baseline:>8.1%}  ({result.majority_label})")
    print(f"  lift over baseline      {result.lift_over_baseline:+.1%}")
    print(f"  priceable records       {result.priceable}")
    print(f"  accuracy on priceable   {result.accuracy_on_priceable:.1%}")
    print(f"  abstention rate         {result.abstention_rate:.1%}")
    print(
        f"    where escalating was correct        {result.abstained_where_escalation_was_correct}"
    )
    print(
        f"    where a treatment was available     "
        f"{result.abstained_where_a_treatment_was_available}"
    )
    print()
    print("  confusion (expected -> proposed):")
    for (expected, proposed), count in result.confusion.items():
        mark = " " if expected == proposed else "*"
        print(f"   {mark} {expected:10s} -> {proposed:10s} {count:5d}")

    if not result.is_complete:
        print(file=sys.stderr)
        if result.unanswered:
            print(f"  {len(result.unanswered)} golden record(s) unanswered", file=sys.stderr)
        if result.unknown_ids:
            print(
                f"  {len(result.unknown_ids)} proposal(s) for records not in the set",
                file=sys.stderr,
            )
        return 1
    return 0


def _gate(path: pathlib.Path, *, update: bool) -> int:
    """Recompute the offline replay and compare it with the committed baseline.

    Exits non-zero on **any** difference. Everything upstream is deterministic, so there is nothing
    for a tolerance band to absorb except a behaviour change somebody would rather not discuss.
    """
    produced = measure()

    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_baseline(produced), encoding="utf-8", newline="\n")
        print(f"wrote {path.name}")
        for provider, metrics in sorted(produced.providers.items()):
            print(
                f"  {provider:10s} {metrics['scored']:3d} scored, "
                f"{metrics['correct']:3d} agreeing, origin {metrics['response_origin']}"
            )
        print()
        print(
            "  This is a reproduction baseline over synthesised cassettes. It is not a model "
            "measurement and none of its figures is a quality threshold."
        )
        return 0

    if not path.is_file():
        print(
            f"{path} does not exist; run `make eval-gate-update` to create it",
            file=sys.stderr,
        )
        return 1

    differences = compare(load_baseline(path), produced)
    if differences:
        print(
            f"the offline evaluation replay no longer matches {path.name}: "
            f"{len(differences)} difference(s)",
            file=sys.stderr,
        )
        for line in differences:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nThis gate protects the reproduction, not a model: evidence assembly, prompt "
            "construction, request fingerprinting, response parsing, the golden labels and the "
            "scorer's arithmetic. If the change above is intended, run "
            "`make eval-gate-update` and put the new baseline in the review.",
            file=sys.stderr,
        )
        return 1

    for provider, metrics in sorted(produced.providers.items()):
        print(
            f"{provider:10s} {metrics['scored']:3d} scored, {metrics['correct']:3d} agreeing, "
            f"{metrics['distinct_recordings_served']:3d} recordings served, origin "
            f"{metrics['response_origin']}"
        )
    print(f"the offline evaluation replay matches {path.name}")
    print(
        "  (reproduction gate over synthesised cassettes: not a model measurement, and no "
        "figure here is a quality threshold)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.evaluation",
        description="Generate, verify, score and gate the §20 evaluation artefacts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate", help="rebuild the committed golden set")
    sub.add_parser("verify", help="fail if the committed golden set has drifted")

    gating = sub.add_parser(
        "gate", help="fail if the offline cassette replay has drifted from the committed baseline"
    )
    gating.add_argument(
        "--update",
        action="store_true",
        help=(
            "rewrite the baseline from the current run instead of comparing. Deliberate: the new "
            "file is what review then sees."
        ),
    )

    scoring = sub.add_parser("score", help="grade a JSONL file of proposals")
    scoring.add_argument("proposals", type=pathlib.Path)
    scoring.add_argument(
        "--origin",
        required=True,
        choices=[origin.value for origin in CassetteOrigin],
        help=(
            "where the responses came from. Required and with no default: a score over "
            "synthesised cassettes measures this harness, not a model, and the report says which."
        ),
    )

    arguments = parser.parse_args(argv)
    if arguments.command == "generate":
        return _generate(GOLDEN_PATH)
    if arguments.command == "verify":
        return _verify(GOLDEN_PATH)
    if arguments.command == "gate":
        return _gate(BASELINE_PATH, update=arguments.update)
    return _score(arguments.proposals, CassetteOrigin(arguments.origin))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
