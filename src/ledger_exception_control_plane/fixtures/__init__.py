"""Deterministic synthetic settlement fixtures — increment M1.3.

This package builds the corpus later milestones are tested against: settlement batches with a
declared residual mix across FR-4's taxonomy, a ledger snapshot to make that residual mean
something, deliberately awkward cases, and deliberately malformed files for the quarantine
path.

**It is test and data infrastructure, not reconciliation.** Nothing here parses a settlement
file, normalises a line, matches anything, evaluates a tolerance or classifies an exception.
Those are M2, and building them here — even accidentally, even as a helper — would make every
later test that uses this corpus circular. The scenario metadata records what each condition
was *constructed* to be, which is what a future matcher can be judged against precisely
because no matcher produced it.

Entry points::

    python -m ledger_exception_control_plane.fixtures generate --out fixtures/canonical
    python -m ledger_exception_control_plane.fixtures verify
    python -m ledger_exception_control_plane.fixtures load --dir fixtures/canonical --reset
"""

from pathlib import Path

from ledger_exception_control_plane.fixtures.determinism import (
    FIXTURE_EPOCH,
    FIXTURE_UUID_NAMESPACE,
    Draw,
    fixture_uuid,
)
from ledger_exception_control_plane.fixtures.generator import (
    BULK_DEFAULT_INSTANCES,
    GeneratedCorpus,
    generate,
    residual_mix,
)
from ledger_exception_control_plane.fixtures.loader import (
    CorpusIntegrityError,
    LoadedCorpus,
    UnsafeTargetError,
    load,
    read_corpus,
)
from ledger_exception_control_plane.fixtures.schema import (
    FIXTURE_SCHEMA_VERSION,
    GENERATOR_VERSION,
    Awkwardness,
    Corpus,
    Manifest,
    MatchIntent,
    Profile,
    Scenario,
    ScenarioCatalogue,
    ScenarioKind,
)


def fixtures_module_paths() -> tuple[Path, ...]:
    """Every source file in this package *and any subpackage*, for the guards to walk.

    Discovered rather than listed: a module added here must come under the guards
    automatically, or they protect only the files someone remembered to enumerate.

    ``rglob``, not ``glob``. The non-recursive form left a hole exactly the shape of the thing
    the guards exist to catch — a ``fixtures/matching/`` subpackage would have been invisible
    to all three of them while the docstring claimed otherwise.
    """
    return tuple(sorted(Path(__file__).parent.rglob("*.py")))


__all__ = [
    "BULK_DEFAULT_INSTANCES",
    "FIXTURE_EPOCH",
    "FIXTURE_SCHEMA_VERSION",
    "FIXTURE_UUID_NAMESPACE",
    "GENERATOR_VERSION",
    "Awkwardness",
    "Corpus",
    "CorpusIntegrityError",
    "Draw",
    "GeneratedCorpus",
    "LoadedCorpus",
    "Manifest",
    "MatchIntent",
    "Profile",
    "Scenario",
    "ScenarioCatalogue",
    "ScenarioKind",
    "UnsafeTargetError",
    "fixture_uuid",
    "fixtures_module_paths",
    "generate",
    "load",
    "read_corpus",
    "residual_mix",
]
