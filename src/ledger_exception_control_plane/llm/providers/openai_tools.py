"""OpenAI Chat Completions adapter that carries the schema as a **tool**, not a response format.

Same contract, same validator, same output ceiling as :mod:`.openai_chat`. One difference, and it
is the whole reason this module exists: the closed schema travels in the tool's ``parameters``
with ``tool_choice`` pinned to that function, instead of in ``response_format.json_schema``.

**Why a second envelope rather than a flag on the first.** ``response_format`` with
``strict: true`` is an OpenAI *feature*, not a property of the wire format. An OpenAI-compatible
gateway — a router in front of several vendors, which is what 6.4 measures through — will accept
the key, return 200, and answer with whatever the routed model felt like. Measured against the real
route before a single evaluation call was made:

- ``response_format`` accepted, **not enforced** — the answer came back with ``evidence_ids``
  instead of ``evidence_refs`` and no ``confidence`` at all, and the shared
  :func:`~..providers.validated_proposal` rejected it, correctly;
- the same schema as a forced tool call came back **exactly conformant** and validated first time.

A schema-valid rate of zero measured that way would be a statement about the gateway's feature
support, published as if it were a statement about a model's ability to follow a contract. That is
the same class of error as scoring a synthesised cassette and calling it model accuracy, so the
envelope moved rather than the number being explained away in a footnote.

**Nothing about the safety boundary changes, and that is the point of putting this here.** The
parameters object is :func:`~..schema.proposal_wire_schema` — the identical call the other adapter
makes — and every answer goes through the identical :func:`~..providers.validated_proposal`. There
is no second definition of what a proposal is, so the no-numeric guard that walks the response
contract covers this path by construction rather than by a second test remembering to.

**``stream`` is sent explicitly, and that is not decoration.** The same gateway switches to
server-sent events the moment a request carries ``tools`` or ``response_format`` and no ``stream``
key — a 200 whose body is ``data:`` frames rather than a JSON object. Real OpenAI defaults it to
false; a router in front of it need not. Stating the default is one key on the wire and removes a
failure that would otherwise arrive as an unparseable response from a provider that did nothing
wrong.

No SDK, and no HTTP client: this module is handed a transport, exactly like every other adapter,
which is what keeps the guard on ``llm/`` intact.
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

__all__ = ["OpenAIToolProposer"]

_PATH: Final = "/v1/chat/completions"

#: The function the model is required to call. Also the name a provider puts in its own error
#: messages, which is the only reason it reads like prose rather than like an identifier.
_TOOL_NAME: Final = "treatment_proposal"

#: Told to the model, and deliberately thin. What to *decide* is the system policy's job (3.3);
#: a description that started explaining treatment codes would be a second, unversioned copy of
#: the policy travelling in the request body where no prompt hash covers it.
_TOOL_DESCRIPTION: Final = "Record the single treatment proposal for this exception."

#: The trailing ``YYYY-MM-DD`` OpenAI puts on a pinned snapshot. Same rule as the other adapter,
#: and the same failure it was written against: an identifier with no dated suffix reports
#: ``unversioned`` rather than having a date guessed for it.
_DATED_SNAPSHOT: Final = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


class OpenAIToolProposer:
    """Implements :class:`~..port.TreatmentProposer` over Chat Completions tool calling."""

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
        """The dated snapshot in the identifier, or ``unversioned``.

        A routed alias — ``auto/best-free`` and friends — carries no date, so this reports
        ``unversioned`` and means it. Reporting the *upstream* model a router happened to pick
        would be a version this adapter cannot verify, and the run metadata records what the
        response actually said instead.
        """
        dated = _DATED_SNAPSHOT.search(self._model_id)
        return dated.group(1) if dated else "unversioned"

    def build_request(self, prompt: ProposalPrompt) -> ProviderRequest:
        """The wire body: the same schema, in the envelope a router will actually enforce."""
        return ProviderRequest(
            path=_PATH,
            body={
                "model": self._model_id,
                "max_completion_tokens": OUTPUT_TOKEN_CEILING,
                # Stated rather than defaulted — see the module docstring. A router that streams
                # a tool call answers with `data:` frames and no adapter can parse that.
                "stream": False,
                "messages": [
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": _TOOL_NAME,
                            "description": _TOOL_DESCRIPTION,
                            # The same object the other adapter puts in `response_format`. One
                            # definition of the contract, two places it can travel.
                            "parameters": proposal_wire_schema(),
                        },
                    }
                ],
                # Forced, not `"auto"`. A model permitted to answer in prose instead of calling
                # the tool would produce an unparseable response that looks like a refusal, and
                # the whole reason for this envelope is that the schema is enforced.
                "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
            },
        )

    async def propose(self, prompt: ProposalPrompt) -> TreatmentProposal:
        return self.parse(await sent(self._transport, self.build_request(prompt)))

    def parse(self, payload: Mapping[str, object]) -> TreatmentProposal:
        """Walk choice → message → tool_calls → arguments, refusing anything that is not that."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("openai response has no choices")

        first = choices[0]
        if not isinstance(first, Mapping):
            raise ProviderResponseError("openai choice is not an object")

        # Named individually for the same reason as the sibling adapter: each leaves nothing to
        # parse, and diagnosing them as malformed content sends an operator to the wrong place.
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

        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            # The commonest live failure on a routed endpoint: the model answered in prose despite
            # `tool_choice`. Named, because "content is not a JSON string" would describe the
            # wrong thing entirely.
            raise ProviderResponseError(
                "openai answered without calling the proposal tool, so there is no proposal to read"
            )

        call = calls[0]
        if not isinstance(call, Mapping):
            raise ProviderResponseError("openai tool call is not an object")

        function = call.get("function")
        if not isinstance(function, Mapping):
            raise ProviderResponseError("openai tool call carries no function")

        # Checked, because a router that forwards a *different* tool's call would otherwise have
        # its arguments validated against this contract and — if they happened to fit — recorded
        # as a proposal the model never made for this schema.
        called = function.get("name")
        if called != _TOOL_NAME:
            raise ProviderResponseError(
                f"openai called {called!r} rather than {_TOOL_NAME!r}, so the arguments are not a "
                "treatment proposal"
            )

        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ProviderResponseError("openai tool call arguments are not a JSON string")

        return validated_proposal(arguments, ProviderId.OPENAI)
