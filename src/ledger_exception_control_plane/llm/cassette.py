"""Record and replay provider interactions (increment 3.4).

The point is stated in the plan in one line: **CI evaluation with no live API key.** Everything here
serves that, and the exit criterion is that the suite runs offline from cassettes.

**Replay is matched on a fingerprint of the whole request, not on the prompt alone.** That choice
does most of the work in this module. A cassette answers a request only if the bytes that would have
gone to the provider are the ones that were sent when it was recorded — so a changed prompt, a
changed response schema, a changed output ceiling or a changed model all produce a *miss* rather
than a stale answer. Staleness detection is not a separate mechanism; it is the absence of a match.

**A miss is loud, and never looks like a provider outage.** :class:`CassetteError` is not a
:class:`~.port.ProviderError` and is re-raised through the adapters' error translation untouched.
If a miss were reported as "provider unavailable", an offline suite would keep passing while testing
nothing, and the failure would look like weather rather than a bug. There is no fallback from replay
to a live call — not a discouraged one, not a configurable one.

**Nothing here can open a socket.** Recording wraps whatever transport it is handed; it does not
own one, and no HTTP client is imported anywhere in this package. That is what lets the guard M3.2
wrote stay true word for word while the recording half of the harness exists. An operator
capturing real
cassettes supplies the transport that speaks HTTP, explicitly, having opted in.

**Scrubbing happens before anything is written**, never at read time, so a secret cannot reach a
file
even if the process dies immediately afterwards. What it removes is defined by `PROJECT_SPEC.md`
§17: authorisation headers and provider identifiers, plus any value shaped like a credential.

That last clause has a consequence worth stating rather than discovering. Evidence packs carry
third-party text, so a model can quote a secret-shaped string back inside its own rationale — and
scrubbing will rewrite it. **Scrubbing is therefore not answer-preserving in general**, and the
earlier claim that it was has been withdrawn rather than reworded. The rule that replaces it is the
one that actually matters: a scrubbed payload must still *parse* to a valid proposal with the same
treatment, confidence and citations, and a secret must never reach a committed file even if a model
quoted it. Free text loses the quoted secret. That is the correct trade, and it is asserted.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from ledger_exception_control_plane.llm.port import ProviderId, ProviderRequest, Transport

__all__ = [
    "CAPTURE_OPT_IN",
    "CASSETTE_VERSION",
    "IDENTITY_VERSION",
    "REDACTED",
    "Cassette",
    "CassetteError",
    "CassetteMalformedError",
    "CassetteMissError",
    "Interaction",
    "Origin",
    "RecordingTransport",
    "ReplayTransport",
    "canonical",
    "capture_is_enabled",
    "cassette_id_for",
    "load_cassette",
    "redact_text",
    "render_cassette",
    "request_fingerprint",
    "scrub",
]

#: Bumped when the on-disk shape changes. A cassette recorded under another version is refused
#: rather than guessed at — a harness that reads an unknown format is not a harness.
CASSETTE_VERSION: Final = "1"

#: Bumped only when the *meaning* of a request identity changes, and deliberately separate from the
#: file-format version.
#:
#: A reviewer pointed out that folding the two together meant adding a field to the file would
#: silently re-key every fingerprint and every derived id — orphaning any stored provenance that
#: pointed at them. Adding a field to a file is not a change to what was asked of a provider.
IDENTITY_VERSION: Final = "1"

#: The environment variable that must be set before anything may record.
#:
#: Deliberately without the ``LECP_`` prefix. Every ``LECP_`` name belongs to the application's
#: settings namespace, which is strict and forbids extras — so a reviewer showed that documenting a
#: ``LECP_``-prefixed opt-in in ``.env.example``, exactly as §17 asks, would break startup for
#: anyone who copied it into ``.env``. A capture switch is not application configuration.
CAPTURE_OPT_IN: Final = "CASSETTE_CAPTURE"

#: What a scrubbed value becomes. A visible marker rather than a deletion, so a reader can see that
#: something was removed and a test can assert on it.
REDACTED: Final = "[scrubbed]"

_HASH_DOMAIN: Final = "lecp.cassette"

#: Keys removed wherever they appear, matched whole and case-insensitively.
#:
#: §17's authorisation headers, and the provider identifiers both vendors assign. Neither adapter
#: reads any of them.
_SCRUBBED_KEYS: Final = frozenset(
    {
        "authorization", "proxy-authorization", "x-api-key", "api-key", "api_key", "apikey",
        "cookie", "set-cookie", "openai-organization", "openai-project",
        "id", "request_id", "x-request-id", "system_fingerprint",
    }
)  # fmt: skip

#: Values shaped like a credential, whatever key they arrived under.
#:
#: Case-insensitive and covering more families than the vendor pair in scope, because the cost of a
#: false positive here is a redacted word in free text and the cost of a miss is a committed secret.
#: A reviewer got uppercase forms and several other providers' key shapes past the first version.
_SECRET_SHAPED: Final = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{12,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|AIza[A-Za-z0-9_\-]{20,}"
    r"|AKIA[A-Z0-9]{12,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})",
    re.IGNORECASE,
)


class Origin(enum.StrEnum):
    """How an interaction came to exist. Recorded, because the difference matters.

    A cassette is supposed to be a recording of something a provider really said. A response
    somebody wrote by hand is a fixture, and calling it a recording would misrepresent what the
    suite is testing against — the repository would appear to hold evidence of real model behaviour
    that nobody ever obtained.

    So the file says which it is, every interaction carries it, and a test asserts it over every
    cassette in the tree. The ones committed here are ``SYNTHESISED``: they exercise replay,
    determinism, scrubbing and the adapters' real parsing against the documented response shapes,
    and they are not evidence about any model. Capturing real ones needs a credential and an
    explicit opt-in, and is what the evaluation increments (6.1, 6.2 and 6.3) will do.
    """

    CAPTURED = "captured"
    SYNTHESISED = "synthesised"


class CassetteError(Exception):
    """A fault in the harness, never in the provider.

    Deliberately **not** a :class:`~.port.ProviderError`. The adapters translate everything a
    transport raises into provider errors so that a caller sees one of two failure modes — and a
    cassette miss is neither. It is a bug in the recording or in the code under test, and it has to
    reach the surface as one.
    """


class CassetteMissError(CassetteError):
    """No recorded interaction matches this request.

    Which is the same event as "the request changed since the cassette was recorded". There is no
    fallback to a live call, so this is where a stale cassette stops the suite.
    """


class CassetteMalformedError(CassetteError):
    """The file is not a cassette this version can read."""


@dataclasses.dataclass(frozen=True, slots=True)
class Interaction:
    """One recorded exchange: what identified the request, and what came back.

    ``prompt_hash`` is deliberately absent. The first version carried one, supplied once when a
    recorder was constructed — so a recorder used for more than one prompt, which is the ordinary
    shape of a capture run, stamped every interaction with the first prompt's hash. A provenance
    field that is wrong for all but one row is worse than no field: the request fingerprint is the
    identity, and the prompt hash belongs on the proposal, where the flow computes it per call.
    """

    cassette_id: str
    provider: ProviderId
    model_id: str
    model_version: str
    path: str
    request_fingerprint: str
    origin: Origin
    response: Mapping[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class Cassette:
    """A file's worth of interactions, addressed by request fingerprint."""

    interactions: tuple[Interaction, ...]

    def find(self, fingerprint: str) -> Interaction | None:
        for interaction in self.interactions:
            if interaction.request_fingerprint == fingerprint:
                return interaction
        return None


