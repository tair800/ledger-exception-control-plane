"""The model layer, behind a port (M3.2).

What exists here is the *shape* of a model decision and the boundary it must cross — not a model
call. Nothing in this package performs I/O, and no provider SDK is a dependency of this project.

The one sentence the whole design rests on: **a model may choose a treatment, and nothing else.**
:mod:`~.schema` is where that stops being a promise and becomes a type — a closed contract with no
numeric anywhere in its tree and no room for a field it does not name. :mod:`~.port` is where
vendor data stops. Between them, a provider can return an amount only by returning something that
fails validation.

3.4 added the cassette harness, which records and replays without owning a socket: it wraps a
transport it is handed. **No transport that speaks HTTP exists anywhere in this package**, and a
guard test fails the build if any module here imports an HTTP client — which is why the recording
half can exist at all without weakening that claim.

Deliberately absent, and owned by later increments: the golden set and scorer (6.1), the CI
evaluation gate (6.2), the three-arm comparison (6.3), approval, and posting. Exactly one module
here touches a database — the assembler's persistence layer — and a guard test keeps the rest
pure.
"""

from __future__ import annotations

from ledger_exception_control_plane.llm.cassette import (
    CAPTURE_OPT_IN,
    CASSETTE_VERSION,
    IDENTITY_VERSION,
    REDACTED,
    Cassette,
    CassetteError,
    CassetteMalformedError,
    CassetteMissError,
    Interaction,
    Origin,
    RecordingTransport,
    ReplayTransport,
    canonical,
    capture_is_enabled,
    cassette_id_for,
    load_cassette,
    redact_text,
    render_cassette,
    request_fingerprint,
    scrub,
)
from ledger_exception_control_plane.llm.evidence import (
    CandidateEntryFact,
    EvidenceItem,
    ExceptionSubject,
    assemble_evidence,
    evidence_id_for,
)
from ledger_exception_control_plane.llm.flow import (
    CitationError,
    ProposalOutcome,
    ProposalStatus,
    propose_treatment,
)
from ledger_exception_control_plane.llm.port import (
    ProviderId,
    ProviderRequest,
    ProviderResponseError,
    Transport,
    TreatmentProposer,
)
from ledger_exception_control_plane.llm.prompt import (
    SYSTEM_POLICY,
    build_prompt,
    canonical_payload,
    prompt_hash,
)
from ledger_exception_control_plane.llm.schema import (
    EvidenceRef,
    ProposalPrompt,
    TreatmentProposal,
    proposal_wire_schema,
)

__all__ = [
    "CAPTURE_OPT_IN",
    "CASSETTE_VERSION",
    "IDENTITY_VERSION",
    "REDACTED",
    "SYSTEM_POLICY",
    "CandidateEntryFact",
    "Cassette",
    "CassetteError",
    "CassetteMalformedError",
    "CassetteMissError",
    "CitationError",
    "EvidenceItem",
    "EvidenceRef",
    "ExceptionSubject",
    "Interaction",
    "Origin",
    "ProposalOutcome",
    "ProposalPrompt",
    "ProposalStatus",
    "ProviderId",
    "ProviderRequest",
    "ProviderResponseError",
    "RecordingTransport",
    "ReplayTransport",
    "Transport",
    "TreatmentProposal",
    "TreatmentProposer",
    "assemble_evidence",
    "build_prompt",
    "canonical",
    "canonical_payload",
    "capture_is_enabled",
    "cassette_id_for",
    "evidence_id_for",
    "load_cassette",
    "prompt_hash",
    "proposal_wire_schema",
    "propose_treatment",
    "redact_text",
    "render_cassette",
    "request_fingerprint",
    "scrub",
]
