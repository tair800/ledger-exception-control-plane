"""M3.4 — record, replay, and the offline claim.

The plan asks for four things and this module is organised around them: a record/replay harness,
scrubbing of authorisation headers and provider identifiers, replay determinism, and a test
asserting no cassette contains a secret-shaped string. The exit criterion is that the suite runs
offline from cassettes, and the section that demonstrates it drives every exception the canonical
corpus produces through the real adapters.

**What the committed cassettes are, stated plainly.** They are ``SYNTHESISED``, not captured. This
repository holds no credential and the harness deliberately ships no HTTP client, so nothing here
has ever spoken to a provider. What the cassettes exercise is real: the adapters' own parsing, the
fingerprint that decides a match, scrubbing, canonical serialisation and determinism. What they are
not is evidence about how any model behaves — that needs a capture, and the evaluation increments
(6.1, 6.2 and 6.3) are where it belongs. The format records the distinction so the two can never be
confused, and a test asserts it over every cassette in the tree.

**Two things in this module were rewritten because they could not fail.** The network guard watched
a choke point that an async connection does not pass through, and its control test used a blocking
connection, so it was green while being blind to the only shape that matters here. The exit
criterion called the transport's lookup directly and asserted things that were true by
construction, so a transport sabotaged to answer every request identically — or to fabricate an
answer it never read — passed the whole suite. Both are now driven through the paths a live call
would take, and both have controls that fail when the guard is removed. The corrections are
recorded in the docstrings rather than tidied away, because "this test passed" was the wrong
evidence twice.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import socket
from collections.abc import Mapping
from typing import Any, Final

import pytest

from ledger_exception_control_plane.db.control import TreatmentCode
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
from ledger_exception_control_plane.llm.evidence import assemble_evidence
from ledger_exception_control_plane.llm.flow import propose_treatment
from ledger_exception_control_plane.llm.port import ProviderError, ProviderId, ProviderRequest
from ledger_exception_control_plane.llm.prompt import build_prompt
from ledger_exception_control_plane.llm.providers.anthropic_messages import (
    AnthropicMessagesProposer,
)
from ledger_exception_control_plane.llm.schema import ProposalPrompt, TreatmentProposal
from ledger_exception_control_plane.matching import DEFAULT_POLICY
from tests.cassette_builder import CANONICAL_CASSETTE as BUILDER_OUTPUT_PATH
from tests.cassette_builder import (
    PROVIDERS,
    build_interactions,
    corpus_subjects,
    envelope,
    render,
    stand_in_answer,
)

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[1]
CASSETTE_DIR: Final = REPO_ROOT / "tests" / "cassettes"
CANONICAL_CASSETTE: Final = CASSETTE_DIR / "canonical-corpus.json"

#: Thirteen exceptions, on two providers.
CORPUS_EXCEPTIONS: Final = 13


# ======================================================================================
# Nothing here may reach the network — and the guard has to be able to see the attempt
# ======================================================================================


_LOOPBACK: Final = ("127.0.0.1", "::1", "localhost", "")


def _loop_classes() -> list[type]:
    """Every event-loop class that implements ``sock_connect`` on this platform.

    **Determined by probe, not by reading.** The obvious choke point is
    ``BaseEventLoop.sock_connect``, and it does not exist: the selector and proactor loops each
    define their own, and ``BaseEventLoop`` defines none. Patching the base class would therefore
    have *added* an attribute that the real implementation shadows — a guard that intercepts
    nothing, which is precisely the failure being corrected here, reintroduced one level down.
    """
    import asyncio.selector_events

    classes: list[type] = [asyncio.selector_events.BaseSelectorEventLoop]
    try:  # pragma: no cover - one branch per platform
        import asyncio.proactor_events

        classes.append(asyncio.proactor_events.BaseProactorEventLoop)
    except ImportError:  # pragma: no cover - non-Windows
        pass
    return [cls for cls in classes if "sock_connect" in cls.__dict__]


def _is_loopback(host: object) -> bool:
    return not isinstance(host, str) or host in _LOOPBACK


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this module may connect to anything off this machine.

    Enforced rather than documented. The whole value of a cassette harness is that CI needs no key
    and makes no call, and "we did not mean to" is not a mechanism.

    **The first version was blind to every async connection**, which is the only kind the port can
    make. It patched ``socket.socket.connect`` alone; on Windows the proactor loop reaches the
    network through ``ConnectEx`` and never touches it. Two reviewers said so, one said otherwise,
    and a direct probe settled it: an async connect to a literal address produced **zero**
    ``socket.connect`` calls, zero name lookups, and one ``sock_connect`` on the loop
    implementation. The guard was absent from exactly the path a transport uses.

    So it watches five: the blocking connect, the two convenience wrappers, name resolution — which
    has already left the machine whether or not a connection follows — and ``sock_connect`` on each
    loop class that defines it. Loopback stays open, because blocking it stops the test runner
    itself: the event loop opens a self-pipe, and a guard that kills the runner proves nothing.
    """
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def refuse(host: object, how: str) -> None:
        raise AssertionError(f"this module must not {how} {host!r}")

    def guarded_connect(self: socket.socket, address: Any, *args: object, **kwargs: object) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            refuse(host, "connect to")
        return real_connect(self, address, *args, **kwargs)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> Any:
        if not _is_loopback(host):
            refuse(host, "resolve")
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_create_connection(address: Any, *args: object, **kwargs: object) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            refuse(host, "connect to")
        return real_create_connection(address, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)

    for cls in _loop_classes():
        real_sock_connect = cls.__dict__["sock_connect"]

        async def guarded_sock_connect(
            self: Any, sock: Any, address: Any, _real: Any = real_sock_connect
        ) -> Any:
            host = address[0] if isinstance(address, tuple) else address
            if not _is_loopback(host):
                refuse(host, "connect to")
            return await _real(self, sock, address)

        monkeypatch.setattr(cls, "sock_connect", guarded_sock_connect)


def test_the_network_guard_sees_a_blocking_connection() -> None:
    """One of three controls. A fixture that blocked nothing would be worse than none."""
    with socket.socket() as probe, pytest.raises(AssertionError, match="must not connect"):
        probe.connect(("api.anthropic.com", 443))


