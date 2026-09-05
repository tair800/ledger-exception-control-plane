"""The ledger adapter port and the reference simulated ledger — §10.1, §13.4 (increment 4.2).

Three modules, one contract:

- :mod:`~.port` declares what an adapter may say. A closed five-valued
  :data:`~.port.PostingOutcome`, a closed three-valued :data:`~.port.QueryOutcome`, the eight-field
  :class:`~.port.LedgerAdapterCapabilities` record, and the port itself — split into
  :class:`~.port.LedgerAdapter` and :class:`~.port.QueryableLedgerAdapter` so that an absent query
  capability is absent from the *type* rather than a method that raises.
- :mod:`~.conformance` is the gate between a declaration and a claim. Two proofs, both measured at
  the ledger, plus the committed record of the run.
- :mod:`~.simulated` is the reference adapter §13.6 names: a simulated ledger declaring
  ``ENFORCES_KEY`` and ``BY_OPERATION_ID``, with an inspectable applied-count.

**What may be claimed, stated once so nobody re-derives it.** §13.5 permits an effectively-once
*financial side effect* only when ``idempotency == ENFORCES_KEY`` **or**
``posting_identity_query == BY_OPERATION_ID``, **and** the operation identifier is stable and
retry-independent. The second conjunct is 4.1's and holds unconditionally. The first is a property
of the **effective** capabilities — the declaration with every unproven claim downgraded — which is
why :func:`~.conformance.capabilities_for` is the only supported way to obtain one.

Where the bar is not met, §13.5 is equally explicit and none of it is optional: do not claim
effectively-once, classify an ambiguous outcome as ``UNKNOWN`` rather than as success or failure,
and do not blindly retry an irreversible financial write. This package makes the first two
structural. The third is 4.4's, and 4.2 discharges it by not retrying at all.

**Nothing here opens a socket, and nothing here computes money.** The reference adapter is
simulated; a real one would need its capability profile established from a vendor's documentation
rather than assumed, which is OPEN-11 and unsettled. Amounts arrive already computed by M2.4 and
already persisted; no module in this package performs arithmetic on one.
"""

from __future__ import annotations

from ledger_exception_control_plane.ledger.conformance import (
    CONFORMANCE_RUNS,
    AdapterInadmissibleError,
    ConformanceReport,
    ConformanceRun,
    assert_admissible,
    capabilities_for,
    run_conformance,
    verified_for,
)
from ledger_exception_control_plane.ledger.port import (
    MAX_POSTING_REF,
    UNBOUNDED,
    Atomicity,
    Confirmed,
    Eventual,
    Found,
    IdempotencyMode,
    IdempotencyScope,
    Indeterminate,
    LedgerAdapter,
    LedgerAdapterCapabilities,
    Linearizable,
    NotFound,
    PartiallyApplied,
    PostingInstruction,
    PostingOutcome,
    PostingQueryMode,
    QueryableLedgerAdapter,
    QueryOutcome,
    Rejected,
    ReversalMode,
    Throttled,
    Unbounded,
    Unknown,
    VerifiedCapabilities,
    effective_capabilities,
)
from ledger_exception_control_plane.ledger.simulated import (
    AppliedPosting,
    Responder,
    SimulatedLedger,
)

__all__ = [
    "CONFORMANCE_RUNS",
    "MAX_POSTING_REF",
    "UNBOUNDED",
    "AdapterInadmissibleError",
    "AppliedPosting",
    "Atomicity",
    "Confirmed",
    "ConformanceReport",
    "ConformanceRun",
    "Eventual",
    "Found",
    "IdempotencyMode",
    "IdempotencyScope",
    "Indeterminate",
    "LedgerAdapter",
    "LedgerAdapterCapabilities",
    "Linearizable",
    "NotFound",
    "PartiallyApplied",
    "PostingInstruction",
    "PostingOutcome",
    "PostingQueryMode",
    "QueryOutcome",
    "QueryableLedgerAdapter",
    "Rejected",
    "Responder",
    "ReversalMode",
    "SimulatedLedger",
    "Throttled",
    "Unbounded",
    "Unknown",
    "VerifiedCapabilities",
    "assert_admissible",
    "capabilities_for",
    "effective_capabilities",
    "run_conformance",
    "verified_for",
]
