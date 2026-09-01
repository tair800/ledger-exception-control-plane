"""The model layer, behind a port (M3.2).

What exists here is the *shape* of a model decision and the boundary it must cross — not a model
call. Nothing in this package performs I/O, and no provider SDK is a dependency of this project.

The one sentence the whole design rests on: **a model may choose a treatment, and nothing else.**
:mod:`~.schema` is where that stops being a promise and becomes a type — a closed contract with no
numeric anywhere in its tree and no room for a field it does not name. :mod:`~.port` is where
vendor data stops. Between them, a provider can return an amount only by returning something that
fails validation.

Deliberately absent, and owned by later increments: evidence assembly and prompt construction
(3.3), the cassette harness and any transport that speaks HTTP (3.4), persistence of a proposal,
approval, and posting. This package cannot reach a database session, and a guard test keeps it
that way.
"""

from __future__ import annotations

from ledger_exception_control_plane.llm.port import (
    ProviderId,
    ProviderRequest,
    ProviderResponseError,
    Transport,
    TreatmentProposer,
)
from ledger_exception_control_plane.llm.schema import (
    EvidenceRef,
    ProposalPrompt,
    TreatmentProposal,
    proposal_wire_schema,
)

__all__ = [
    "EvidenceRef",
    "ProposalPrompt",
    "ProviderId",
    "ProviderRequest",
    "ProviderResponseError",
    "Transport",
    "TreatmentProposal",
    "TreatmentProposer",
    "proposal_wire_schema",
]
