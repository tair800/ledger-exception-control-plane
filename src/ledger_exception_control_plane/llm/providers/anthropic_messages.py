"""Anthropic Messages API adapter (OPEN-5, ADR-049).

Structured output arrives as a **content-block list**: the answer is a ``text`` block, and its text
is the JSON the schema constrained. The list can also carry blocks this adapter has no interest in,
so it searches rather than indexing — a response whose first block is something else is a shape
change, not a failure, and indexing blindly would turn it into a crash.

``stop_reason`` is read before the content is, because this API declines and truncates in band: a
refusal and a hit output ceiling both return HTTP 200 with a body that simply lacks the answer.
Reporting those as "no text block to parse" would throw away the provider's own diagnosis and leave
an operator guessing — a reviewer found precisely that asymmetry against the other adapter, which
had handled its equivalent from the start.

No SDK. The request is a JSON body and the response is a JSON body, which is all this layer needs
and the only form that can be replayed from a cassette in CI without a key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ledger_exception_control_plane.llm.port import (
    ProviderId,
    ProviderRequest,
    ProviderResponseError,
    Transport,
)
from ledger_exception_control_plane.llm.providers import (
    ANTHROPIC_MODEL_ID,
    ANTHROPIC_MODEL_VERSION,
    OUTPUT_TOKEN_CEILING,
    sent,
    validated_proposal,
)
from ledger_exception_control_plane.llm.schema import (
    ProposalPrompt,
    TreatmentProposal,
    proposal_wire_schema,
)

__all__ = ["AnthropicMessagesProposer"]

_PATH: Final = "/v1/messages"


class AnthropicMessagesProposer:
    """Implements :class:`~..port.TreatmentProposer` over the Messages API."""

    def __init__(self, transport: Transport, *, model_id: str = ANTHROPIC_MODEL_ID) -> None:
        self._transport = transport
        self._model_id = model_id

    @property
    def provider(self) -> ProviderId:
        return ProviderId.ANTHROPIC

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        """The version this vendor publishes, for the model actually called.

        Anthropic's current identifiers carry no separate version component — the identifier *is*
        the pinned behaviour — so the version is the identifier, and ADR-049 carries the pin date.

        Derived from ``self._model_id`` rather than returned as a constant, which was the first
        version and was self-contradictory: overriding the model id left the *other* model's name
        sitting in the version column, so one row named two different models. Provenance that can
        disagree with itself is worse than none.
        """
        return self._model_id if self._model_id != ANTHROPIC_MODEL_ID else ANTHROPIC_MODEL_VERSION

    def build_request(self, prompt: ProposalPrompt) -> ProviderRequest:
        """The wire body, including the closed schema itself as the output constraint.

        The schema is not described to the model in prose — it *is* the constraint, generated from
        the same class the response is later validated against. Two copies of a contract drift; one
        copy used at both ends cannot.

        ``max_tokens`` is a generation ceiling, not a field of the proposal, and worth naming
        because it is the only integer here while the contract's whole claim is "no numbers": it
        bounds how much text may be produced and cannot appear in the response. The no-numeric rule
        is a property of the *response* schema, which is what the guards check.
        """
        return ProviderRequest(
            path=_PATH,
            body={
                "model": self._model_id,
                "max_tokens": OUTPUT_TOKEN_CEILING,
                # A top-level field, where the other adapter uses a message — the first of the
                # shape differences that make this a real adapter rather than a rename.
                "system": prompt.system,
                "messages": [{"role": "user", "content": prompt.user}],
                "output_config": {
                    "format": {"type": "json_schema", "schema": proposal_wire_schema()}
                },
            },
        )

    async def propose(self, prompt: ProposalPrompt) -> TreatmentProposal:
        return self.parse(await sent(self._transport, self.build_request(prompt)))

    def parse(self, payload: Mapping[str, object]) -> TreatmentProposal:
        """Read the provider's own verdict first, then find the answer, then validate it."""
        self._raise_for_stop_reason(payload)

        content = payload.get("content")
        if not isinstance(content, list):
            raise ProviderResponseError("anthropic response has no content blocks")

        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                return validated_proposal(text, ProviderId.ANTHROPIC)

        raise ProviderResponseError("anthropic response contains no text block to parse")

    @staticmethod
    def _raise_for_stop_reason(payload: Mapping[str, object]) -> None:
        """Refusal and truncation are answers about the answer. Report them as themselves."""
        stop_reason = payload.get("stop_reason")

        if stop_reason == "refusal":
            details = payload.get("stop_details")
            category = details.get("category") if isinstance(details, Mapping) else None
            raise ProviderResponseError(
                f"anthropic declined to answer (category: {category or 'unspecified'})"
            )

        if stop_reason == "max_tokens":
            raise ProviderResponseError(
                "anthropic stopped at the output ceiling before completing the proposal"
            )
