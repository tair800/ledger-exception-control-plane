"""Provider adapters. Vendor-shaped data comes in; a validated proposal goes out.

Two things are shared, and both are the last line of defence rather than convenience helpers.
:func:`validated_proposal` is the single gate every answer passes through, so the adapters can
differ in *where the answer is* and never in *what counts as an answer*. :func:`sent` is the single
place a transport failure becomes a port error, so no vendor exception can reach a caller.
"""

from __future__ import annotations

from collections.abc import Mapping

import pydantic

from ledger_exception_control_plane.llm.cassette import CassetteError, redact_text
from ledger_exception_control_plane.llm.port import (
    ProviderId,
    ProviderRequest,
    ProviderResponseError,
    ProviderUnavailableError,
    Transport,
)
from ledger_exception_control_plane.llm.schema import TreatmentProposal

__all__ = [
    "ANTHROPIC_MODEL_ID",
    "ANTHROPIC_MODEL_VERSION",
    "OPENAI_MODEL_ID",
    "OPENAI_MODEL_VERSION",
    "OUTPUT_TOKEN_CEILING",
    "sent",
    "validated_proposal",
]

#: Pinned for measurement (OPEN-5, ADR-049; pinned 2026-09-01).
#:
#: Identifiers, not defaults to be quietly changed: a `Measured` table that does not name the model
#: it measured is decoration. Constants rather than configuration for the same reason — re-pinning
#: should be a reviewed edit with a date attached, not an environment variable that happened to be
#: set differently on the machine which produced the numbers.
#:
#: The two are stamped differently because the vendors number things differently. OpenAI publishes
#: dated snapshots and the date is part of the identifier. Anthropic's current identifiers carry no
#: date component and appending one names a model that does not exist, so the pin date for that side
#: lives in ADR-049.
ANTHROPIC_MODEL_ID = "claude-opus-5"
OPENAI_MODEL_ID = "gpt-5.4-mini-2026-03-17"

#: What each vendor calls a version, which is not the same thing on both sides.
#:
#: Anthropic's identifiers carry no version component — the identifier is the pinned behaviour — so
#: the version is the identifier and ADR-049 holds the pin date. OpenAI's embed a dated snapshot,
#: and that date is genuinely the version: the same family on two snapshots is two models. 3.3
#: persists whichever applies, so the difference is recorded rather than flattened.
ANTHROPIC_MODEL_VERSION = ANTHROPIC_MODEL_ID
OPENAI_MODEL_VERSION = "2026-03-17"

#: One output ceiling, applied to both providers.
#:
#: A ceiling is required by one API and available on the other, and giving the two different budgets
#: would quietly make the `Measured` comparison unfair — the reason these constants exist at all.
#:
#: The value is not arbitrary. A reviewer caught the first one: 1024 tokens against a model whose
#: reasoning is on by default and counts against the same ceiling, which would have truncated the
#: JSON mid-object and surfaced as a parse error rather than as "the answer did not fit".
OUTPUT_TOKEN_CEILING = 16000


async def sent(transport: Transport, request: ProviderRequest) -> Mapping[str, object]:
    """Send, and turn any failure into a port error.

    Everything a transport can raise is vendor-shaped — a client library's timeout, a connection
    error, an HTTP status wrapper. The port promises callers two exception types, so the
    translation has to happen above the transport rather than inside each implementation of it,
    where a new transport could forget.

    With exactly one exception, below: a harness fault is not a provider fault.
    """
    try:
        payload = await transport.send(request)
    except CassetteError:
        # Straight through, and this is the most consequential line in the module.
        #
        # A cassette miss is a fault in the *harness* — the request changed, or the recording is
        # stale — not in a provider. Wrapping it as unavailability would leave an offline suite
        # passing while it tested nothing, and the failure would read as weather rather than as a
        # bug. It is the one exception that must not be translated.
        raise
    except Exception as exc:  # the whole point is that anything else at all can arrive here
        # Redacted, because a client library's exception routinely carries the request URL and its
        # headers — a reviewer traced a key in a query string all the way into
        # `ProposalOutcome.detail`, which a later increment will put in an audit row. The original
        # exception is still chained for a debugger; only the *message* is cleaned.
        raise ProviderUnavailableError(
            f"the provider could not be reached: {redact_text(repr(exc))}"
        ) from exc

    if not isinstance(payload, Mapping):
        raise ProviderResponseError(
            f"the provider returned {type(payload).__name__}, not an object"
        )
    return payload


def validated_proposal(raw: str, provider: ProviderId) -> TreatmentProposal:
    """Parse provider text into the closed contract, or refuse.

    ``model_validate_json`` rather than ``model_validate`` on a pre-parsed dict, deliberately: the
    input *is* JSON, and validating it as JSON is what makes strict mode meaningful at this
    boundary. A provider that answers ``{"abstained": "true"}`` has not answered — the string is
    not a boolean, and quietly accepting it would record a decision nobody made.
    """
    try:
        return TreatmentProposal.model_validate_json(raw)
    except pydantic.ValidationError as exc:
        raise ProviderResponseError(
            f"{provider.value} returned something that is not a treatment proposal: {exc}"
        ) from exc
