"""The RED baseline required by `PROJECT_SPEC.md` §19 (increment 4.5).

A deliberately unsafe implementation of the same job ``src/`` does, kept so the chaos suite has
something that **must fail**. §19: *"A suite that passes on both branches proves nothing and is a
defect."*

Read ``naive/README.md`` first. The short version: every failure here is a failure of *omission*,
each omission has a named counterpart in ``src/``, and nothing in ``src/`` may import this — a guard
test enforces the direction.
"""

from __future__ import annotations

from naive.pipeline import (
    NAIVE_RETRIES,
    NaiveResidual,
    approve,
    claim_open_residuals,
    dispatch,
    dispatch_with_a_stable_key,
    ingest,
    record_adjustment,
)
from naive.schema import NAIVE_TABLES, create_schema, drop_schema, truncate

__all__ = [
    "NAIVE_RETRIES",
    "NAIVE_TABLES",
    "NaiveResidual",
    "approve",
    "claim_open_residuals",
    "create_schema",
    "dispatch",
    "dispatch_with_a_stable_key",
    "drop_schema",
    "ingest",
    "record_adjustment",
    "truncate",
]
