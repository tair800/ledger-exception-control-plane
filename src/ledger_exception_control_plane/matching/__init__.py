"""Deterministic reconciliation matching — increment M2.2.

Settlement lines and ledger entries in; ``match_result`` rows for the pairs that can be matched
under the approved rules. It answers exactly one question:

    *Can this settlement line be matched to exactly one ledger entry under the approved rules?*

It does not answer what kind of discrepancy an unmatched line represents. Residual classification
and exception creation are M2.3, and nothing here creates an exception, assigns a taxonomy class or
assembles evidence — a test walks this package's AST to keep it that way.

No model is involved and none can be: the matcher sees five fields per line and five per entry, and
free text is not among them.

* :mod:`.policy` — the rules and the tolerance bands (OPEN-2, resolved in ADR-042).
* :mod:`.engine` — the decision, as a pure order-independent function.
* :mod:`.service` — reading candidates, and persisting a match with the line state atomically.

Callable without HTTP::

    from ledger_exception_control_plane.matching import run_matching

    run = await run_matching(engine, matched_at=when)
"""

from ledger_exception_control_plane.matching.engine import (
    RULE_PRECEDENCE,
    CandidateEntry,
    CandidateLine,
    MatchOutcome,
    ProposedMatch,
    match,
)
from ledger_exception_control_plane.matching.policy import (
    DEFAULT_POLICY,
    EXACT_ONLY_POLICY,
    MatchRule,
    TolerancePolicy,
)
from ledger_exception_control_plane.matching.service import MatchRun, run_matching

__all__ = [
    "DEFAULT_POLICY",
    "EXACT_ONLY_POLICY",
    "RULE_PRECEDENCE",
    "CandidateEntry",
    "CandidateLine",
    "MatchOutcome",
    "MatchRule",
    "MatchRun",
    "ProposedMatch",
    "TolerancePolicy",
    "match",
    "run_matching",
]
