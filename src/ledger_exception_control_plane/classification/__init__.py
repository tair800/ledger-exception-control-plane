"""Deterministic residual classification and exception creation — increment M2.3.

Settlement lines M2.2 could not match in; one ``exception`` row each, carrying a class from FR-4's
closed taxonomy and the rule that assigned it. It answers exactly one question:

    *What deterministic exception condition can be proved about this residual from the evidence
    the system holds?*

It does not answer what should be posted. No amount is computed, no account selected, no period
assigned, no treatment proposed — those are M2.4 and M3, and a test walks this package's AST to
keep it that way. There is no model here and none can be: the classifier sees six fields per
settlement line and free text is not among them.

**It is not a second matcher.** :class:`~.engine.SettlementMovement` carries no ledger entry, no
account and no external reference, so pairing a line with an entry is not expressible here. What
the ledger said about a line arrives as a single boolean — whether M2.2 reconciled it — and M2.2
remains the only code in this system that consumes a ledger entry or writes a ``match_result``.

**Two of FR-4's six classes are declared and unreachable, deliberately.** ``partial_capture`` and
``fx_rounding`` are claims about a line's relationship to one particular ledger entry, and no
deterministic key links the two; the residuals that would carry them fall to ``unclassified``
rather than being asserted without evidence. :mod:`.taxonomy` states the reasoning, ADR-045 records
the decision, and the measured effect is in ``PROJECT_STATUS.md``.

* :mod:`.taxonomy` — the rule set, its precedence and its version (OPEN-3, resolved in ADR-045).
* :mod:`.engine` — the decision, as a pure order-independent function.
* :mod:`.service` — reading residuals, and writing one exception each.

Callable without HTTP::

    from ledger_exception_control_plane.classification import run_classification

    run = await run_classification(engine)
"""

from ledger_exception_control_plane.classification.engine import (
    RULES,
    Classification,
    SettlementMovement,
    classify,
)
from ledger_exception_control_plane.classification.service import (
    ClassificationRun,
    correlation_id_for,
    run_classification,
)
from ledger_exception_control_plane.classification.taxonomy import (
    CLASSIFIER_VERSION,
    RULE_CLASSIFICATION,
    RULE_PRECEDENCE,
    ClassificationRule,
    MovementType,
    accounting_period,
    movement_type,
)

__all__ = [
    "CLASSIFIER_VERSION",
    "RULES",
    "RULE_CLASSIFICATION",
    "RULE_PRECEDENCE",
    "Classification",
    "ClassificationRule",
    "ClassificationRun",
    "MovementType",
    "SettlementMovement",
    "accounting_period",
    "classify",
    "correlation_id_for",
    "movement_type",
    "run_classification",
]