@pytest.mark.asyncio
async def test_the_network_guard_sees_an_async_connection() -> None:
    """**The control that was missing**, and the only shape the port can actually produce.

    The blocking control above passed while the guard could not see this at all.

    Stopped at resolution rather than at connect, which is why the match is on the common prefix:
    a hostname is looked up before anything is dialled, and the lookup has already left the
    machine. The literal-address case below is the one that reaches the loop-level hook.
    """
    with pytest.raises(AssertionError, match="must not"):
        await asyncio.open_connection("api.anthropic.com", 443)


@pytest.mark.asyncio
async def test_the_network_guard_sees_an_async_connection_to_a_literal_address() -> None:
    """No name lookup happens here, so only the loop-level hook can catch it.

    This is the case the probe used to settle the disagreement between reviewers: a literal address
    bypasses ``getaddrinfo`` entirely, so a guard built on name resolution alone would let it out.
    """
    with pytest.raises(AssertionError, match="must not connect"):
        await asyncio.open_connection("93.184.216.34", 443)


def test_the_network_guard_sees_a_name_lookup() -> None:
    """A resolution has already left the machine, whether or not a connection follows."""
    with pytest.raises(AssertionError, match="must not resolve"):
        socket.getaddrinfo("api.openai.com", 443)


def test_the_network_guard_lets_loopback_through() -> None:
    """Its own limit, asserted. A guard that blocked the runner's self-pipe would be reverted."""
    assert _is_loopback("127.0.0.1")
    assert not _is_loopback("api.anthropic.com")


def test_at_least_one_loop_class_is_actually_guarded() -> None:
    """The loop hook must exist on this platform, or the async controls above are vacuous."""
    assert _loop_classes(), "no event-loop class implements sock_connect; the guard has no hook"


# ======================================================================================
# Helpers
# ======================================================================================


class _UnusableTransport:
    """A transport that exists only to satisfy a constructor. Calling it is a bug."""

    async def send(self, request: ProviderRequest) -> Mapping[str, Any]:
        raise AssertionError("nothing in this module may send")


class _StubTransport:
    """Stands in for whatever an operator would supply. Records nothing, opens nothing."""

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.calls = 0

    async def send(self, request: ProviderRequest) -> Any:
        self.calls += 1
        return self._payload


def _first_subject_prompt() -> ProposalPrompt:
    subject, candidates = corpus_subjects()[0]
    return build_prompt(subject, assemble_evidence(subject, candidates, DEFAULT_POLICY))


def _some_request() -> ProviderRequest:
    return AnthropicMessagesProposer(_UnusableTransport()).build_request(_first_subject_prompt())


def _written(tmp_path: pathlib.Path, text: str, name: str = "c.json") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _answer_text(response: Mapping[str, Any]) -> str:
    """The JSON the provider wrapped, from either vendor's envelope."""
    content = response.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                return str(block["text"])
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        return str(choices[0]["message"]["content"])
    raise AssertionError("the recording carries no answer")


# ======================================================================================
# The committed cassette
# ======================================================================================


def test_the_committed_cassette_matches_what_the_builder_produces() -> None:
    """Drift-checked like the fixture corpus and the demo snapshot are, byte for byte.

    A cassette nobody can regenerate is a binary blob with a JSON extension. The builder lives in
    ``llm/cassette_corpus.py`` and is reachable as ``make cassettes`` — which was a correction: the
    first version's builder lived inside this test and its failure message named a target that did
    not exist.
    """
    assert CANONICAL_CASSETTE.exists(), (
        f"{CANONICAL_CASSETTE} is missing; regenerate it with `make cassettes`"
    )
    assert CANONICAL_CASSETTE.read_bytes() == render().encode("utf-8")


def test_the_builder_writes_where_the_suite_reads() -> None:
    """``make cassettes`` must rewrite the file these tests load, not a different one."""
    assert BUILDER_OUTPUT_PATH == CANONICAL_CASSETTE


def test_the_committed_cassette_has_no_carriage_returns() -> None:
    """It is compared byte for byte, so a checkout that rewrote line endings would fail the drift
    check for a reason that has nothing to do with its contents. ``.gitattributes`` pins it; this
    is what notices if that stops being true."""
    assert b"\r" not in CANONICAL_CASSETTE.read_bytes()
    assert CANONICAL_CASSETTE.read_bytes().endswith(b"\n")


def test_the_committed_cassette_covers_the_corpus_on_both_providers() -> None:
    """One recording per exception per provider, each with its own fingerprint and id."""
    interactions = load_cassette(CANONICAL_CASSETTE).interactions

    assert len(interactions) == CORPUS_EXCEPTIONS * len(PROVIDERS)
    assert len({i.request_fingerprint for i in interactions}) == len(interactions)
    assert len({i.cassette_id for i in interactions}) == len(interactions)
    for provider, _ in PROVIDERS:
        assert sum(1 for i in interactions if i.provider is provider) == CORPUS_EXCEPTIONS


def test_no_synthesised_cassette_carries_a_usage_or_cost_field() -> None:
    """**A synthesised recording must not look like a measurement.**

    6.3 computes cost per 1,000 lines from provider usage fields rather than estimating it. A
    synthesised interaction carrying ``{"input_tokens": 0, "output_tokens": 0}`` does not read as
    "nobody measured this" — it reads as "this call was free", and that zero would flow straight
    into a published figure. The rule is stated as absence, because absence is the only encoding
    that cannot be mistaken for a measurement.

    Captured cassettes will carry usage, and should. This is scoped to the synthesised ones.
    """
    forbidden = {"usage", "cost", "token_count", "input_tokens", "output_tokens", "total_tokens"}

    def walk(node: object, where: str) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.lower() in forbidden:
                    found.append(f"{where}.{key}")
                found.extend(walk(value, f"{where}.{key}"))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                found.extend(walk(item, f"{where}[{index}]"))
        return found

    offences: list[str] = []
    for file in _cassette_files(CASSETTE_DIR):
        document = json.loads(file.read_text(encoding="utf-8"))
        for interaction in document.get("interactions", []):
            if interaction.get("origin") != Origin.SYNTHESISED.value:
                continue
            offences.extend(walk(interaction.get("response"), file.name))

    assert offences == [], "a synthesised recording carries something a cost figure would read"


def test_kill_a_synthesised_usage_field_is_detected() -> None:
    """The rule above, killed. It is an absence check, so it passes on an empty payload."""

    def has_usage(payload: dict[str, Any]) -> bool:
        return "usage" in canonical(payload)

    assert has_usage({"usage": {"input_tokens": 0}})
    assert not has_usage(dict(envelope(ProviderId.ANTHROPIC, {"treatment": "rebook"})))


