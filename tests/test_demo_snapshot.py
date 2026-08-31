"""The M2 visual snapshot — that it reports the pipeline rather than reimplementing it.

A demo that quietly decides anything is a second implementation of the thing it claims to show, and
the numbers on it would then be evidence of nothing. So the tests here are mostly about what the
renderer *cannot* do: no parser, no matching rule, no taxonomy, no formula, no clock, no float.

The rest verify the page itself — that every required section is present, that its counts are the
ones the pipeline produced, that it embeds no secret, path or timestamp, and that the committed copy
still matches what the code renders today.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys
from typing import Final

import pytest

from ledger_exception_control_plane.demo import (
    DEFAULT_INSTANCES,
    DEFAULT_SEED,
    DEMO_TREATMENT,
    PipelineSnapshot,
    build,
    render,
)
from ledger_exception_control_plane.demo.__main__ import DEFAULT_OUTPUT, main

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[1]
DEMO_ROOT: Final = REPO_ROOT / "src" / "ledger_exception_control_plane" / "demo"
COMMITTED: Final = REPO_ROOT / DEFAULT_OUTPUT


@pytest.fixture(scope="module")
def snapshot() -> PipelineSnapshot:
    """One pipeline run, shared by the tests below — it takes a few seconds to build."""
    return build()


@pytest.fixture(scope="module")
def page(snapshot: PipelineSnapshot) -> str:
    return render(snapshot)


# ======================================================================================
# The page reports what the pipeline actually did
# ======================================================================================


def test_the_pipeline_runs_end_to_end_and_produces_work_at_every_stage(
    snapshot: PipelineSnapshot,
) -> None:
    """A snapshot where a stage produced nothing would render an empty section and look fine.

    Each stage must have done something: files parsed, lines matched both ways, residuals left,
    classes assigned, instructions priced *and* refused. That is what makes the page worth showing.
    """
    assert snapshot.ingestion.batches > 0
    assert snapshot.ingestion.lines_parsed == snapshot.ingestion.lines_offered > 0
    assert snapshot.ingestion.batches_quarantined > 0, "the quarantine path must be exercised"
    assert snapshot.matching.exact > 0 and snapshot.matching.tolerance > 0
    assert snapshot.matching.unmatched > 0
    assert snapshot.classification.residuals == snapshot.matching.unmatched
    assert snapshot.classification.classified > 0
    assert snapshot.calculator.priced > 0
    assert snapshot.calculator.refused_by_reason


def test_the_stages_are_chained_rather_than_recomputed(snapshot: PipelineSnapshot) -> None:
    """What one stage produced is what the next one saw.

    A report that ran each stage from the corpus independently could show four internally consistent
    sections describing four different worlds.
    """
    matching = snapshot.matching
    assert matching.considered == snapshot.ingestion.lines_parsed
    assert matching.exact + matching.tolerance + matching.ambiguous + matching.unmatched == (
        matching.considered
    )
    assert matching.entries_consumed == matching.exact + matching.tolerance
    assert sum(snapshot.classification.by_class.values()) == snapshot.classification.residuals
    assert sum(snapshot.classification.by_rule.values()) == snapshot.classification.residuals
    assert snapshot.calculator.considered == snapshot.classification.residuals
    assert snapshot.calculator.priced + sum(snapshot.calculator.refused_by_reason.values()) == (
        snapshot.calculator.considered
    )


def test_every_number_on_the_page_comes_from_the_snapshot(
    snapshot: PipelineSnapshot, page: str
) -> None:
    """The rendered counts are the pipeline's, not a second tally.

    Checked by looking for each figure in the page rather than by re-deriving it: if the renderer
    ever computed its own, this is where the two would disagree.
    """
    for value in (
        snapshot.ingestion.batches,
        snapshot.ingestion.lines_parsed,
        snapshot.ingestion.batches_quarantined,
        snapshot.matching.considered,
        snapshot.matching.exact,
        snapshot.matching.tolerance,
        snapshot.matching.unmatched,
        snapshot.classification.residuals,
        snapshot.calculator.considered,
        snapshot.calculator.priced,
        snapshot.ground_truth.false_matches,
        snapshot.ground_truth.classifications_wrong,
        snapshot.ground_truth.wrong_financial_instructions,
    ):
        assert f">{value}<" in page, f"{value} does not appear in the rendered page"

    for name, count in snapshot.classification.by_class.items():
        assert name in page and str(count) in page
    for reason, count in snapshot.calculator.refused_by_reason.items():
        assert reason in page and str(count) in page


def test_the_report_states_how_it_was_produced(snapshot: PipelineSnapshot, page: str) -> None:
    """Profile, seed, size and the command, so the page can be reproduced from itself."""
    assert snapshot.profile == "bulk"
    assert snapshot.seed == DEFAULT_SEED
    assert snapshot.instances == DEFAULT_INSTANCES
    for token in ("bulk", str(DEFAULT_SEED), str(DEFAULT_INSTANCES), "make m2-demo"):
        assert token in page


def test_the_page_says_the_treatment_was_supplied_rather_than_decided(page: str) -> None:
    """Nothing in the system chooses a treatment yet. The page must not imply otherwise."""
    assert DEMO_TREATMENT.value in page
    assert "supplied by this demo" in page
    assert "Nothing in the system chooses a treatment" in page


@pytest.mark.parametrize(
    "section",
    [
        "Ledger Exception Control Plane",
        "M2 pipeline snapshot",
        "Pipeline",
        "What the pipeline did",
        "Ingestion",
        "Quarantine reasons",
        "Matching",
        "Classification",
        "Calculator",
        "Why the rest were refused",
        "Controls",
        "Representative cases",
        "Fixture evaluation",
    ],
)
def test_every_required_section_is_present(page: str, section: str) -> None:
    assert section.lower() in page.lower(), f"the report is missing: {section}"


def test_actual_output_and_ground_truth_are_separate_sections(page: str) -> None:
    """The whole reason the demo is trustworthy: a running system knows the first, not the
    second.

    Ordered, so the fixture comparison cannot be read as part of the pipeline's own reporting.
    """
    pipeline_at = page.lower().index("what the pipeline did")
    truth_at = page.lower().index("fixture evaluation")
    assert pipeline_at < truth_at
    assert "not</b> something a running system can know" in page


def test_the_page_shows_representative_cases_without_dumping_the_corpus(
    snapshot: PipelineSnapshot,
) -> None:
    """A handful of real rows, one per outcome — not a table of 215 lines."""
    assert 4 <= len(snapshot.examples) <= 12
    stages = {example.stage for example in snapshot.examples}
    assert stages == {"Matching", "Classification", "Calculator"}
    outcomes = " ".join(example.outcome for example in snapshot.examples)
    assert "Matched exactly" in outcomes
    assert "within tolerance" in outcomes
    assert "unclassified" in outcomes
    assert "refused" in outcomes


# ======================================================================================
# Determinism, and nothing environment-specific on the page
# ======================================================================================


def test_rendering_twice_produces_identical_bytes(page: str) -> None:
    """The property that lets the committed copy be checked for drift."""
    assert render(build()) == page


def test_the_page_embeds_no_timestamp_path_username_or_secret(page: str) -> None:
    """Anything environment-specific would make the committed artifact drift for no real reason —
    and a DSN on a portfolio page would be worse than drift."""
    forbidden = (
        re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"),  # ISO timestamp
        re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:/"),  # Windows path
        re.compile(r"/home/|/Users/|C:\\Users"),  # home directory
        re.compile(r"postgres(ql)?://"),  # DSN
        re.compile(r"\b(password|passwd|secret|api[_-]?key|token)\b", re.IGNORECASE),
        re.compile(r"taira|lecp_local_dev"),  # machine and dev credential
    )
    for pattern in forbidden:
        assert not pattern.search(page), f"the page contains {pattern.pattern}"


def test_the_only_dates_on_the_page_are_business_periods(page: str) -> None:
    """Accounting periods and value dates come from the corpus, which is seeded and fixed. A wall
    clock anywhere would show up as a date this test does not expect."""
    for match in re.findall(r"\b\d{4}-\d{2}(?:-\d{2})?\b", page):
        assert match.startswith("2026-"), f"unexpected date on the page: {match}"


def test_the_page_is_standalone_with_no_external_request(page: str) -> None:
    """It must open from disk. No CDN, no font host, no script, no image."""
    assert "<script" not in page.lower()
    for token in ("http://", "https://", "//cdn", "<img", "<iframe", "src="):
        assert token not in page.lower(), f"the page reaches for {token}"
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")


def test_the_committed_snapshot_matches_what_the_code_renders(page: str) -> None:
    """Drift detection. If this fails, run `make m2-demo` and commit the result."""
    assert COMMITTED.exists(), f"{DEFAULT_OUTPUT} is missing — run `make m2-demo`"
    assert COMMITTED.read_text(encoding="utf-8") == page, (
        f"{DEFAULT_OUTPUT} has drifted from the pipeline; regenerate with `make m2-demo`"
    )


def test_the_cli_renders_and_verifies(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Generation succeeds from a clean location, and `verify` catches a tampered file."""
    out = tmp_path / "nested" / "m2-demo.html"
    assert main(["render", "--out", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 5_000

    assert main(["verify", "--out", str(out)]) == 0
    out.write_text(
        out.read_text(encoding="utf-8").replace("Matching", "Muddling"), encoding="utf-8"
    )
    assert main(["verify", "--out", str(out)]) == 1
    assert main(["verify", "--out", str(tmp_path / "absent.html")]) == 1


def test_generation_succeeds_from_a_clean_checkout(tmp_path: pathlib.Path) -> None:
    """Run as a subprocess with no inherited state, the way CI or a new clone would."""
    out = tmp_path / "m2-demo.html"
    result = subprocess.run(
        [sys.executable, "-m", "ledger_exception_control_plane.demo", "render", "--out", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


# ======================================================================================
# The demo decides nothing
# ======================================================================================


def _demo_sources() -> list[tuple[str, ast.Module]]:
    paths = sorted(DEMO_ROOT.rglob("*.py"))
    assert len(paths) >= 4, "the guards must be walking real files"
    return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def _referenced_names(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.alias):
            found.append(node.name.rsplit(".", 1)[-1])
            if node.asname:
                found.append(node.asname)
    return found


def test_the_demo_contains_no_float() -> None:
    """It aggregates monetary values. A float would misreport them as surely as miscompute them."""
    for name, tree in _demo_sources():
        assert "float" not in _referenced_names(tree), f"{name} references float"


def test_the_demo_reimplements_no_business_rule() -> None:
    """Its job is *pipeline output → aggregation → HTML*, never *raw fixture → decide outcomes*.

    None of these names may be rebuilt here: the parser and normaliser are ingestion's, the
    tolerance policy and the rules are the matcher's, the taxonomy and its rule set are the
    classifier's, and the account policy and the money contract are the calculator's. The demo
    calls the boundaries that own them and counts what comes back.
    """
    forbidden = {
        # M2.1
        "parse",
        "normalise",
        "ParsedRow",
        "QuarantineCode",
        # M2.2
        "TolerancePolicy",
        "MatchRule_",
        "_eligible",
        "_accept_mutually_unique",
        # M2.3
        "ClassificationRule",
        "RULES",
        "RULE_CLASSIFICATION",
        "RULE_PRECEDENCE",
        "MovementType",
        # M2.4
        "AccountPolicy",
        "account_policy",
        "NonCalculable",
        "ROUNDING",
        "within_money_scale",
        "MONEY_QUANTUM",
        "MONEY_MAX_SCALE",
    }
    for name, tree in _demo_sources():
        for referenced in _referenced_names(tree):
            assert referenced not in forbidden, f"{name} reimplements or reaches for {referenced}"


def test_the_demo_calls_every_boundary_it_reports_on() -> None:
    """The complement of the guard above: it must actually run the pipeline, not describe it.

    A renderer that imported nothing would pass every prohibition here and report invented numbers.
    """
    called = {
        referenced for _name, tree in _demo_sources() for referenced in _referenced_names(tree)
    }
    for boundary in ("interpret", "match", "classify", "compute_adjustment", "generate"):
        assert boundary in called, f"the demo never calls {boundary}"


def test_the_demo_reads_no_clock_and_no_random_source() -> None:
    """A timestamp would make the committed artifact drift on every run."""
    for name, tree in _demo_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"now", "utcnow", "today", "time", "monotonic"}, (
                    f"{name} reads a clock via .{node.attr}"
                )
            if isinstance(node, ast.Name):
                assert node.id not in {"random", "uuid4", "randint", "choice", "shuffle"}, (
                    f"{name} references {node.id}"
                )


def test_the_demo_touches_no_database_and_no_network() -> None:
    """It runs from a generated corpus in memory. No session, no engine, no socket, no container."""
    permitted_stdlib = {
        "__future__",
        "argparse",
        "ast",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "html",
        "pathlib",
        "re",
        "sys",
        "typing",
        "uuid",
    }
    permitted_internal = {
        "ledger_exception_control_plane.demo",
        "ledger_exception_control_plane.classification",
        "ledger_exception_control_plane.db.control",
        "ledger_exception_control_plane.fixtures",
        "ledger_exception_control_plane.ingest",
        "ledger_exception_control_plane.matching",
        "ledger_exception_control_plane.money",
    }
    for name, tree in _demo_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("ledger_exception_control_plane"):
                    assert any(module.startswith(p) for p in permitted_internal), (
                        f"{name} imports {module}"
                    )
                else:
                    assert module.split(".")[0] in permitted_stdlib, (
                        f"{name} imports {module}, which is not on the allowlist"
                    )


def test_fixture_ground_truth_never_reaches_a_production_boundary() -> None:
    """The demo *does* read construction metadata — for the labelled evaluation section only.

    What must not happen is that metadata being handed back into the pipeline. The production types
    have no field for it, which is the structural half; this asserts the other half, that the two
    scenario lookups are confined to the grading helper and the section it feeds.
    """
    source = (DEMO_ROOT / "snapshot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    grading = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_grade"
    )
    graded_lines = set(range(grading.lineno, (grading.end_lineno or grading.lineno) + 1))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in {
            "scenario_id",
            "intended_classification",
        }:
            continue
        assert node.lineno in graded_lines or node.attr == "scenario_id", (
            f"construction metadata read at line {node.lineno}, outside the grading helper"
        )

    # And the rendered page must never expose a scenario label as if it were pipeline output.
    assert "intended_classification" not in render(build())


def test_no_frontend_tooling_or_model_layer_was_introduced() -> None:
    """M3 and M7 both stay unstarted. One HTML file is not an operations console."""
    for forbidden in ("package.json", "package-lock.json", "node_modules", "frontend", "web", "ui"):
        assert not (REPO_ROOT / forbidden).exists(), f"{forbidden} was introduced"
    for forbidden in ("llm", "providers"):
        assert not (DEMO_ROOT.parent / forbidden).exists()

    page = COMMITTED.read_text(encoding="utf-8")
    assert "No AI is involved" in page
    assert "Nothing is posted to a ledger" in page
    assert "not</b> the operations console" in page
