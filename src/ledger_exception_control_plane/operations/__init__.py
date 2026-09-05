"""Operation identity, claim locking and dispatch — `PROJECT_SPEC.md` §12 and §13 (4.1, 4.2).

One sentence from the plan holds 4.1: *one residual, one worker, one operation identifier.* 4.2 adds
the sending of it, and nothing more.

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
dispatches one operation when asked and schedules nothing — bounded retry, backoff, the dead-letter
queue and replay (4.3); the ``UNKNOWN`` reconciliation workflow, the idempotency-window and
inflight-window bounds, the supersession interlock and the manual-recovery queue (4.4) — 4.2 names
the branch that routes to each and walks neither; the naive baseline and the chaos suite (4.5); the
approval gate (5.1) and audit-event emission (5.2).
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
    "IdentifierContradictionError",
    "OperationIdentity",
    "OperationRecord",
    "RecordingRefusedError",
    "ResendDecision",
    "canonical_amount",
    "claim_residuals",
    "derive_identity",
    "dispatch_once",
    "enqueue_posting",
    "instruction_payload_hash",
    "operation_id",
    "outcome_code",
    "reconciliation_is_available",
    "record_operation",
    "resend_decision",
]