def canonical(payload: Any) -> str:
    """The one serialisation this module uses, everywhere.

    Sorted keys, fixed separators, escaped non-ASCII. Every degree of freedom left in a serialiser
    is a way for two identical things to compare unequal, and this text is both hashed and written
    to a file that must round-trip byte for byte.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def request_fingerprint(request: ProviderRequest) -> str:
    """What replay matches on: the path and the whole body, canonically.

    The *whole* body, so that anything which changes what the provider is asked — the prompt, the
    response schema that constrains its answer, the output ceiling, the model — changes the
    fingerprint and produces a miss.
    """
    return hashlib.sha256(
        "\n".join((_HASH_DOMAIN, IDENTITY_VERSION, request.path, canonical(request.body))).encode()
    ).hexdigest()


def cassette_id_for(provider: ProviderId, model_id: str, fingerprint: str) -> str:
    """A stable id for one interaction, suitable for `treatment_proposal.cassette_id`.

    Derived rather than random, so re-recording an identical exchange produces the same id and a
    stored proposal keeps pointing at the thing that produced it. Keyed on the identity version
    rather than the file-format version, so adding a field to the file cannot orphan it.
    """
    return hashlib.sha256(
        "\n".join((_HASH_DOMAIN, IDENTITY_VERSION, provider.value, model_id, fingerprint)).encode()
    ).hexdigest()


def redact_text(text: str) -> str:
    """Replace anything credential-shaped in a free string.

    Exposed because a secret can escape through a channel that is not a cassette: a client
    library's exception message routinely embeds the request URL, and an auth header or a key in a
    query string travels with it. A reviewer traced one into ``ProposalOutcome.detail``.
    """
    return _SECRET_SHAPED.sub(REDACTED, text)


def scrub(payload: Any) -> Any:
    """Remove authorisation headers, provider identifiers and anything credential-shaped.

    Applied before a cassette is written and never at read time, so a secret cannot reach disk even
    briefly. Recursive, and whole-key matching rather than substring matching — ``evidence_id``
    contains ``id`` and must survive.
    """
    if isinstance(payload, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str) and key.lower() in _SCRUBBED_KEYS
            else scrub(value)
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple, set)):
        return [scrub(item) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


def render_cassette(interactions: Iterable[Interaction]) -> str:
    """The file text. Canonical, totally ordered, newline-terminated.

    Sorted by fingerprint *and* id, which is a total order once duplicates are refused on load —
    the first version sorted by id alone and was therefore order-dependent whenever two entries
    shared one, which a real capture produces the moment the same request is sent twice.
    """
    document = {
        "cassette_version": CASSETTE_VERSION,
        "interactions": [
            {
                "cassette_id": item.cassette_id,
                "provider": item.provider.value,
                "model_id": item.model_id,
                "model_version": item.model_version,
                "path": item.path,
                "request_fingerprint": item.request_fingerprint,
                "origin": item.origin.value,
                "response": item.response,
            }
            for item in sorted(interactions, key=lambda i: (i.request_fingerprint, i.cassette_id))
        ],
    }
    return canonical(document) + "\n"


def load_cassette(path: pathlib.Path) -> Cassette:
    """Read a cassette, refusing anything this version does not understand.

    Every failure becomes a :class:`CassetteMalformedError`. The first version caught only
    ``OSError`` and ``JSONDecodeError``, so a bit flip that broke the UTF-8 and a deeply nested
    document escaped as ``UnicodeDecodeError`` and ``RecursionError`` — two reviewers found the
    same gap, and "a harness that reads an unknown format is not a harness" has to include the
    formats nobody anticipated.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise CassetteMalformedError(f"{path.name} is not readable as a cassette: {exc}") from exc

    if not isinstance(document, Mapping):
        raise CassetteMalformedError(f"{path.name} is not a cassette object")

    version = document.get("cassette_version")
    if version != CASSETTE_VERSION:
        raise CassetteMalformedError(
            f"{path.name} is cassette version {version!r}, and this is version {CASSETTE_VERSION}"
        )

    raw = document.get("interactions")
    if not isinstance(raw, list):
        raise CassetteMalformedError(f"{path.name} has no interactions")

    interactions = tuple(_interaction(path, entry) for entry in raw)

    # Duplicates are refused rather than resolved. `find` returns the first match, so a second
    # recording of the same request would be unreachable — and the file's ordering would depend on
    # the order the recorder happened to see them, which a reviewer showed breaks the drift check.
    # The format holds one answer per request; more than one is a decision for the increments that
    # sample a model repeatedly, not something to allow by accident here.
    seen: dict[str, int] = {}
    for item in interactions:
        seen[item.request_fingerprint] = seen.get(item.request_fingerprint, 0) + 1
    duplicated = sorted(f for f, count in seen.items() if count > 1)
    if duplicated:
        raise CassetteMalformedError(
            f"{path.name} records more than one answer for {len(duplicated)} request(s): "
            f"{[f[:12] for f in duplicated]}"
        )

    return Cassette(interactions=interactions)


