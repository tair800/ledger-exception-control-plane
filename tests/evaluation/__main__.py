"""The evaluation CLI: generate, verify, score, gate, packet and compare (M6.1 to M6.3).

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

``packet``
    Write the human-label packet for the frozen hold-out slice — a CSV with a blank ``HUMAN_LABEL``
    column, a JSONL companion and the instructions. **It generates no label.**

``import-labels``
    Validate a returned label file against the frozen slice and report. It writes nothing, produces
    no label, and refuses a file rather than repairing it. A file marked ``synthetic`` is validated
    and then explicitly refused a human label source.

``compare``
    Render §20's three-arm comparison as markdown. Cells with no run print ``NOT MEASURED``.

``live-eval``
    The one command that reaches a provider, and the only one that is gated. Two independent
    opt-ins, both required, and neither implies the other: ``LECP_LIVE_EVAL=1`` says a measurement
    run was intended, ``CASSETTE_CAPTURE=1`` says a recording was. It is bounded by a call budget
    that raises rather than warns, it prints its plan before the first call, and it refuses
    outright if any prompt it is about to send carries part of the answer key.

    **The shipped package still ships no HTTP client**, and the guard on ``llm/`` is unchanged and
    still passes. The transport lives beside this harness in
    :mod:`tests.evaluation.livetransport`, which is exactly the shape the cassette module was
    written for: *"recording wraps a transport an operator supplies and nothing here owns a
    socket."* ``--plan-only`` prints the plan and runs the leakage check without dialling.

    It is never invoked by CI: both opt-ins are absent there and so is the credential.

Every other command is offline by construction: no HTTP client is in the dependency graph of any
module they import, and none of them takes a credential.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import os
import pathlib
import sys
from typing import Any, Final

from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.llm.cassette import CAPTURE_OPT_IN
from ledger_exception_control_plane.llm.evidence import assemble_evidence
from ledger_exception_control_plane.llm.prompt import build_prompt
from ledger_exception_control_plane.matching import DEFAULT_POLICY
from tests.evaluation.arms import compare_arms, render_comparison
from tests.evaluation.gate import (
    BASELINE_PATH,
    compare,
    load_baseline,
    measure,
    render_baseline,
)
from tests.evaluation.golden import (
    GOLDEN_PATH,
    GoldenSet,
    build_golden_set,
    load_golden_set,
    render_golden_set,
)
from tests.evaluation.humanlabels import (
    PACKET_CSV,
    PACKET_DIR,
    PACKET_JSONL,
    PACKET_README,
    ImportRejected,
    build_packet,
    read_import,
    render_packet_csv,
    render_packet_jsonl,
    render_packet_readme,
)
from tests.evaluation.livecapture import (
    LIVE_CASSETTE_PATH,
    LIVE_PROPOSALS_PATH,
    LIVE_RUN_PATH,
    LiveOutcome,
    assert_no_answer_leaks,
    build_subjects,
    plan,
    rederive_from_cassette,
    run_live_evaluation,
)
from tests.evaluation.livetransport import (
    API_KEY_VARIABLE,
    BASE_URL_VARIABLE,
    MODEL_VARIABLE,
    CallBudget,
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


def _packet() -> int:
    """Write the packet. Three files, all generated, none of them containing a label."""
    packet = build_packet()
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    for path, text in (
        (PACKET_CSV, render_packet_csv(packet)),
        (PACKET_JSONL, render_packet_jsonl(packet)),
        (PACKET_README, render_packet_readme(packet)),
    ):
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(PACKET_DIR.parents[1])}")

    # The owner-facing pair, under `artifacts/`. Imported here rather than at module scope so the
    # rest of this CLI — the golden set, the gate, the scorer — still runs with no spreadsheet
    # library installed.
    from tests.evaluation.workbook import write_artifacts

    root = PACKET_DIR.parents[2]
    for artefact in write_artifacts(packet):
        print(f"wrote {artefact.relative_to(root)}")

    print()
    print(f"  {len(packet.records)} records, hold-out version {packet.hold_out_version}")
    print(f"  hold_out_sha256 {packet.digest}")
    print("  HUMAN_LABEL is blank in every row. No label was generated by this command.")
    return 0


def _import_labels(path: pathlib.Path) -> int:
    """Validate a returned label file. Reports; never writes, never fills anything in."""
    packet = build_packet()
    try:
        imported = read_import(path, packet)
    except ImportRejected as rejected:
        print(f"{path.name} is refused, for {len(rejected.reasons)} reason(s):", file=sys.stderr)
        for reason in rejected.reasons:
            print(f"  {reason}", file=sys.stderr)
        return 1

    print(f"{path.name} validates against hold-out version {imported.hold_out_version}")
    print(f"  {len(imported.labels)} label(s), covering the frozen slice exactly")
    counts: dict[str, int] = {}
    for label in imported.labels.values():
        counts[label.treatment.value] = counts.get(label.treatment.value, 0) + 1
    for treatment, count in sorted(counts.items()):
        print(f"    {treatment:10s} {count:4d}")

    if imported.synthetic:
        print(file=sys.stderr)
        print(
            "  MARKED SYNTHETIC. These labels exercise this validator and are not evidence about "
            "anything. They have no label source, must never be reported as human, must never be "
            "counted in an accuracy figure, and must never be written into the golden set.",
            file=sys.stderr,
        )
        return 1

    print(f"  label source: {imported.label_source.value}")
    print(
        "  Nothing has been written. Applying these labels to the golden set is a separate, "
        "reviewed change — see docs/evaluation.md."
    )
    return 0


def _compare() -> int:
    """Print §20's three-arm table. Not written to a file, and that is a decision.

    One column is wall clock on the machine that ran it, so a committed copy could not be
    drift-checked the way the §19 results table is — and a generated artefact nobody re-derives is
    the thing `CLAUDE.md` §5 was written against. So this prints, and whoever publishes it records
    the command beside the table.
    """
    print(render_comparison(compare_arms()))
    return 0


#: The environment variable that must be set to ``1`` before ``live-eval`` will do anything.
#:
#: Deliberately its own name rather than reusing the cassette opt-in: capture and *evaluation
#: against a paid API* are different decisions, and one variable for both would mean anyone
#: recording a cassette had also enabled a measurement run.
LIVE_EVAL_OPT_IN: Final = "LECP_LIVE_EVAL"


def _refuse_live(reasons: list[str]) -> int:
    """Say exactly what is missing, by variable NAME only, and spend nothing.

    A command that can reach a paid API is never the default and is never inferred from a
    credential being present. **No value is printed here, asked for here, or read into any
    artefact this repository commits.**
    """
    print("live evaluation is refused.", file=sys.stderr)
    print(file=sys.stderr)
    for index, reason in enumerate(reasons, start=1):
        print(f"  {index}. {reason}", file=sys.stderr)
    print("\n  The variables involved, by NAME only:", file=sys.stderr)
    for name in (
        LIVE_EVAL_OPT_IN,
        CAPTURE_OPT_IN,
        BASE_URL_VARIABLE,
        MODEL_VARIABLE,
        API_KEY_VARIABLE,
    ):
        print(f"    {name}", file=sys.stderr)
    return 1


def _live_eval(*, plan_only: bool, max_attempts: int, from_cassette: bool = False) -> int:
    """Run one bounded live evaluation, or refuse and say precisely why.

    Capturing a fixture and *measuring a model against a paid API* are different decisions, so
    they have different switches and both are required.
    """
    if from_cassette:
        # No credential, no opt-in, no network: this recomputes the published figures from the
        # committed capture. It is the command a reader runs to check the numbers.
        golden = load_golden_set()
        return _report_live(golden, asyncio.run(rederive_from_cassette(golden)))

    reasons: list[str] = []
    if os.environ.get(LIVE_EVAL_OPT_IN) != "1":
        reasons.append(
            f"{LIVE_EVAL_OPT_IN} is not set to 1. A command that can reach a paid API is never "
            "the default and is never inferred from a credential being present."
        )
    if os.environ.get(CAPTURE_OPT_IN) != "1":
        reasons.append(
            f"{CAPTURE_OPT_IN} is not set to 1. A live run records what it saw, and the recorder "
            "refuses to be constructed without it."
        )
    absent = [
        name
        for name in (BASE_URL_VARIABLE, MODEL_VARIABLE, API_KEY_VARIABLE)
        if not os.environ.get(name)
    ]
    if absent:
        reasons.append("these are unset: " + ", ".join(absent))
    if reasons:
        return _refuse_live(reasons)

    golden = load_golden_set()
    print("live evaluation plan - no value of any credential appears below")
    print(json.dumps(plan(golden.records, max_attempts), indent=2, sort_keys=True))
    print()

    if plan_only:
        subjects = build_subjects()
        prompts = {
            record.exception_id: build_prompt(
                subjects[record.exception_id][0],
                assemble_evidence(*subjects[record.exception_id], DEFAULT_POLICY),
            )
            for record in golden.records
        }
        assert_no_answer_leaks(prompts, golden.records)
        print(f"leakage check: PASS over {len(prompts)} prompts. No call made (--plan-only).")
        return 0

    return _report_live(golden, asyncio.run(run_live_evaluation(golden, max_attempts=max_attempts)))


def _report_live(golden: GoldenSet, result: dict[str, Any]) -> int:
    """Everything the run measured: quality, schema validity, abstention, latency, tokens, cost."""
    outcomes: list[LiveOutcome] = result["outcomes"]
    budget: CallBudget = result["budget"]
    answered = [outcome for outcome in outcomes if outcome.treatment is not None]
    failed = [outcome for outcome in outcomes if outcome.treatment is None]

    proposals = [
        Proposal(
            exception_id=outcome.exception_id,
            treatment=TreatmentCode(outcome.treatment),
            abstained=outcome.abstained,
        )
        for outcome in answered
        if outcome.treatment is not None
    ]
    full = score(golden, proposals, origin=CassetteOrigin.CAPTURED)
    held = {record.exception_id for record in golden.hold_out}
    hold = score(
        dataclasses.replace(golden, records=golden.hold_out),
        [proposal for proposal in proposals if proposal.exception_id in held],
        origin=CassetteOrigin.CAPTURED,
    )

    named = sorted({o.reported_model for o in outcomes if o.reported_model})
    print("=" * 78)
    print("LIVE MODEL EVALUATION")
    print("=" * 78)
    print(f"  route model alias      {os.environ.get(MODEL_VARIABLE)}")
    print(f"  models the route named {named}")
    print(f"  records                {len(outcomes)}")
    print(f"  live calls made        {budget.spent} of a {budget.maximum} ceiling")
    print(f"  extra attempts (retry) {sum(o.attempts - 1 for o in outcomes)}")
    print()
    rate = len(answered) / len(outcomes) if outcomes else 0.0
    print(f"  schema-valid responses {len(answered)}/{len(outcomes)}  ({rate:.1%})")
    for kind in sorted({o.failure for o in failed if o.failure}):
        print(f"    {kind:26s} {sum(1 for o in failed if o.failure == kind)}")
    print()
    print(f"  --- against all {len(golden.records)} golden records ---")
    print(f"  {full.headline()}")
    print(f"  accuracy               {full.accuracy:.1%}")
    print(f"  constant baseline      {full.majority_baseline:.1%}  ({full.majority_label})")
    print(f"  lift over baseline     {full.lift_over_baseline:+.1%}")
    print(f"  accuracy on priceable  {full.accuracy_on_priceable:.1%}  over {full.priceable}")
    print(f"  abstention rate        {full.abstention_rate:.1%}")
    print(f"    escalating correct   {full.abstained_where_escalation_was_correct}")
    print(f"    a treatment existed  {full.abstained_where_a_treatment_was_available}")
    print(f"  unanswered             {len(full.unanswered)}")
    print()
    print(f"  --- against the {len(golden.hold_out)} human-confirmed hold-out records ---")
    print(f"  agreement              {hold.accuracy:.1%}  ({hold.correct}/{hold.scored})")
    print(f"  on priceable           {hold.accuracy_on_priceable:.1%}  over {hold.priceable}")
    print("  NOTE: the derived label is a pure function of the classification, so this slice is")
    print("        four independent judgements, not 25. See ADR-068.")
    print()
    print("  confusion (expected -> proposed):")
    for (expected, proposed), count in full.confusion.items():
        mark = " " if expected == proposed else "*"
        print(f"   {mark} {expected:10s} -> {proposed:10s} {count:5d}")
    print()
    latencies = sorted(outcome.latency_seconds for outcome in outcomes)
    print(
        f"  latency  min {latencies[0]:.2f}s  p50 {_quantile(latencies, 0.5):.2f}s  "
        f"p95 {_quantile(latencies, 0.95):.2f}s  max {latencies[-1]:.2f}s"
    )
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [getattr(o, field) for o in outcomes if getattr(o, field) is not None]
        if values:
            print(
                f"  {field:18s} total {sum(values):>9,}   mean {sum(values) / len(values):>8.0f}"
                f"   reported on {len(values)}/{len(outcomes)}"
            )
        else:
            print(f"  {field:18s} not reported by the route")
    print()
    print("  Actual marginal API cost not measured; calls were executed through the owner's")
    print("  subscription-backed OmniRoute route.")
    print()
    print(f"  artefacts: {LIVE_PROPOSALS_PATH}")
    print(f"             {LIVE_CASSETTE_PATH}")
    print(f"             {LIVE_RUN_PATH}")
    return 0


def _quantile(ordered: list[float], q: float) -> float:
    """Nearest-rank. Interpolating would imply a precision this sample size does not have."""
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


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

    sub.add_parser("packet", help="write the human-label packet for the frozen hold-out slice")

    importing = sub.add_parser(
        "import-labels", help="validate a returned human-label file against the frozen slice"
    )
    importing.add_argument("labels", type=pathlib.Path)

    sub.add_parser("compare", help="render §20's three-arm comparison as markdown")
    live = sub.add_parser(
        "live-eval",
        help=(
            f"capture live provider responses and score them. Refused without "
            f"{LIVE_EVAL_OPT_IN}=1 and {CAPTURE_OPT_IN}=1. Never run by CI."
        ),
    )
    live.add_argument(
        "--from-cassette",
        action="store_true",
        help=(
            "recompute the published figures from the committed capture. No credential, no "
            "opt-in, no network, and no call."
        ),
    )
    live.add_argument(
        "--plan-only",
        action="store_true",
        help="print the plan and run the leakage check, without making a call",
    )
    live.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="attempts per record, including the first. The call ceiling is records x this.",
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
    if arguments.command == "packet":
        return _packet()
    if arguments.command == "import-labels":
        return _import_labels(arguments.labels)
    if arguments.command == "compare":
        return _compare()
    if arguments.command == "live-eval":
        return _live_eval(
            plan_only=arguments.plan_only,
            max_attempts=arguments.max_attempts,
            from_cassette=arguments.from_cassette,
        )
    return _score(arguments.proposals, CassetteOrigin(arguments.origin))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