def test_the_committed_cassette_exercises_every_treatment_and_an_abstention() -> None:
    """**The coverage the first version silently lacked.**

    Its answers were derived by hashing the exception id, which looked more principled and left the
    abstention branch unreached — so the committed file exercised neither an abstaining proposal
    nor every member of the closed vocabulary, and a reviewer's mutants in that region survived the
    whole suite. Assigning by sorted position covers both by construction; this asserts it rather
    than trusting the construction.
    """
    answers = [
        json.loads(_answer_text(i.response)) for i in load_cassette(CANONICAL_CASSETTE).interactions
    ]

    assert {a["treatment"] for a in answers} == {member.value for member in TreatmentCode}
    abstaining = [a for a in answers if a["abstained"]]
    assert abstaining, "no recording exercises an abstention"
    assert all(a["treatment"] == TreatmentCode.ESCALATE.value for a in abstaining)


# ======================================================================================
# §17 — what may never reach a committed file
# ======================================================================================


def _cassette_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every cassette under ``root``, at any depth.

    ``rglob``, not ``glob``, and that was a correction with teeth: the first version scanned only
    the top directory, so a reviewer planted a leaking cassette one level down in
    ``tests/cassettes/captured/`` and every §17 guard passed.
    """
    return sorted(root.rglob("*.json"))


def _files_with_a_secret_shaped_value(root: pathlib.Path) -> list[str]:
    """Cassettes whose text still contains something shaped like a credential.

    Expressed through :func:`redact_text` — if redacting the file changes nothing, it holds no
    secret-shaped value. That shares a definition with the scrubber, which is a real limitation and
    is stated rather than hidden: a credential family neither knows about would pass both. The
    independent half of §17 is :func:`_unredacted_scrubbed_keys` below, which checks the *keys*
    §17 names regardless of what their values look like.
    """
    found = []
    for path in _cassette_files(root):
        text = path.read_text(encoding="utf-8")
        if redact_text(text) != text:
            found.append(path.name)
    return found


def _unredacted_scrubbed_keys(root: pathlib.Path) -> list[str]:
    """Every place a §17 key survives with a real value, anywhere in any document.

    Independent of what a value looks like: the rule is that these keys carry ``[scrubbed]`` in a
    committed file, full stop. Recursive, because a header block nested inside a response is
    exactly where one would hide.
    """
    keys = (
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "api_key",
        "apikey",
        "cookie",
        "set-cookie",
        "openai-organization",
        "openai-project",
        "id",
        "request_id",
        "x-request-id",
        "system_fingerprint",
    )
    offences: list[str] = []

    def walk(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{where}.{key}"
                if isinstance(key, str) and key.lower() in keys:
                    if value != REDACTED:
                        offences.append(path)
                else:
                    walk(value, path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")

    for file in _cassette_files(root):
        document = json.loads(file.read_text(encoding="utf-8"))
        for interaction in document.get("interactions", []):
            walk(interaction.get("response"), file.name)
    return offences


def _foreign_origins(root: pathlib.Path) -> list[str]:
    """Interactions claiming to be a recording of a real provider."""
    offences = []
    for file in _cassette_files(root):
        document = json.loads(file.read_text(encoding="utf-8"))
        for interaction in document.get("interactions", []):
            if interaction.get("origin") != Origin.SYNTHESISED.value:
                offences.append(f"{file.name}:{interaction.get('cassette_id', '?')[:12]}")
    return offences


def test_the_cassette_tree_is_not_empty() -> None:
    """**Every scan below is vacuous over an empty tree**, and a reviewer proved it: deleting the
    cassettes directory made all three §17 guards pass. A scan that reports nothing because it
    found nothing to scan is not a guard."""
    assert CASSETTE_DIR.is_dir()
    assert _cassette_files(CASSETTE_DIR), "no cassette to scan; the §17 guards would be vacuous"


def test_no_cassette_contains_a_secret_shaped_string() -> None:
    """The plan asks for exactly this test, and §17 for the rule behind it.

    Matched on secret-shaped **values**, which is a correction. The first version banned the
    substrings ``sk-``, ``bearer ``, ``api_key``, ``authorization`` and ``x-api-key`` anywhere in
    the file — which forbids what *correct* scrubbing leaves behind, since a scrubbed capture still
    carries ``"authorization": "[scrubbed]"``, and fires on the word "risk-based" in a merchant
    description. It passed only because no cassette had yet been captured, so it would have failed
    the first time it was needed.
    """
    assert _files_with_a_secret_shaped_value(CASSETTE_DIR) == []


def test_no_cassette_carries_an_authorisation_header_or_provider_identifier() -> None:
    """§17's other half, at any depth. Both vendors return an identifier; neither adapter reads
    one."""
    assert _unredacted_scrubbed_keys(CASSETTE_DIR) == []


def test_no_cassette_claims_to_be_a_recording_of_a_real_provider() -> None:
    """No credential is present, the harness ships no HTTP client, and no call has ever been made.

    Extended to every file in the tree, which is the same ``rglob`` correction: the origin check
    used to run over the canonical cassette alone, so a planted file elsewhere could claim anything.
    """
    assert _foreign_origins(CASSETTE_DIR) == []


@pytest.mark.parametrize(
    ("label", "planted"),
    [
        (
            "a bearer token in a header",
            {"headers": {"Authorization": "Bearer sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"}},
        ),
        ("a key quoted in prose", {"note": "the caller sent sk-proj-0123456789abcdefghij"}),
        ("a token in a nested list", {"content": [{"text": "AKIAABCDEFGHIJKLMNOP"}]}),
    ],
)
def test_kill_a_leaking_cassette_is_detected(
    tmp_path: pathlib.Path, label: str, planted: dict[str, Any]
) -> None:
    """**The secret scan must fail on a file that leaks.** Otherwise it is decoration.

    Planted one directory down, because that is where a reviewer's leaking cassette went unseen.
    """
    nested = tmp_path / "captured"
    nested.mkdir()
    _written(
        nested,
        canonical(
            {
                "cassette_version": CASSETTE_VERSION,
                "interactions": [_interaction_document(response=planted)],
            }
        ),
        "leaky.json",
    )

    assert _files_with_a_secret_shaped_value(tmp_path) == ["leaky.json"], label


def test_kill_a_surviving_authorisation_header_is_detected(tmp_path: pathlib.Path) -> None:
    """The key-based half must fail independently of what the value looks like.

    ``"hunter2"`` is not secret-*shaped*, so the regex scan is silent about it. §17 does not care:
    an authorisation header in a committed file is the offence.
    """
    nested = tmp_path / "deeper" / "still"
    nested.mkdir(parents=True)
    _written(
        nested,
        canonical(
            {
                "cassette_version": CASSETTE_VERSION,
                "interactions": [
                    _interaction_document(response={"headers": {"Authorization": "hunter2"}})
                ],
            }
        ),
    )

    assert _files_with_a_secret_shaped_value(tmp_path) == []
    assert _unredacted_scrubbed_keys(tmp_path) == ["c.json.headers.Authorization"]


def test_kill_a_cassette_claiming_to_be_captured_is_detected(tmp_path: pathlib.Path) -> None:
    """A synthesised answer that calls itself a recording misrepresents what the suite tests."""
    _written(
        tmp_path,
        canonical(
            {
                "cassette_version": CASSETTE_VERSION,
                "interactions": [_interaction_document(origin=Origin.CAPTURED.value)],
            }
        ),
    )
    assert _foreign_origins(tmp_path) == ["c.json:aaaaaaaaaaaa"]


def test_kill_an_empty_cassette_tree_is_detected(tmp_path: pathlib.Path) -> None:
    """The emptiness check itself, killed. All three scans are silent over nothing."""
    assert _cassette_files(tmp_path) == []
    assert _files_with_a_secret_shaped_value(tmp_path) == []
    assert _unredacted_scrubbed_keys(tmp_path) == []
    assert _foreign_origins(tmp_path) == []


def _interaction_document(
    *, response: dict[str, Any] | None = None, origin: str = Origin.SYNTHESISED.value
) -> dict[str, Any]:
    """A well-formed interaction, for planting things into."""
    return {
        "cassette_id": "a" * 64,
        "provider": ProviderId.ANTHROPIC.value,
        "model_id": "m",
        "model_version": "v",
        "path": "/v1/messages",
        "request_fingerprint": "f" * 64,
        "origin": origin,
        "response": response if response is not None else {},
    }


# ======================================================================================
# The offline claim — the exit criterion
# ======================================================================================


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_every_corpus_exception_replays_offline_into_its_recorded_proposal(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """**The exit criterion.** The suite runs from cassettes, with no key and no call.

    Driven through ``await proposer.propose(prompt)``, so it crosses ``ReplayTransport.send``, the
    shared error translation and the adapter's own parsing — the whole path a live call would take
    apart from the socket.

    **The first version could not fail.** It called the transport's lookup directly and asserted
    that the result was a ``TreatmentProposal`` whose ``treatment`` was a member of the enum: both
    true by construction, since ``parse`` returns that type or raises and the field is a strict
    enum. A reviewer's mutants passed the entire suite with the fingerprint ignored so every
    exception got one answer, with the transport fabricating a proposal it never read, and with the
    lookup wired to a constant.

    What makes it falsifiable is the served ledger plus the equality below: each call must be
    answered by *its own* recording, and the proposal must carry what that recording encodes.
    """
    cassette = load_cassette(CANONICAL_CASSETTE)
    replay = ReplayTransport(cassette)
    proposer = adapter(replay)

    expected: set[str] = set()
    for subject, candidates in corpus_subjects():
        pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
        prompt = build_prompt(subject, pack)

        proposal = await proposer.propose(prompt)
        assert isinstance(proposal, TreatmentProposal)

        interaction = cassette.find(request_fingerprint(proposer.build_request(prompt)))
        assert interaction is not None, "the cassette does not cover the corpus"
        assert interaction.provider is provider
        expected.add(interaction.cassette_id)

        # The answer is the one *this* recording encodes, not merely a valid proposal.
        encoded = json.loads(_answer_text(interaction.response))
        assert proposal.treatment.value == encoded["treatment"]
        assert proposal.confidence.value == encoded["confidence"]
        assert proposal.abstained == encoded["abstained"]
        assert [ref.evidence_id for ref in proposal.evidence_refs] == [
            ref["evidence_id"] for ref in encoded["evidence_refs"]
        ]

    assert len(expected) == CORPUS_EXCEPTIONS
    assert set(replay.served) == expected, "an answer came from somewhere other than its recording"
    assert len(replay.served) == CORPUS_EXCEPTIONS, "one call, one recording, no repeats"


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_the_two_providers_are_answered_from_different_recordings(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """One cassette holds both, and the fingerprint keeps them apart.

    The bodies differ — a system message against a top-level field, a different output-ceiling key,
    a different path — so a harness that matched loosely would hand one vendor's envelope to the
    other's parser. That would not even fail loudly: both encode the same answer.
    """
    replay = ReplayTransport(load_cassette(CANONICAL_CASSETTE))
    prompt = _first_subject_prompt()

    await adapter(replay).propose(prompt)
    (served,) = replay.served

    other = next(a for p, a in PROVIDERS if p is not provider)
    other_replay = ReplayTransport(load_cassette(CANONICAL_CASSETTE))
    await other(other_replay).propose(prompt)

    assert other_replay.served != [served]


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_replay_is_deterministic(provider: ProviderId, adapter: type[Any]) -> None:
    """The same request, answered identically, every time — over ``send``, repeatedly.

    The first version compared two lookups on two freshly loaded transports, so a transport that
    answered correctly once and wrongly afterwards passed. Three sends on one transport is what
    catches that; two transports is what catches state surviving a reload.
    """
    prompt = _first_subject_prompt()

    replay = ReplayTransport(load_cassette(CANONICAL_CASSETTE))
    proposer = adapter(replay)
    answers = [(await proposer.propose(prompt)).model_dump_json() for _ in range(3)]

    reloaded = ReplayTransport(load_cassette(CANONICAL_CASSETTE))
    answers.append((await adapter(reloaded).propose(prompt)).model_dump_json())

    assert len(set(answers)) == 1
    assert len(set(replay.served)) == 1, "three sends, one recording"
    assert set(reloaded.served) == set(replay.served)


@pytest.mark.asyncio
async def test_a_replayed_payload_cannot_be_mutated_by_its_caller() -> None:
    """Two replays of one recording must not share a mutable payload.

    ``Interaction`` is frozen, which stops assignment and does nothing about the mapping the field
    holds — so a caller editing what it was handed changed what the next replay saw. The request
    side already took a defensive copy for exactly this reason.
    """
    replay = ReplayTransport(load_cassette(CANONICAL_CASSETTE))
    request = _some_request()

    first = dict(await replay.send(request))
    nested = first["content"]
    assert isinstance(nested, list)
    nested[0]["text"] = "rewritten"

    second = await replay.send(request)
    replayed = second["content"]
    assert isinstance(replayed, list)
    assert replayed[0]["text"] != "rewritten"


def test_a_cassette_round_trips_byte_for_byte() -> None:
    """Rendering what was loaded reproduces the file exactly."""
    text = CANONICAL_CASSETTE.read_text(encoding="utf-8")
    assert render_cassette(load_cassette(CANONICAL_CASSETTE).interactions) == text


def test_rendering_is_ordered_by_fingerprint_not_by_insertion() -> None:
    """A total order, so two runs that visit the corpus differently produce one file.

    Ordering by cassette id alone was the first version, which is not a total order: two entries
    can share an id, and then the file depends on the order the recorder happened to see them.
    """
    interactions = build_interactions()
    assert render_cassette(interactions) == render_cassette(list(reversed(interactions)))


# ======================================================================================
# Scrubbing, and the invariant that actually holds
# ======================================================================================


HOSTILE_PAYLOAD: dict[str, Any] = {
    "id": "msg_01ABCDEF",
    "system_fingerprint": "fp_0123456789",
    "request_id": "req_abc",
    "headers": {
        "Authorization": "Bearer sk-ant-api03-REDACTMEREDACTMEREDACTME",
        "X-API-Key": "sk-proj-0123456789abcdefghij",
        "content-type": "application/json",
    },
    "content": [{"type": "text", "text": "the answer"}],
    "note": "a leaked key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA in prose",
    "evidence_id": "kept: this is not a provider identifier",
}


def test_scrub_removes_authorisation_headers_and_provider_identifiers() -> None:
    """§17, exactly: both categories, wherever they appear in the tree."""
    scrubbed = scrub(HOSTILE_PAYLOAD)

    assert scrubbed["id"] == REDACTED
    assert scrubbed["system_fingerprint"] == REDACTED
    assert scrubbed["request_id"] == REDACTED
    assert scrubbed["headers"]["Authorization"] == REDACTED
    assert scrubbed["headers"]["X-API-Key"] == REDACTED

    # Whole keys, not substrings: `evidence_id` contains `id` and is a domain identifier.
    assert scrubbed["evidence_id"] == HOSTILE_PAYLOAD["evidence_id"]
    assert scrubbed["headers"]["content-type"] == "application/json"


def test_scrub_removes_a_secret_shaped_value_under_any_key() -> None:
    """Belt and braces over the key list. A token in an unexpected place is still a token."""
    scrubbed = scrub(HOSTILE_PAYLOAD)
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(scrubbed)
    assert REDACTED in scrubbed["note"]


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("an uppercase bearer token", "BEARER ABCDEFGHIJKLMNOPQRSTUVWX"),
        ("a Google key", "AIzaSyA0123456789abcdefghijklmnopqrs"),
        ("an AWS access key id", "AKIAIOSFODNN7EXAMPLE"),
        ("a GitHub token", "ghp_0123456789abcdefghijklmnopqrstuvwx"),
        ("a JWT", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4f"),
    ],
)
def test_scrub_covers_more_than_the_two_vendors_in_scope(label: str, value: str) -> None:
    """The cost of a false positive is a redacted word in prose. The cost of a miss is a commit.

    A reviewer got the uppercase form past the first version, which was case-sensitive, along with
    several other providers' key shapes — and cassettes will eventually carry text from whatever a
    developer had in their environment, not only from the two vendors this project calls.
    """
    assert redact_text(f"prefix {value} suffix") == f"prefix {REDACTED} suffix", label


def test_scrub_leaves_ordinary_prose_alone() -> None:
    """The other direction. Evidence packs carry merchant text, and over-redaction is a real cost.

    ``sk-`` as a bare substring matched "risk-based" in the first version's file scan. The value
    patterns require enough following characters to be a credential rather than a word.
    """
    prose = "A risk-based review of the sk-item and the bearer of record. Task-force notes."
    assert redact_text(prose) == prose


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
def test_scrubbing_preserves_every_field_a_decision_is_made_from(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """**The invariant that makes scrubbing safe** — and it is narrower than first claimed.

    Scrubbing edits a payload a replay later hands to a real adapter. If it removed something the
    adapter reads, every replayed test would exercise a shape no provider produces. So the
    structured contract must survive it exactly.
    """
    subject, candidates = corpus_subjects()[0]
    pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
    answer = stand_in_answer(0, [str(item.evidence_id) for item in pack])

    raw = envelope(provider, answer)
    proposer = adapter(_UnusableTransport())
    assert proposer.parse(raw) == proposer.parse(scrub(raw))


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
def test_scrubbing_rewrites_a_secret_a_model_quoted_in_its_rationale(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """**Scrubbing is not answer-preserving in general, and the claim that it was is withdrawn.**

    Not reworded — withdrawn. The first version asserted that a scrubbed payload parses
    *identically* to an unscrubbed one, and that is false the moment a model quotes a
    credential back: evidence packs carry third-party text, so it can. A reviewer produced
    the counter-example.

    What survives is the trade that matters, asserted here in both directions: the decision fields
    are untouched, and the secret does not reach the file even though the free text loses it. A
    rationale is provenance for humans and no code path parses it, which is what makes the trade
    correct rather than merely necessary.
    """
    subject, candidates = corpus_subjects()[0]
    pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
    answer = stand_in_answer(0, [str(item.evidence_id) for item in pack])
    answer["rationale"] = "The memo quotes Bearer sk-ant-api03-QUOTEDBYTHEMODEL01 verbatim."

    raw = envelope(provider, answer)
    proposer = adapter(_UnusableTransport())
    before = proposer.parse(raw)
    after = proposer.parse(scrub(raw))

    assert after != before
    assert after.rationale != before.rationale
    assert REDACTED in after.rationale
    assert "sk-ant-api03-QUOTEDBYTHEMODEL01" not in canonical(scrub(raw))

    # Everything a decision is made from is identical.
    assert (after.treatment, after.confidence, after.abstained) == (
        before.treatment,
        before.confidence,
        before.abstained,
    )
    assert after.evidence_refs == before.evidence_refs


# ======================================================================================
# A miss is loud, and never a provider outage
# ======================================================================================


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_a_missing_recording_raises_and_does_not_fall_back(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """There is no live fallback — not a discouraged one, not a configurable one.

    Parametrised over both adapters, which was a correction: every failure-mode test here used the
    Anthropic path, so a reviewer's mutant that swallowed a miss inside the *OpenAI* adapter passed
    the whole suite. One provider's error translation is not evidence about the other's.
    """
    empty = adapter(ReplayTransport(Cassette(interactions=())))
    with pytest.raises(CassetteMissError, match="stale"):
        await empty.propose(_first_subject_prompt())


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_a_stale_cassette_is_a_miss_rather_than_a_wrong_answer(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """**Staleness detection is the absence of a match**, which is why it cannot be forgotten.

    The recording is keyed on a fingerprint of the whole request body, so changing anything the
    provider would see stops it matching. A harness keyed on the prompt hash alone would have
    replayed happily through a changed response schema.
    """
    proposer = adapter(ReplayTransport(load_cassette(CANONICAL_CASSETTE)))
    prompt = _first_subject_prompt()

    assert await proposer.propose(prompt) is not None

    with pytest.raises(CassetteMissError):
        await proposer.propose(prompt.model_copy(update={"user": "a different question"}))


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_a_miss_is_not_reported_as_provider_unavailability(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """**The most consequential wiring in the increment.**

    The adapters translate everything a transport raises into a provider error, so callers see one
    of two failure modes. A cassette miss must not be one of them: reported as unavailability, an
    offline suite would keep passing while testing nothing, and the failure would read as weather
    rather than as a bug.
    """
    proposer = adapter(ReplayTransport(Cassette(interactions=())))

    with pytest.raises(CassetteMissError):
        await proposer.propose(_first_subject_prompt())

    assert not issubclass(CassetteMissError, ProviderError)
    assert not issubclass(CassetteError, ProviderError)


@pytest.mark.parametrize(("provider", "adapter"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.asyncio
async def test_the_flow_does_not_swallow_a_miss_into_an_outcome(
    provider: ProviderId, adapter: type[Any]
) -> None:
    """The proposal flow turns provider failures into outcomes. A harness fault must escape it.

    Otherwise ``propose_treatment`` would return ``UNAVAILABLE`` for a broken cassette and the
    caller would record "the provider was down" about an event that never involved a provider.
    """
    subject, candidates = corpus_subjects()[0]
    pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
    proposer = adapter(ReplayTransport(Cassette(interactions=())))

    with pytest.raises(CassetteMissError):
        await propose_treatment(proposer, subject, pack)


@pytest.mark.asyncio
async def test_an_ordinary_transport_failure_is_still_a_provider_error() -> None:
    """The other side of the same wiring. Only cassette faults are exempt from translation."""

    class _Broken:
        async def send(self, request: ProviderRequest) -> Mapping[str, Any]:
            raise TimeoutError("connection timed out")

    with pytest.raises(ProviderError):
        await AnthropicMessagesProposer(_Broken()).propose(_first_subject_prompt())


@pytest.mark.asyncio
async def test_a_credential_in_a_transport_exception_never_reaches_the_message() -> None:
    """A client library's exception routinely carries the request URL and its headers.

    A reviewer traced one all the way into ``ProposalOutcome.detail``, which a later increment puts
    in an audit row — a secret laundered out through the error path rather than the cassette one.
    The original exception is still chained for a debugger; only the message is cleaned.
    """

    class _Leaky:
        async def send(self, request: ProviderRequest) -> Mapping[str, Any]:
            raise RuntimeError("POST /v1/messages x-api-key=sk-ant-api03-LEAKEDINANERROR01 failed")

    with pytest.raises(ProviderError) as caught:
        await AnthropicMessagesProposer(_Leaky()).propose(_first_subject_prompt())

    assert "sk-ant-api03-LEAKEDINANERROR01" not in str(caught.value)
    assert REDACTED in str(caught.value)


# ======================================================================================
# A recording must describe the call it actually made
# ======================================================================================


@pytest.mark.asyncio
async def test_replay_refuses_a_recording_whose_declared_model_disagrees() -> None:
    """A matching fingerprint proves the body matched. It says nothing about the metadata.

    Which is what a stored ``cassette_id`` would carry into a provenance record: a row naming a
    model that was never called. A reviewer served one vendor's declared model to the other
    vendor's adapter and nothing objected.
    """
    request = _some_request()
    fingerprint = request_fingerprint(request)
    lying = Interaction(
        cassette_id=cassette_id_for(ProviderId.ANTHROPIC, "a-different-model", fingerprint),
        provider=ProviderId.ANTHROPIC,
        model_id="a-different-model",
        model_version="v",
        path=request.path,
        request_fingerprint=fingerprint,
        origin=Origin.SYNTHESISED,
        response={},
    )

    with pytest.raises(CassetteMalformedError, match="declares model"):
        await ReplayTransport(Cassette(interactions=(lying,))).send(request)


@pytest.mark.asyncio
async def test_replay_refuses_a_recording_whose_declared_path_disagrees() -> None:
    """The same check on the other half of the request identity."""
    request = _some_request()
    fingerprint = request_fingerprint(request)
    model = request.body["model"]
    assert isinstance(model, str)
    lying = Interaction(
        cassette_id="c" * 64,
        provider=ProviderId.ANTHROPIC,
        model_id=model,
        model_version="v",
        path="/v1/somewhere-else",
        request_fingerprint=fingerprint,
        origin=Origin.SYNTHESISED,
        response={},
    )

    with pytest.raises(CassetteMalformedError, match="declares path"):
        await ReplayTransport(Cassette(interactions=(lying,))).send(request)


# ======================================================================================
# Recording, and the opt-in that gates it
# ======================================================================================


def _recorder(inner: Any, monkeypatch: pytest.MonkeyPatch) -> RecordingTransport:
    monkeypatch.setenv(CAPTURE_OPT_IN, "1")
    return RecordingTransport(
        inner,
        provider=ProviderId.ANTHROPIC,
        model_id=AnthropicMessagesProposer(_UnusableTransport()).model_id,
        model_version=AnthropicMessagesProposer(_UnusableTransport()).model_version,
    )


def test_the_opt_in_is_outside_the_application_settings_namespace() -> None:
    """**Renamed, because ``.env.example`` is a file people copy.**

    §17 asks for the capture switch to be documented there. Every ``LECP_`` name belongs to the
    application's settings model, which forbids extras — so a reviewer showed that documenting
    ``LECP_CASSETTE_CAPTURE`` as §17 asks would break startup for anyone who copied the example
    into a real ``.env``. A capture switch is not application configuration.
    """
    assert CAPTURE_OPT_IN == "CASSETTE_CAPTURE"
    assert not CAPTURE_OPT_IN.startswith("LECP_")


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE", "on", "2"])
def test_recording_refuses_without_the_opt_in(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """**Fail closed.** Capture is the only operation here that could reach a paid API.

    A construction-time refusal rather than a branch inside ``send``, so there is no path that
    records first and checks afterwards.
    """
    monkeypatch.setenv(CAPTURE_OPT_IN, value)
    assert not capture_is_enabled()
    with pytest.raises(CassetteError, match=CAPTURE_OPT_IN):
        RecordingTransport(
            _StubTransport({}), provider=ProviderId.ANTHROPIC, model_id="m", model_version="v"
        )


def test_recording_refuses_when_the_opt_in_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset is the ordinary state, on every machine and in CI."""
    monkeypatch.delenv(CAPTURE_OPT_IN, raising=False)
    assert not capture_is_enabled()
    with pytest.raises(CassetteError, match=CAPTURE_OPT_IN):
        RecordingTransport(
            _StubTransport({}), provider=ProviderId.ANTHROPIC, model_id="m", model_version="v"
        )


