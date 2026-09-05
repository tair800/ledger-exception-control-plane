"""Operation identity and claim locking — `PROJECT_SPEC.md` §12 and §13.1 (increment 4.1).

Two mechanisms, one sentence from the plan: *one residual, one worker, one operation identifier.*

- :mod:`~.claim` takes an exclusive, transaction-scoped claim on open residuals with
  ``SELECT … FOR UPDATE SKIP LOCKED``, so two workers cannot hold one residual.
- :mod:`~.identity` derives the retry-independent operation identifier — the same value on attempt
  one and attempt five, bound to the whole posting instruction, and independent of the approver.
- :mod:`~.service` persists that identifier before anything could dispatch it (§12.1.1), and is
  the only module in this package permitted to write an ``adjustment`` row.

**What this package guarantees, stated at the strength it actually holds.** §13.1's Guarantee 1 is
unconditional and internal: two workers cannot claim one residual, and at most one ``adjustment``
exists per ``operation_id`` — the second enforced by a unique constraint rather than by this code.
That is the whole of it. Nothing here says anything about what a downstream ledger does with the
identifier: §12.3 is explicit that sending one is a *request* for idempotent treatment, honoured
only if the provider implements it. The effectively-once financial side effect of §13.5 is
**conditional on declared adapter capability**, no adapter exists yet, and this package must not be
read as claiming it.

**Deliberately absent, and owned by later increments:** the transactional outbox, the dispatcher,
the ledger adapter port and its capability declaration, the write-ahead attempt record — which is
4.2's, and shares §12.1.1 with the persistence rule 4.1 *does* deliver, because one constrains
when the identifier comes into existence and the other what must happen before every socket write
— bounded retry, the dead-letter queue and replay (4.3), ``UNKNOWN`` handling, reconciliation, the
supersession interlock and manual recovery (4.4), the naive baseline and chaos suite (4.5), the
approval gate (5.1) and audit-event emission (5.2). Nothing here opens a socket, writes an outbox
row, or records an attempt.
"""

from __future__ import annotations

from ledger_exception_control_plane.operations.claim import (
    Claim,
    ClaimedResidual,
    claim_residuals,
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
    record_operation,
)

__all__ = [
    "INSTRUCTION_DOMAIN_TAG",
    "OPERATION_DOMAIN_TAG",
    "PAYLOAD_COMPONENTS",
    "AmountNotStorableError",
    "Claim",
    "ClaimedResidual",
    "IdentifierContradictionError",
    "OperationIdentity",
    "OperationRecord",
    "RecordingRefusedError",
    "canonical_amount",
    "claim_residuals",
    "derive_identity",
    "instruction_payload_hash",
    "operation_id",
    "record_operation",
]