def _text(path: pathlib.Path, entry: Mapping[str, Any], field: str) -> str:
    """A required string field, refused rather than coerced.

    ``str(entry[field])`` was the first version, and a reviewer put a JSON ``null`` in a fingerprint
    and got the literal string ``"None"`` — several of which collide on one lookup key. Coercion at
    a trust boundary turns a malformed file into a plausible one.
    """
    value = entry.get(field)
    if not isinstance(value, str):
        raise CassetteMalformedError(
            f"{path.name} has an interaction whose {field} is {type(value).__name__}, not a string"
        )
    return value


def _interaction(path: pathlib.Path, entry: Any) -> Interaction:
    if not isinstance(entry, Mapping):
        raise CassetteMalformedError(f"{path.name} holds an interaction that is not an object")

    required = (
        "cassette_id",
        "provider",
        "model_id",
        "model_version",
        "path",
        "request_fingerprint",
        "origin",
        "response",
    )
    missing = [field for field in required if field not in entry]
    if missing:
        raise CassetteMalformedError(f"{path.name} has an interaction missing {missing}")

    response = entry["response"]
    if not isinstance(response, Mapping):
        raise CassetteMalformedError(
            f"{path.name} has an interaction whose response is not an object"
        )

    try:
        provider = ProviderId(_text(path, entry, "provider"))
    except ValueError as exc:
        raise CassetteMalformedError(f"{path.name} names an unknown provider") from exc

    try:
        origin = Origin(_text(path, entry, "origin"))
    except ValueError as exc:
        raise CassetteMalformedError(f"{path.name} declares an unknown origin") from exc

    return Interaction(
        cassette_id=_text(path, entry, "cassette_id"),
        provider=provider,
        model_id=_text(path, entry, "model_id"),
        model_version=_text(path, entry, "model_version"),
        path=_text(path, entry, "path"),
        request_fingerprint=_text(path, entry, "request_fingerprint"),
        origin=origin,
        response=response,
    )