@pytest.mark.asyncio
async def test_recording_scrubs_before_it_keeps_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbing happens on the way in, so a secret cannot reach a file even briefly."""
    inner = _StubTransport(HOSTILE_PAYLOAD)
    recorder = _recorder(inner, monkeypatch)

    returned = await recorder.send(_some_request())

    assert inner.calls == 1
    (interaction,) = recorder.recorded
    assert interaction.origin is Origin.CAPTURED
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA" not in canonical(dict(interaction.response))
    assert interaction.response["id"] == REDACTED

    # What the caller saw during capture is what a replay will see. A recording that returned the
    # unscrubbed payload could pass while its own cassette failed.
    assert returned == interaction.response


@pytest.mark.asyncio
async def test_a_recorded_payload_cannot_be_mutated_by_its_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller gets a copy, so editing it cannot change what gets written to the file."""
    recorder = _recorder(_StubTransport(dict(HOSTILE_PAYLOAD)), monkeypatch)
    returned = await recorder.send(_some_request())

    returned["note"] = "rewritten"  # type: ignore[index]
    assert recorder.recorded[0].response["note"] != "rewritten"


@pytest.mark.asyncio
async def test_recording_refuses_a_response_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A provider that answers with a list has answered.** It has not become unreachable.

    ``dict(response)`` was the first version, and it ran before the port's own non-object check
    could — so a JSON array became either a fabricated one-key recording (``dict(["ab"])`` is a
    silent success) or a ``ProviderUnavailableError`` about a provider that had just replied. Both
    misdescribe what happened; the second would send an operator to look at the network.
    """
    recorder = _recorder(_StubTransport([{"not": "an object"}]), monkeypatch)

    with pytest.raises(CassetteError, match="not an object"):
        await recorder.send(_some_request())
    assert recorder.recorded == []


@pytest.mark.asyncio
async def test_recording_refuses_a_request_for_a_model_it_is_not_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder told it is capturing one model must not file another model's call under it.

    Self-contradicting provenance is the defect the ``model_version`` properties were rewritten to
    prevent at M3.2; this is the same thing one layer down.
    """
    monkeypatch.setenv(CAPTURE_OPT_IN, "1")
    recorder = RecordingTransport(
        _StubTransport({}),
        provider=ProviderId.ANTHROPIC,
        model_id="some-other-model",
        model_version="v",
    )

    with pytest.raises(CassetteError, match="misdescribe"):
        await recorder.send(_some_request())
    assert recorder.recorded == []


