"""OpenAI Chat Completions adapter (OPEN-5, ADR-049).

The same closed contract, reached along a different path. Structured output arrives as a **JSON
string inside a message inside a choice** — ``choices[0].message.content`` — rather than as a block
in a list, and the system prompt is a message rather than a top-level field. Those two differences
are the substance of provider portability: if the port were shaped around either vendor's response,
the other one would not fit, and the mismatch would be discovered when a provider was swapped under
load rather than here.

There is a third difference with teeth. This API declines in band: ``message.refusal`` carries text
and ``content`` is then null. That is not a proposal and it is not an abstention — an abstention is
something a model chooses inside the contract, a refusal is the API declining to answer at all — so
it raises rather than quietly becoming ``ESCALATE``. The two ``finish_reason`` values that leave
nothing to parse, ``length`` and ``content_filter``, are named individually for the same reason:
each one raises saying what happened, rather than arriving as a malformed-content error about a
provider that behaved exactly as documented.

No SDK, for the same reason as the other adapter: a JSON body in, a JSON body out, replayable
offline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from ledger_exception_control_plane.llm.port import (
    ProviderId,
    ProviderRequest,
    ProviderResponseError,
    Transport,
)
from ledger_exception_control_plane.llm.providers import (
    OPENAI_MODEL_ID,
    OUTPUT_TOKEN_CEILING,
    sent,
    validated_proposal,
)
from ledger_exception_control_plane.llm.schema import (
    ProposalPrompt,
    TreatmentProposal,
    proposal_wire_schema,
)

__all__ = ["OpenAIChatProposer"]

_PATH: Final = "/v1/chat/completions"

#: Names the schema in the provider's own error messages. Not part of the contract.
_SCHEMA_NAME: Final = "treatment_proposal"

#: The trailing ``YYYY-MM-DD`` OpenAI puts on a pinned snapshot.
#:
#: A first attempt split on the last hyphen and read ``17`` off ``gpt-5.4-mini-2026-03-17``,
#: so every model reported ``unversioned`` — the fix for one reviewer's finding introducing a
#: quieter version of the same thing, caught only because the check was re-run.
_DATED_SNAPSHOT: Final = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


class OpenAIChatProposer:
    """Implements :class:`~..port.TreatmentProposer` over Chat Completions."""

    def __init__(self, transport: Transport, *, model_id: str = OPENAI_MODEL_ID) -> None:
        self._transport = transport
        self._model_id = model_id

    @property
    def provider(self) -> ProviderId:
        return ProviderId.OPENAI

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        """The version this vendor publishes, for the model actually called.

        OpenAI's identifiers embed a dated snapshot, and the date is the version: the same family
        on two snapshots is two models. Parsed out of the identifier in use rather than returned as
        a constant — a reviewer overrode the id and the row still claimed the pinned snapshot date,
        naming a model that was never called.

        An identifier with no dated suffix reports ``unversioned`` rather than guessing.
        """
        dated = _DATED_SNAPSHOT.search(self._model_id)
        return dated.group(1) if dated else "unversioned"

    def build_request(self, prompt: ProposalPrompt) -> ProviderRequest:
        """The wire body. Same schema object, different envelope around it.

        The output ceiling is the same number the other adapter sends. Giving the two providers
        different budgets would quietly make the `Measured` comparison unfair, which is the reason
        these constants exist at all.
        """
        return ProviderRequest(
            path=_PATH,
            body={
                "model": self._model_id,
                "max_completion_tokens": OUTPUT_TOKEN_CEILING,
                # A system *message*, where the other adapter uses a top-level field.
                "messages": [
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": _SCHEMA_NAME,
                        # Without this the schema is a suggestion. With it the provider enforces
                        # the same closure the validator enforces on the way back — belt and
                        # braces, and the braces are the ones that are tested.
                        "strict": True,
                        "schema": proposal_wire_schema(),
                    },
                },
            },
        )

    async def propose(self, prompt: ProposalPrompt) -> TreatmentProposal:
        return self.parse(await sent(self._transport, self.build_request(prompt)))

    def parse(self, payload: Mapping[str, object]) -> TreatmentProposal:
        """Walk choice → message → content, refusing anything that is not that shape."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("openai response has no choices")

        first = choices[0]
        if not isinstance(first, Mapping):
            raise ProviderResponseError("openai choice is not an object")

        # Named individually, because the diagnosis is the whole value of the check. Both leave
        # `content` null, so without them the failure surfaces as "content is not a JSON string" —
        # which reads as a malformed provider and sends an operator to look at the wrong thing.
        # A reviewer pointed out that `content_filter` was unhandled while the module docstring
        # claimed `finish_reason` was covered.
        stop = first.get("finish_reason")
        if stop == "length":
            raise ProviderResponseError(
                "openai stopped at the output ceiling before completing the proposal"
            )
        if stop == "content_filter":
            raise ProviderResponseError(
                "openai stopped on a content filter, so there is no proposal to read"
            )

        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ProviderResponseError("openai choice carries no message")

        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            raise ProviderResponseError(f"openai declined to answer: {refusal}")

        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderResponseError("openai message content is not a JSON string")

        return validated_proposal(content, ProviderId.OPENAI)