def capture_is_enabled() -> bool:
    """Whether the operator has explicitly opted in to recording."""
    return os.environ.get(CAPTURE_OPT_IN) == "1"


class ReplayTransport:
    """Answers from a recording, or fails. Never from a network.

    Implements :class:`~.port.Transport`, so an adapter cannot tell it apart from anything else —
    which is the point: the code under test is the real adapter, exercised on the real bytes a
    provider returned.
    """

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self.served: list[str] = []

    async def send(self, request: ProviderRequest) -> Mapping[str, Any]:
        fingerprint = request_fingerprint(request)
        interaction = self._cassette.find(fingerprint)
        if interaction is None:
            raise CassetteMissError(
                f"no recorded interaction for request fingerprint {fingerprint[:12]}…; "
                "the request changed, or the cassette is stale. Nothing is called live."
            )

        self._assert_identity_agrees(interaction, request)
        self.served.append(interaction.cassette_id)
        # A copy. The stored payload is shared by every replay from this cassette, and a caller that
        # mutated what it was handed would change what the next replay sees — the same aliasing the
        # request side already guards against.
        return copy.deepcopy(dict(interaction.response))

    @staticmethod
    def _assert_identity_agrees(interaction: Interaction, request: ProviderRequest) -> None:
        """The recording's declared identity must match the request it is answering.

        A matching fingerprint already proves the path and body are the ones that were recorded, so
        this catches a file whose *metadata* disagrees with its own contents — which is what a
        stored ``cassette_id`` would carry into a provenance record. A reviewer served an
        interaction declaring one vendor's model to the other vendor's adapter, and nothing
        objected.
        """
        if interaction.path != request.path:
            raise CassetteMalformedError(
                f"cassette {interaction.cassette_id[:12]} declares path {interaction.path!r} "
                f"but answers a request to {request.path!r}"
            )

        model = request.body.get("model")
        if isinstance(model, str) and model != interaction.model_id:
            raise CassetteMalformedError(
                f"cassette {interaction.cassette_id[:12]} declares model {interaction.model_id!r} "
                f"but answers a request for {model!r}"
            )