@pytest.mark.asyncio
async def test_what_was_recorded_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The round trip the harness exists for: record, render, load, replay, identical."""
    subject, candidates = corpus_subjects()[0]
    pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
    prompt = build_prompt(subject, pack)
    answer = stand_in_answer(0, [str(item.evidence_id) for item in pack])

    recorder = _recorder(_StubTransport(envelope(ProviderId.ANTHROPIC, answer)), monkeypatch)
    captured = await AnthropicMessagesProposer(recorder).propose(prompt)

    path = _written(tmp_path, render_cassette(recorder.recorded))
    replay = ReplayTransport(load_cassette(path))
    replayed = await AnthropicMessagesProposer(replay).propose(prompt)

    assert replayed == captured
    assert replay.served == [recorder.recorded[0].cassette_id]


@pytest.mark.asyncio
async def test_a_recording_declares_itself_captured_and_only_a_recorder_may(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the class holding a transport that could have dialled may claim a capture."""
    recorder = _recorder(_StubTransport(envelope(ProviderId.ANTHROPIC, {"x": 1})), monkeypatch)
    await recorder.send(_some_request())

    assert recorder.recorded[0].origin is Origin.CAPTURED
    assert all(i.origin is Origin.SYNTHESISED for i in build_interactions())


# ======================================================================================
# Malformed cassettes
# ======================================================================================


