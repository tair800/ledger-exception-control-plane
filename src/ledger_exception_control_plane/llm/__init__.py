"""The model layer, behind a port (M3.2).

What exists here is the *shape* of a model decision and the boundary it must cross — not a model
call. Nothing in this package performs I/O, and no provider SDK is a dependency of this project.

The one sentence the whole design rests on: **a model may choose a treatment, and nothing else.**
:mod:`~.schema` is where that stops being a promise and becomes a type — a closed contract with no
numeric anywhere in its tree and no room for a field it does not name. :mod:`~.port` is where
vendor data stops. Between them, a provider can return an amount only by returning something that
fails validation.

Deliberately absent, and owned by later increments: the cassette harness and any transport
that speaks HTTP (3.4), approval, and posting. Exactly one module here touches a database — the
assembler's persistence layer — and a guard test keeps the rest pure.
"""

from __future__ import annotations

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
    "SYSTEM_POLICY",
    "CandidateEntryFact",
    "CitationError",
    "EvidenceItem",
    "EvidenceRef",
    "ExceptionSubject",
    "ProposalOutcome",
    "ProposalPrompt",
    "ProposalStatus",
    "ProviderId",
    "ProviderRequest",
    "ProviderResponseError",
    "Transport",
    "TreatmentProposal",
    "TreatmentProposer",
    "assemble_evidence",
    "build_prompt",
    "canonical_payload",
    "evidence_id_for",
    "prompt_hash",
    "proposal_wire_schema",
    "propose_treatment",
]
