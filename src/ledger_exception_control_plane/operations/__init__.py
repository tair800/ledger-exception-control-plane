"""Operation identity, claim locking, dispatch and recovery — §12 and §13 (4.1—4.4).

One sentence from the plan holds 4.1: *one residual, one worker, one operation identifier.* 4.2 adds
the sending of it. 4.3 adds the way back from a failure that provably never left the client. 4.4
adds the only thing that can be done about a send whose outcome nobody knows.

- :mod:`~.claim` takes an exclusive, transaction-scoped claim on open residuals with
  ``SELECT … FOR UPDATE SKIP LOCKED``, so two workers cannot hold one residual.
- :mod:`~.identity` derives the retry-independent operation identifier — the same value on attempt
  one and attempt five, bound to the whole posting instruction, and independent of the approver.
- :mod:`~.service` persists that identifier before anything could dispatch it (§12.1.1), writes the
  outbox row **in the same transaction as the state change**, and is the only module in this package
  permitted to write an ``adjustment`` row.
- :mod:`~.dispatcher` sends one already-persisted operation to a
  :mod:`~ledger_exception_control_plane.ledger` adapter, committing a write-ahead attempt record in
  its own transaction before the call, and settles that record with what came back.
- :mod:`~.retry` retries **only** an allowlisted transport failure, under two independent bounds,
  then dead-letters with the envelope its replay command re-reads.
- :mod:`~.reconcile` walks §13.5's capability branch for an operation whose outcome is undetermined:
  query where the adapter can be queried, re-send only inside the declared window *and* scope, and
  otherwise stop. Every query it asks is appended as evidence, and the count of consecutive negative
  answers is derived from those rows rather than stored.
- :mod:`~.recovery` is where the automatic path stops — an operator queue carrying the evidence
  procedure, an SLA, a segregation-of-duties rule the database enforces, and the
  ``RESOLVED_UNVERIFIED`` outcome that makes an unverifiable judgement visible as one.

**The guarantees, at the strength each actually holds**, kept separate because collapsing them is
the failure this project exists to prevent:

1. *Internal processing.* Unconditional. Two workers cannot claim one residual, and at most one
   ``adjustment`` exists per ``operation_id`` — the second enforced by a unique constraint rather
   than by this code.
2. *The transactional outbox.* At-least-once, and only that. It guarantees the intent cannot be
   lost, never that it is delivered once.
3. *Duplicate dispatch prevention.* Bounded by what this system knows. A second send is refused for
   an operation in a **known** terminal state, and refused after an ambiguous one unless capability
   permits it. An outcome nobody observed constrains nothing.
4. *Adapter capability.* Declared data, read and branched on, never inferred — and downgraded to
   ``NONE`` until a conformance run proves it.
5. *An effectively-once financial side effect.* **Conditional**, on the retry-independent identifier
   of (1) together with a *verified* ``ENFORCES_KEY`` or ``BY_OPERATION_ID`` from (4). Where an
   adapter does not meet that bar the claim is withdrawn, not reworded, and this package refuses the
   send rather than guessing. §12.3 is explicit that sending an identifier is a *request* for
   idempotent treatment, honoured only if the provider implements it.

**Deliberately absent, and owned by later increments:** the dispatcher *loop* — this package
dispatches and reconciles one operation when asked and schedules nothing; what drives either is a
deployment decision (10.1), not this package's — the naive baseline and the chaos suite (4.5); and
the extension of audit-event emission to *every* state transition (5.2). 4.4 emits for its own —
every attempt, every ``UNKNOWN``, every query result and every operator decision — which is what its
deliverables name and no more.
"""

from __future__ import annotations

from ledger_exception_control_plane.operations.claim import (
    Claim,
    ClaimedResidual,
    claim_residuals,
)
from ledger_exception_control_plane.operations.dispatcher import (
    DispatchRefusedError,
    DispatchResult,
    ResendDecision,
    dispatch_once,
    outcome_code,
    reconciliation_is_available,
    resend_decision,
)
from ledger_exception_control_plane.operations.identity import (
    INSTRUCTION_DOMAIN_TAG,
    OPERATION_DOMAIN_TAG,
    PAYLOAD_COMPONENTS,
    AmountNotStorableError,
    OperationIdentity,
    canonical_amount,
    derive_identity,
    instruction_payload_hash,
    operation_id,
)
from ledger_exception_control_plane.operations.reconcile import (
    ReconciliationPolicy,
    ReconciliationReport,
    ResendBound,
    Resolution,
    reconcile_once,
    resend_is_within_bounds,
)
from ledger_exception_control_plane.operations.recovery import (
    EvidenceProcedure,
    RecoveryReason,
    RecoveryRefusal,
    RecoveryRefusedError,
    RecoveryView,
    evidence_procedure_for,
    open_items,
    resolve_item,
    stale_items,
)
from ledger_exception_control_plane.operations.service import (
    IdentifierContradictionError,
    OperationRecord,
    RecordingRefusedError,
    enqueue_posting,
    record_operation,
)

__all__ = [
    "INSTRUCTION_DOMAIN_TAG",
    "OPERATION_DOMAIN_TAG",
    "PAYLOAD_COMPONENTS",
    "AmountNotStorableError",
    "Claim",
    "ClaimedResidual",
    "DispatchRefusedError",
    "DispatchResult",
    "EvidenceProcedure",
    "IdentifierContradictionError",
    "OperationIdentity",
    "OperationRecord",
    "ReconciliationPolicy",
    "ReconciliationReport",
    "RecordingRefusedError",
    "RecoveryReason",
    "RecoveryRefusal",
    "RecoveryRefusedError",
    "RecoveryView",
    "ResendBound",
    "ResendDecision",
    "Resolution",
    "canonical_amount",
    "claim_residuals",
    "derive_identity",
    "dispatch_once",
    "enqueue_posting",
    "evidence_procedure_for",
    "instruction_payload_hash",
    "open_items",
    "operation_id",
    "outcome_code",
    "reconcile_once",
    "reconciliation_is_available",
    "record_operation",
    "resend_decision",
    "resend_is_within_bounds",
    "resolve_item",
    "stale_items",
]