_VALID_ENTRY: Final = (
    '{"cassette_id":"a","provider":"anthropic","model_id":"m","model_version":"v",'
    '"path":"/v1/messages","request_fingerprint":"f","origin":"synthesised","response":{}}'
)


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("not json", "{not json"),
        ("not an object", "[]"),
        ("a bare string", '"a cassette"'),
        ("no version", '{"interactions":[]}'),
        ("a future version", '{"cassette_version":"99","interactions":[]}'),
        ("a numeric version", '{"cassette_version":1,"interactions":[]}'),
        ("no interactions", '{"cassette_version":"1"}'),
        ("interactions that are not a list", '{"cassette_version":"1","interactions":{}}'),
        ("an interaction that is not an object", '{"cassette_version":"1","interactions":[1]}'),
        ("a missing field", '{"cassette_version":"1","interactions":[{"cassette_id":"a"}]}'),
        (
            "an unknown provider",
            '{"cassette_version":"1","interactions":[{"cassette_id":"a","provider":"acme",'
            '"model_id":"m","model_version":"v","path":"/p","request_fingerprint":"f",'
            '"origin":"synthesised","response":{}}]}',
        ),
        (
            "an unknown origin",
            '{"cassette_version":"1","interactions":[{"cassette_id":"a","provider":"anthropic",'
            '"model_id":"m","model_version":"v","path":"/p","request_fingerprint":"f",'
            '"origin":"imagined","response":{}}]}',
        ),
        (
            "a response that is not an object",
            '{"cassette_version":"1","interactions":[{"cassette_id":"a","provider":"anthropic",'
            '"model_id":"m","model_version":"v","path":"/p","request_fingerprint":"f",'
            '"origin":"synthesised","response":"text"}]}',
        ),
        (
            "a null fingerprint",
            '{"cassette_version":"1","interactions":[{"cassette_id":"a","provider":"anthropic",'
            '"model_id":"m","model_version":"v","path":"/p","request_fingerprint":null,'
            '"origin":"synthesised","response":{}}]}',
        ),
        (
            "a numeric cassette id",
            '{"cassette_version":"1","interactions":[{"cassette_id":7,"provider":"anthropic",'
            '"model_id":"m","model_version":"v","path":"/p","request_fingerprint":"f",'
            '"origin":"synthesised","response":{}}]}',
        ),
        (
            "two answers for one request",
            f'{{"cassette_version":"1","interactions":[{_VALID_ENTRY},{_VALID_ENTRY}]}}',
        ),
    ],
)
def test_a_malformed_cassette_is_refused_rather_than_guessed_at(
    tmp_path: pathlib.Path, label: str, text: str
) -> None:
    """Every shape failure names itself. A harness that reads an unknown format is not a harness.

    The type checks are not pedantry. ``str(entry[field])`` was the first version, so a JSON
    ``null`` in a fingerprint became the literal string ``"None"`` — and several of those collide
    on one lookup key, turning a corrupt file into a plausible one that answers the wrong request.
    """
    with pytest.raises(CassetteMalformedError):
        load_cassette(_written(tmp_path, text))


