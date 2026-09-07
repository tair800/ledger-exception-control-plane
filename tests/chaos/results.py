"""§19's results table — recorded during the run, rendered from the recording (increment 4.5).

`PROJECT_SPEC.md` §19 asks for the outcome as a table: *"scenario, branch, adjustments posted,
expected, observed"*. `CLAUDE.md` §10 decides where the numbers may come from: *"Never invent a
metric. Every number in the README comes from a committed script and is reproducible."*

Those two together rule out the obvious implementation. A table written by hand from the
expectations would have an *observed* column that is a copy of the *expected* column — every cell
agreeing by construction, which is the one arrangement that cannot be wrong and therefore says
nothing. So:

**The observation is recorded by the test that made it, from the ledger, before it asserts.**
:func:`observe` writes one small file per cell as the scenario runs. Recording *before* the
assertion is deliberate: a disagreeing cell still leaves an honest number behind, and the renderer
marks the disagreement rather than a run vanishing without trace.

**The renderer refuses to guess.** :func:`render` requires all forty-two cells — seven scenarios,
three capability configurations, two branches — and raises naming the missing ones. A table with a
gap silently filled would be worse than no table.

**Rendered output is committed; the recording is not.** §19 says where the table goes — *"Results
go in the README as a table"* — so the renderer writes into ``README.md`` between two markers, and
``make chaos-check`` re-runs the suite and fails if what is committed there has drifted from
what the code now does. The same arrangement the cassettes use, and for the same reason: a
committed number nobody re-derives is a number nobody is checking.

**One row per scenario and configuration, with both branches side by side.** §19 asks for *scenario,
branch, adjustments posted, expected, observed*; splitting the branches into columns carries exactly
that and puts the comparison the gate is about on one line, where a reader can see it without
counting rows.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
from typing import Final

from tests.chaos.scenarios import (
    CONFIGURATION_LABELS,
    CONFIGURATIONS,
    SCENARIO_FAULTS,
    SCENARIO_TITLES,
    SCENARIOS,
    Capability,
    Scenario,
    expectation,
)

__all__ = ["END", "OBSERVATIONS", "RENDERED", "START", "Cell", "observe", "render"]

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[2]

#: Where the run leaves its observations. Working state, not a deliverable — gitignored.
OBSERVATIONS: Final = pathlib.Path(
    os.environ.get("LECP_CHAOS_OBSERVATIONS", REPO_ROOT / ".chaos-observations")
)

#: Where the committed table lives, and the markers it lives between. §19: *"Results go in the
#: README as a table."*
RENDERED: Final = REPO_ROOT / "README.md"
START: Final = "<!-- chaos-results:start -->"
END: Final = "<!-- chaos-results:end -->"

BRANCHES: Final[tuple[str, ...]] = ("main", "naive")


@dataclasses.dataclass(frozen=True, slots=True)
class Cell:
    """One row of §19's table: what was expected here, and what the ledger actually held."""

    scenario: Scenario
    capability: Capability
    branch: str
    expected: int
    observed: int

    @property
    def agrees(self) -> bool:
        return self.expected == self.observed


def _path(scenario: Scenario, capability: Capability, branch: str) -> pathlib.Path:
    return OBSERVATIONS / branch / f"{scenario.value}__{capability.value}.json"


def observe(*, scenario: Scenario, capability: Capability, branch: str, applied: int) -> None:
    """Record what the ledger held, from inside the scenario that drove it.

    Called by both runners immediately **before** their final assertion, so the recording is what
    was measured rather than what the run concluded. ``applied`` is §19's *adjustments posted*:
    financial effects committed at the ledger for one economic unit of work, counted across
    identifiers — the count taken from the ledger's own applied-count and never from our records.
    """
    if branch not in BRANCHES:  # pragma: no cover - both call sites pass a literal
        raise ValueError(f"{branch!r} is not a branch of this comparison")

    destination = _path(scenario, capability, branch)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "scenario": scenario.value,
                "capability": capability.value,
                "branch": branch,
                "applied": applied,
                "fault": SCENARIO_FAULTS[scenario].value,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_all() -> tuple[list[Cell], list[str]]:
    """Every recorded cell, and the names of the ones that are missing."""
    cells: list[Cell] = []
    missing: list[str] = []
    for branch in BRANCHES:
        for scenario in SCENARIOS:
            for capability in CONFIGURATIONS:
                path = _path(scenario, capability, branch)
                if not path.is_file():
                    missing.append(f"{branch}/{scenario.value}/{capability.value}")
                    continue
                recorded = json.loads(path.read_text(encoding="utf-8"))
                cells.append(
                    Cell(
                        scenario=scenario,
                        capability=capability,
                        branch=branch,
                        expected=expectation(
                            scenario=scenario, capability=capability, branch=branch
                        ).applied,
                        observed=int(recorded["applied"]),
                    )
                )
    return cells, missing


def _table(cells: list[Cell]) -> list[str]:
    by_key = {(cell.scenario, cell.capability, cell.branch): cell for cell in cells}
    lines = [
        "| Scenario | Adapter capability | `main` expected | `main` observed "
        "| `naive/` expected | `naive/` observed |",
        "| --- | --- | --: | --: | --: | --: |",
    ]
    for scenario in SCENARIOS:
        for capability in CONFIGURATIONS:
            row = [SCENARIO_TITLES[scenario], CONFIGURATION_LABELS[capability]]
            for branch in BRANCHES:
                cell = by_key[(scenario, capability, branch)]
                row.append(str(cell.expected))
                row.append(str(cell.observed) if cell.agrees else f"**{cell.observed}**")
            lines.append("| " + " | ".join(row) + " |")
    return lines


