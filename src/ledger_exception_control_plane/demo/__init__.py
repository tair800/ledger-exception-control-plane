"""A static visual snapshot of the completed M2 pipeline.

Runs the real deterministic boundaries once over a generated corpus and renders one standalone HTML
page: what ingestion made of the files, what the matcher cleared, what the classifier could prove
about the residual, and what the calculator would price or refuse.

**It is a demo artifact, not a product surface.** It decides nothing — no parser, no matching rule,
no taxonomy, no formula — and a guard test walks this package's AST to keep it so. Its job is
*pipeline output → aggregation → HTML*, and if a number on the page is wrong then the pipeline is
wrong, which is the only thing that makes such a page worth showing.

**Not the operations console.** That is a later milestone with a real UI, filters and an approval
flow. This is one static file with no server, no JavaScript and no dependencies, intended for a
developer or a portfolio reader who wants to see what the deterministic core actually does.

**No database.** Every M2 boundary has a pure entry point, so nothing here connects to PostgreSQL,
starts a container, or leaves anything to clean up.

Usage::

    make m2-demo          # render artifacts/m2-demo.html
    make m2-demo-check    # fail if the committed page has drifted from the pipeline
"""

from ledger_exception_control_plane.demo.render import REGENERATE_COMMAND, render
from ledger_exception_control_plane.demo.snapshot import (
    DEFAULT_INSTANCES,
    DEFAULT_SEED,
    DEMO_TREATMENT,
    CalculatorCounts,
    ClassificationCounts,
    Example,
    GroundTruth,
    IngestionCounts,
    MatchingCounts,
    PipelineSnapshot,
    build,
)

__all__ = [
    "DEFAULT_INSTANCES",
    "DEFAULT_SEED",
    "DEMO_TREATMENT",
    "REGENERATE_COMMAND",
    "CalculatorCounts",
    "ClassificationCounts",
    "Example",
    "GroundTruth",
    "IngestionCounts",
    "MatchingCounts",
    "PipelineSnapshot",
    "build",
    "render",
]