def test_a_truncated_cassette_is_refused(tmp_path: pathlib.Path) -> None:
    """The commonest corruption there is."""
    text = CANONICAL_CASSETTE.read_text(encoding="utf-8")
    with pytest.raises(CassetteMalformedError):
        load_cassette(_written(tmp_path, text[: len(text) // 2]))


def test_a_cassette_that_is_not_utf8_is_refused(tmp_path: pathlib.Path) -> None:
    """A bit flip in the encoding escaped the first version as ``UnicodeDecodeError``."""
    path = tmp_path / "bytes.json"
    path.write_bytes(b'{"cassette_version":"1","interactions":[]}\xff\xfe')
    with pytest.raises(CassetteMalformedError):
        load_cassette(path)


def test_a_deeply_nested_cassette_is_refused(tmp_path: pathlib.Path) -> None:
    """And a ``RecursionError``, which is what a nesting bomb produces rather than a parse error."""
    depth = 100_000
    text = '{"cassette_version":"1","interactions":' + "[" * depth + "]" * depth + "}"
    with pytest.raises(CassetteMalformedError):
        load_cassette(_written(tmp_path, text))


def test_a_missing_cassette_is_refused(tmp_path: pathlib.Path) -> None:
    """An absent file is a malformed cassette, not a crash out of the harness."""
    with pytest.raises(CassetteMalformedError):
        load_cassette(tmp_path / "nothing.json")


# ======================================================================================
# Identity: what a fingerprint and a cassette id promise
# ======================================================================================


def test_the_version_is_pinned() -> None:
    """A cassette from another version is refused, and this is what would notice a bump."""
    assert CASSETTE_VERSION == "1"
    assert json.loads(CANONICAL_CASSETTE.read_text(encoding="utf-8"))["cassette_version"] == "1"


def test_the_identity_version_is_separate_from_the_file_format_version() -> None:
    """**Adding a field to a file is not a change to what was asked of a provider.**

    They were one constant. A reviewer pointed out that bumping it to add a field would silently
    re-key every fingerprint and every derived id, orphaning any stored provenance pointing at
    them. Two constants, because they answer two different questions.
    """
    import ledger_exception_control_plane.llm.cassette as harness

    source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
    assert 'IDENTITY_VERSION: Final = "1"' in source
    assert "IDENTITY_VERSION" in source.split("def request_fingerprint")[1].split("def ")[0]
    assert "IDENTITY_VERSION" in source.split("def cassette_id_for")[1].split("def ")[0]
    assert IDENTITY_VERSION == "1"


def test_a_cassette_id_is_derived_and_stable() -> None:
    """Re-recording an identical exchange keeps the id a stored proposal points at."""
    fingerprint = request_fingerprint(_some_request())
    first = cassette_id_for(ProviderId.ANTHROPIC, "claude-opus-5", fingerprint)

    assert first == cassette_id_for(ProviderId.ANTHROPIC, "claude-opus-5", fingerprint)
    assert first != cassette_id_for(ProviderId.OPENAI, "claude-opus-5", fingerprint)
    assert first != cassette_id_for(ProviderId.ANTHROPIC, "claude-haiku-4-5", fingerprint)


@pytest.mark.parametrize(
    ("label", "update"),
    [
        ("the user prompt", {"user": "something else"}),
        ("the system prompt", {"system": "something else"}),
    ],
)
def test_anything_the_provider_would_see_changes_the_fingerprint(
    label: str, update: dict[str, str]
) -> None:
    """The *whole* body is hashed, which is what makes staleness undetectable-by-omission."""
    prompt = _first_subject_prompt()
    proposer = AnthropicMessagesProposer(_UnusableTransport())

    before = request_fingerprint(proposer.build_request(prompt))
    after = request_fingerprint(proposer.build_request(prompt.model_copy(update=update)))
    assert before != after, label


def test_the_output_ceiling_is_part_of_the_request_identity() -> None:
    """A changed ceiling changes what the provider was asked, so it must produce a miss.

    A harness keyed on the prompt alone would replay an answer produced under a different budget
    and report it as the same call.
    """
    request = _some_request()
    altered = dataclasses.replace(request, body={**request.body, "max_tokens": 1})
    assert request_fingerprint(request) != request_fingerprint(altered)


def test_the_path_is_part_of_the_request_identity() -> None:
    """Two providers, one cassette. The path is half of what keeps their recordings apart."""
    request = _some_request()
    altered = dataclasses.replace(request, path="/v1/somewhere-else")
    assert request_fingerprint(request) != request_fingerprint(altered)