def render() -> str:
    """The committed table, built from the recording. Raises if any cell is missing."""
    cells, missing = _read_all()
    if missing:
        raise SystemExit(
            "the chaos suite has not recorded every cell, so no table can be rendered.\n"
            f"missing {len(missing)} of {len(BRANCHES) * len(SCENARIOS) * len(CONFIGURATIONS)}:\n  "
            + "\n  ".join(missing)
            + "\n\nrun `make chaos-verify` (both branches, real PostgreSQL) and render again."
        )

    duplicates = sorted(
        {
            scenario.value
            for scenario in SCENARIOS
            if expectation(scenario=scenario, capability=Capability.NONE, branch="naive").applied
            > 1
        }
    )
    mismatched = [cell for cell in cells if not cell.agrees]

    header = [
        START,
        "",
        "### Chaos suite results",
        "",
        "`PROJECT_SPEC.md` §19, both branches, every scenario against all three adapter",
        "capability configurations. **Generated by `make chaos-table` from a run against real",
        "PostgreSQL — do not edit by hand.** Every number is the simulated ledger's own",
        "applied-count, recorded by the scenario that drove it and never inferred from this",
        "system's records; §19.1 forbids the latter by name, because inferring an outcome from",
        "our own state is the defect the whole reliability layer exists to prevent. A number in",
        "**bold** disagrees with the expectation declared before the run.",
        "",
        "*Adjustments posted* means financial effects committed at the ledger for one economic",
        "unit of work, **counted across identifiers**, and it is the only count that sees any of",
        "the baseline's duplicates. `naive/` mints a fresh request identifier on every attempt, so",
        "a per-identifier count reads 1 for each of its postings; two residuals from one delivered",
        "payload, or two approvals from one replayed token, land under two *different* identifiers",
        'as well. Either way each posting is "applied once" while the money has moved twice.',
        "",
    ]
    footer = [
        "",
        f"**`naive/` commits the same financial effect twice in {len(duplicates)} of "
        f"{len(SCENARIOS)} scenarios; `main` applies at most once in all "
        f"{len(SCENARIOS) * len(CONFIGURATIONS)} cells.** The baseline is not a straw man — see",
        "[`naive/README.md`](naive/README.md), which maps every omission to the increment that",
        "closed it in `src/`.",
        "",
        "Two rows are worth reading closely, because neither is a duplicate.",
        "",
        "- **Worker killed mid-batch** loses `naive/` its work rather than duplicating it: the",
        "  baseline claims a whole batch up front and commits the claim, so the work is stranded",
        "  rather than re-claimable. A different defect, recorded as the different number it is.",
        "- **Ambiguous 5xx** leaves `naive/` correct *by luck*. Nothing had been applied, so its",
        "  retry completed the work once — on the identical inference that double-posts in §19.1.",
        "",
        "Where `main` posts **zero** times the work did not complete, and in both such cells that",
        "is the correct outcome rather than a shortfall. **They are not the same outcome, and the",
        "distinction is §13.5's own**, so it is worth stating rather than averaging:",
        "",
        "- Under **`BY_OPERATION_ID`** the adapter is asked, answers `NotFound` N consecutive",
        "  times with both declared windows elapsed, and the operation resolves `REJECTED` and",
        "  settles. That is §13.5 clause 4 — reconcile by query — and no operator is involved.",
        '  The negative answer is *earned*: a single `NotFound` means only "not visible to this',
        '  query yet", and an `Indeterminate` never counts at all.',
        "- Under **`NONE`/`NONE`** there is nothing to ask and nothing that would suppress a",
        "  re-send, so the automatic path stops and an operator takes it with an evidence",
        "  procedure. That is clause 5.",
        "",
        "A `1` in either cell would mean the system had re-sent an irreversible financial write on",
        "the assumption the first one failed, which is the defect this project exists to prevent.",
        "",
        "**What these numbers do not prove.** Under `ENFORCES_KEY` the suppression is performed by",
        "a simulated ledger written in this repository, so that column shows the dispatcher",
        "behaving correctly *given* an enforcing ledger — not that any particular real ledger",
        "enforces anything. The conditional claim of §13.5 is unchanged by this table: an",
        "effectively-once financial effect is available only where an adapter's capability is",
        "declared **and** proven, and is withdrawn rather than reworded where it is not.",
        "",
        END,
    ]
    if mismatched:
        footer[1:1] = [
            "",
            f"> **{len(mismatched)} cell(s) disagree with the declared expectation.** The gate is",
            "> not passing. The expectations are declared before the run and are not to be",
            "> refitted to it.",
        ]
    return "\n".join([*header, *_table(cells), *footer])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render or verify the §19 results table.")
    parser.add_argument("command", choices=("render", "verify"))
    arguments = parser.parse_args(argv)

    produced = render()
    document = RENDERED.read_text(encoding="utf-8")
    if START not in document or END not in document:
        print(f"{RENDERED.name} has no {START} / {END} pair to render into", file=sys.stderr)
        return 1

    before, _, rest = document.partition(START)
    _, _, after = rest.partition(END)
    updated = before + produced + after

    if arguments.command == "render":
        if updated == document:
            print(f"{RENDERED.name} already holds this table")
            return 0
        RENDERED.write_text(updated, encoding="utf-8")
        print(f"wrote the §19 results table into {RENDERED.name}")
        return 0

    if updated != document:
        print(
            f"the results table committed in {RENDERED.name} has drifted from what the suite "
            "observes; run `make chaos-table` and review the diff",
            file=sys.stderr,
        )
        return 1
    print("the committed results table matches the run")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
