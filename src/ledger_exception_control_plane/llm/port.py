"""The provider-neutral port. Vendor data stops here.

The rest of the system asks for a :class:`~.schema.TreatmentProposal` and gets one, or gets one of
two errors it can reason about. It never sees a vendor response object, a tool-call block, a
``choices`` array, a JSON string, or a ``dict[str, Any]`` — because every one of those would put the
burden of "is this safe?" on whichever caller happened to receive it, and that burden is exactly
what the port exists to discharge once.

Two provider families sit behind it (ADR-049). They differ in more than branding: one returns
structured output inside a content-block list, the other inside a JSON string nested in a message.
Both differences are the adapter's problem and stop at this boundary.

**Async, deliberately, before there is anything to await.** Every other I/O boundary in this system
is async — FastAPI, ``asyncpg``, ``AsyncSession`` — so the transport that speaks HTTP at 3.4 will
be too. A synchronous port would have left exactly one choice at that point: block the event loop,
hide a thread pool behind the interface, or change the port and both adapters. Settling the shape of
this seam *is* the increment, so it is settled for the caller that will actually exist.

**No adapter here performs I/O.** A :class:`Transport` is injected, and none ships in this
increment — the ones that speak HTTP arrive with the cassette harness (3.4), which is also where a
recorded call can be replayed in CI without a key. That is not a limitation dressed up as a choice:
an adapter that owns its socket cannot be tested offline, and this whole layer has to be provable
without a paid call.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from ledger_exception_control_plane.llm.schema import ProposalPrompt, TreatmentProposal

__all__ = [
    "ProviderError",
    "ProviderId",
    "ProviderRequest",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "Transport",
    "TreatmentProposer",
]


class ProviderId(enum.StrEnum):
    """The implemented providers (OPEN-5, ADR-049). Two vendors, not two front doors to one."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ProviderError(Exception):
    """Base for everything this port can raise. Callers may catch exactly this and be complete."""


class ProviderResponseError(ProviderError):
    """A provider answered, and the answer is not a valid proposal.

    Raised rather than returned, and never softened into a default. There is no "assume escalate"
    fallback at this layer: a caller that cannot tell a real abstention from a parse failure would
    record a model decision that no model made. What to do about the failure — queue the exception
    for a human, and never block the deterministic path (NFR-11) — is the caller's decision, and
    3.3 owns it.
    """


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached, or failed before answering.

    A separate type because it is a separate situation, and a reviewer found the port silently
    conflating them: ``propose`` passed transport exceptions straight through, so a timeout would
    have surfaced at 3.4 as a vendor-shaped ``httpx`` error in a caller the port exists to shield
    from vendor vocabulary. The distinction is also operationally real — NFR-11 says unavailability
    must queue the exception for human treatment, which is a different response from "the model
    said something invalid".
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderRequest:
    """What to send. Never how to authenticate.

    The adapter shapes the payload; the transport owns the base URL, the credential and the socket.
    Splitting it this way means no adapter, no test fixture and no recorded cassette can come to
    hold a key — the one place a secret could leak into this repository is the one place that does
    not exist yet.
    """

    path: str
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        # A defensive copy, so the caller's dictionary and the request cannot alias — the mapping
        # handed to a frozen dataclass is not itself frozen, which a reviewer pointed out.
        #
        # A copy rather than a `MappingProxyType`, and that was a correction: the read-only view is
        # stronger, and it is not JSON-serialisable. Every transport's first act is `json.dumps` on
        # this body, so the stricter choice would have handed 3.4 a `TypeError` in exchange for
        # preventing a mutation that only in-process code could perform on an object it just built.
        object.__setattr__(self, "body", dict(self.body))


@runtime_checkable
class Transport(Protocol):
    """Carries a request to a provider and returns the decoded JSON body.

    ``Mapping[str, object]`` is raw provider data, and it is deliberately confined to this protocol
    and the adapter that consumes it. It never crosses the port.

    An implementation may raise whatever its client library raises; the adapter translates it. That
    is the point of putting the translation above the transport rather than inside each one.
    """

    async def send(self, request: ProviderRequest) -> Mapping[str, object]: ...


@runtime_checkable
class TreatmentProposer(Protocol):
    """The port. One method, one return type, no vendor vocabulary.

    ``provider``, ``model_id`` and ``model_version`` are on the interface because a proposal
    without them is not reproducible: the audit trail has to answer *which model said this*, and
    OPEN-5 pins the identifiers precisely so a measured result means something later. 3.3 persists
    all three, which is why the version is a declared property rather than something a caller has
    to know — and the two vendors version differently, so it is the adapter that knows how.

    Raises :class:`ProviderResponseError` if the answer is not a valid proposal, and
    :class:`ProviderUnavailableError` if there was no answer. Nothing else escapes.
    """

    @property
    def provider(self) -> ProviderId: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    async def propose(self, prompt: ProposalPrompt) -> TreatmentProposal: ...