class RecordingTransport:
    """Wraps another transport and keeps what it saw, scrubbed.

    It owns no socket. Whatever it is given is what talks to the provider, which is how the
    recording half of this harness exists without any HTTP client entering the package, and
    therefore without weakening the guard that says none may.

    Constructing one requires the capture opt-in, because construction is the last point before a
    real request could be made.
    """

    def __init__(
        self,
        inner: Transport,
        *,
        provider: ProviderId,
        model_id: str,
        model_version: str,
    ) -> None:
        if not capture_is_enabled():
            raise CassetteError(
                f"recording requires {CAPTURE_OPT_IN}=1. Capture can reach a paid API, so it is "
                "never the default and never inferred."
            )
        self._inner = inner
        self._provider = provider
        self._model_id = model_id
        self._model_version = model_version
        self.recorded: list[Interaction] = []

    async def send(self, request: ProviderRequest) -> Mapping[str, Any]:
        response = await self._inner.send(request)

        # Checked, not coerced. `dict(response)` was the first version, and it ran *before* the
        # port's own non-object check could — so a provider answering with a JSON array became
        # either a fabricated one-key recording or an "unavailable" error about a provider that
        # had answered. Refusing here lets `sent()` report what actually happened.
        if not isinstance(response, Mapping):
            raise CassetteError(
                f"cannot record a {type(response).__name__} response; a provider answered with "
                "something that is not an object, which is a provider fault rather than a capture "
                "one and must not become a recording"
            )

        fingerprint = request_fingerprint(request)
        self._assert_request_is_ours(request)
        scrubbed = scrub(dict(response))

        self.recorded.append(
            Interaction(
                cassette_id=cassette_id_for(self._provider, self._model_id, fingerprint),
                provider=self._provider,
                model_id=self._model_id,
                model_version=self._model_version,
                path=request.path,
                request_fingerprint=fingerprint,
                # Only this class may claim a capture, because only this class has been handed a
                # transport that could have spoken to a provider.
                origin=Origin.CAPTURED,
                response=scrubbed,
            )
        )
        # A copy of the scrubbed payload: what the caller sees during a capture run is what a later
        # replay will see, and mutating it must not reach what gets written to the file.
        return copy.deepcopy(scrubbed)

    def _assert_request_is_ours(self, request: ProviderRequest) -> None:
        """The declared identity must describe the request actually being sent.

        A recorder is told which provider and model it is recording for. If that disagrees with the
        request, the cassette would carry provenance about a call that never happened — the same
        self-contradicting provenance the ``model_version`` properties were rewritten to prevent,
        reintroduced one layer down. A reviewer recorded an Anthropic request as an OpenAI one.
        """
        model = request.body.get("model")
        if isinstance(model, str) and model != self._model_id:
            raise CassetteError(
                f"recorder is configured for model {self._model_id!r} but the request asks for "
                f"{model!r}; the recording would misdescribe the call"
            )
