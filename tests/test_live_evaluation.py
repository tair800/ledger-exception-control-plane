"""The live evaluation path (6.4): the tool envelope, the bounds, and the leakage refusal.

**Every test here is offline.** The one component that can dial is handed a fake, and the captured
cassette is replayed from disk. That is not a convenience — the whole argument for putting an HTTP
transport in this repository at all is that it changes nothing about which suites need a network,
and a test file that needed one would refute it.

Three things are under test, in order of how much damage they prevent:

1. **The prompt cannot carry the answer.** :func:`assert_no_answer_leaks` is the last thing that
   runs before a paid call, so it is exercised against a deliberately poisoned prompt as well as
   against the real ones.
2. **The run is bounded by arithmetic.** The budget raises; it does not warn.
3. **The tool envelope parses what a router actually returns**, including the failures — a model
   answering in prose despite ``tool_choice``, a router forwarding somebody else's tool call.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

import pytest

from ledger_exception_control_plane.db.control import ConfidenceBand, TreatmentCode
from ledger_exception_control_plane.llm.cassette import (
    Origin,
    ReplayTransport,
    load_cassette,
    request_fingerprint,
)
from ledger_exception_control_plane.llm.port import (
    ProviderRequest,
    ProviderResponseError,
)
from ledger_exception_control_plane.llm.providers.openai_tools import OpenAIToolProposer
from ledger_exception_control_plane.llm.schema import ProposalPrompt, proposal_wire_schema
from tests.evaluation.golden import load_golden_set
from tests.evaluation.livecapture import (
    ANSWER_FIELDS,
    LIVE_CASSETTE_PATH,
    LIVE_PROPOSALS_PATH,
    LIVE_RUN_PATH,
    assert_no_answer_leaks,
    build_subjects,
)
from tests.evaluation.livetransport import (
    BudgetExhaustedError,
    CallBudget,
    api_origin,
    route_of,
)

PROMPT = ProposalPrompt(system="policy", user='{"exception":{}}')


class _Unusable:
    """A transport that fails if anything tries to send through it."""

    async def send(self, request: ProviderRequest) -> Mapping[str, Any]:  # pragma: no cover
        raise AssertionError("this test must not send anything")


def _proposer() -> OpenAIToolProposer:
    return OpenAIToolProposer(_Unusable(), model_id="some/alias")


def _tool_response(arguments: str, *, name: str = "treatment_proposal") -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ]
    }


VALID = json.dumps(
    {
        "treatment": "escalate",
        "confidence": "low",
        "rationale": "not enough evidence",
        "evidence_refs": [{"evidence_id": "e-1"}],
        "abstained": True,
    }
)


# ======================================================================================
# The tool envelope
# ======================================================================================


def test_the_tool_carries_the_same_schema_the_other_adapter_puts_in_response_format() -> None:
    """One definition of the contract, two places it can travel.

    Asserted by identity of content rather than by reading both modules: a second, drifting copy
    of the schema is exactly the failure the shared ``validated_proposal`` was written to prevent,
    and it would be invisible until a provider enforced one of them.
    """
    body = _proposer().build_request(PROMPT).body
    tools = body["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["parameters"] == proposal_wire_schema()


def test_the_tool_call_is_forced_and_the_stream_flag_is_explicit() -> None:
    """Both are load-bearing against a routed endpoint, and neither is a default worth trusting.

    A router streams a tool call when ``stream`` is absent — a 200 whose body is ``data:`` frames
    — and lets the model answer in prose when ``tool_choice`` is ``auto``. Both were observed on
    the real route before the evaluation ran.
    """
    body = _proposer().build_request(PROMPT).body
    assert body["stream"] is False
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "treatment_proposal"},
    }


def test_the_request_carries_no_credential_and_no_authentication_field() -> None:
    """The adapter shapes the payload; the transport owns the credential. Asserted, not assumed.

    By key, not by substring — the first version banned the string ``token`` and tripped on
    ``max_completion_tokens``, which is the output ceiling. Twice in one file was enough to make
    the point: a scan that matches vocabulary instead of structure fails on the innocent case and
    teaches whoever reads it to ignore the next one.
    """
    banned = {
        "authorization",
        "api_key",
        "apikey",
        "api-key",
        "x-api-key",
        "bearer",
        "credential",
        "secret",
        "headers",
        "auth",
    }

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return {str(k).lower() for k in node} | {k for v in node.values() for k in keys(v)}
        if isinstance(node, list):
            return {k for item in node for k in keys(item)}
        return set()

    body = _proposer().build_request(PROMPT).body
    assert keys(body) & banned == set()
    assert "sk-" not in json.dumps(body)


def test_a_routed_alias_reports_unversioned_rather_than_guessing() -> None:
    """``auto/best-free`` carries no dated snapshot, and inventing one would name a model that was
    never called."""
    assert OpenAIToolProposer(_Unusable(), model_id="auto/best-free").model_version == "unversioned"
    assert (
        OpenAIToolProposer(_Unusable(), model_id="gpt-5.4-mini-2026-03-17").model_version
        == "2026-03-17"
    )


def test_a_conformant_tool_call_validates() -> None:
    proposal = _proposer().parse(_tool_response(VALID))
    assert proposal.treatment is TreatmentCode.ESCALATE
    assert proposal.confidence is ConfidenceBand.LOW
    assert proposal.abstained is True


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({}, "no choices"),
        ({"choices": []}, "no choices"),
        ({"choices": [{"finish_reason": "length"}]}, "output ceiling"),
        ({"choices": [{"finish_reason": "content_filter"}]}, "content filter"),
        ({"choices": [{"message": {"refusal": "no"}}]}, "declined to answer"),
        ({"choices": [{"message": {"content": "I think escalate."}}]}, "without calling"),
        ({"choices": [{"message": {"tool_calls": []}}]}, "without calling"),
    ],
)
def test_every_shape_that_is_not_a_proposal_is_refused(
    payload: dict[str, Any], fragment: str
) -> None:
    """Each failure is named individually, because the diagnosis is the value of the check.

    The prose case is the one that actually happens on a routed endpoint: a model that ignores
    ``tool_choice`` and answers in a sentence. Reported as "content is not JSON" it would send an
    operator to look at a parser.
    """
    with pytest.raises(ProviderResponseError, match=fragment):
        _proposer().parse(payload)


def test_somebody_elses_tool_call_is_refused_even_if_its_arguments_would_fit() -> None:
    """A router forwarding a different function's call must not have it read as a proposal.

    The arguments here are a *valid* proposal, so nothing downstream would object. The name is the
    only thing that says this answer was given to this contract.
    """
    with pytest.raises(ProviderResponseError, match="rather than 'treatment_proposal'"):
        _proposer().parse(_tool_response(VALID, name="something_else"))


def test_arguments_that_are_not_a_json_string_are_refused() -> None:
    response = _tool_response(VALID)
    response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {"treatment": "x"}
    with pytest.raises(ProviderResponseError, match="not a JSON string"):
        _proposer().parse(response)


# ======================================================================================
# The bounds
# ======================================================================================


def test_the_budget_raises_rather_than_warning() -> None:
    budget = CallBudget(maximum=2)
    budget.take()
    budget.take()
    with pytest.raises(BudgetExhaustedError, match="budget of 2 is exhausted"):
        budget.take()
    assert budget.spent == 2, "a refused call is not spent"


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        ("http://gw:8080/v1", "/v1/chat/completions", "http://gw:8080"),
        ("http://gw:8080/v1/", "/v1/chat/completions", "http://gw:8080"),
        ("http://gw:8080", "/v1/chat/completions", "http://gw:8080"),
        ("https://api.openai.com/v1", "/v1/chat/completions", "https://api.openai.com"),
        # Only an exact trailing segment, and only when the path repeats it.
        ("http://gw/v1/v1", "/v1/chat/completions", "http://gw/v1"),
        ("https://api.anthropic.com", "/v1/messages", "https://api.anthropic.com"),
        ("http://gw/openai", "/v1/chat/completions", "http://gw/openai"),
    ],
)
def test_the_base_url_is_reconciled_with_the_adapter_path(
    base: str, path: str, expected: str
) -> None:
    """An OpenAI-style base URL ends ``/v1``; the adapter path begins with it. Both are right."""
    assert api_origin(base, path) == expected


def test_a_route_with_credential_shaped_material_is_not_echoed() -> None:
    """A base URL is configuration, but it is also somewhere people put tokens."""
    assert "hunter2" not in route_of("https://user:hunter2@gw.example/v1")
    assert "k=abc" not in route_of("https://gw.example/v1?k=abc")


# ======================================================================================
# The leakage firewall
# ======================================================================================


def test_the_real_prompts_carry_no_answer() -> None:
    """The measurement's premise, checked over every record rather than a sample."""
    golden = load_golden_set()
    subjects = build_subjects()
    from ledger_exception_control_plane.llm.evidence import assemble_evidence
    from ledger_exception_control_plane.llm.prompt import build_prompt
    from ledger_exception_control_plane.matching import DEFAULT_POLICY

    prompts = {
        record.exception_id: build_prompt(
            subjects[record.exception_id][0],
            assemble_evidence(*subjects[record.exception_id], DEFAULT_POLICY),
        )
        for record in golden.records
    }
    assert len(prompts) == len(golden.records)
    assert_no_answer_leaks(prompts, golden.records)


def test_a_poisoned_prompt_is_refused_before_anything_is_sent() -> None:
    """A guard nobody has watched fail is a guard nobody has watched fail.

    Both halves are exercised: a field *name* appearing, which catches a future edit that starts
    interpolating a golden record, and a label's *text* appearing under some other key, which the
    field-name check alone would miss entirely.
    """
    golden = load_golden_set()
    record = golden.hold_out[0]

    by_name = {record.exception_id: ProposalPrompt(system="p", user='{"expected_treatment":"x"}')}
    with pytest.raises(RuntimeError, match="part of the answer key"):
        assert_no_answer_leaks(by_name, [record])

    by_value = {
        record.exception_id: ProposalPrompt(system="p", user=json.dumps({"note": record.label_why}))
    }
    with pytest.raises(RuntimeError, match="the value of label_why"):
        assert_no_answer_leaks(by_value, [record])


def test_every_answer_bearing_field_of_a_golden_record_is_in_the_forbidden_list() -> None:
    """The list is checked against the dataclass, not maintained by hand.

    A field added to ``GoldenRecord`` that carried an answer and was not added here would leave the
    firewall silently narrower than it looks. The state block is enumerated instead, so a new
    *state* field does not trip this and a new *label* field does.
    """
    state = {
        "exception_id",
        "classification",
        "rule_id",
        "amount",
        "currency",
        "value_date",
        "settlement_period",
        "originating_period",
        "transaction_type",
        "has_merchant_reference",
    }
    fields = {f.name for f in dataclasses.fields(load_golden_set().records[0])}
    assert fields - state == set(ANSWER_FIELDS), (
        "a golden record field is neither declared state nor a guarded answer field"
    )


# ======================================================================================
# The captured run: it exists, it is marked captured, and it replays offline
# ======================================================================================


def test_the_captured_cassette_is_marked_captured_and_is_not_the_synthesised_one() -> None:
    """``origin`` is the difference between a headline that may be quoted and one that may not."""
    cassette = load_cassette(LIVE_CASSETTE_PATH)
    assert cassette.interactions
    assert {i.origin for i in cassette.interactions} == {Origin.CAPTURED}
    synthesised = load_cassette(
        LIVE_CASSETTE_PATH.parents[2] / "cassettes" / "canonical-corpus.json"
    )
    assert {i.origin for i in synthesised.interactions} == {Origin.SYNTHESISED}


@pytest.mark.asyncio
async def test_the_captured_responses_replay_offline_to_the_same_proposals() -> None:
    """The reproduction check §11 asks for: no network, and the same answers.

    This is what makes the published numbers auditable rather than a printout. The cassette is
    replayed through the same adapter that recorded it, and every proposal must come back
    identical to the row committed in the proposals file.
    """
    committed = {
        row["exception_id"]: row
        for row in (
            json.loads(line)
            for line in LIVE_PROPOSALS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    run = json.loads(LIVE_RUN_PATH.read_text(encoding="utf-8"))
    alias = run["model_alias"]

    subjects = build_subjects()
    from ledger_exception_control_plane.llm.evidence import assemble_evidence
    from ledger_exception_control_plane.llm.prompt import build_prompt
    from ledger_exception_control_plane.matching import DEFAULT_POLICY

    replay = ReplayTransport(load_cassette(LIVE_CASSETTE_PATH))
    proposer = OpenAIToolProposer(replay, model_id=alias)

    replayed = 0
    for exception_id, row in committed.items():
        prompt = build_prompt(
            subjects[exception_id][0],
            assemble_evidence(*subjects[exception_id], DEFAULT_POLICY),
        )
        proposal = await proposer.propose(prompt)
        assert proposal.treatment.value == row["treatment"], exception_id
        assert proposal.abstained == row["abstained"], exception_id
        replayed += 1

    assert replayed == len(committed) > 0


def test_the_recorded_requests_match_what_the_adapter_builds_today() -> None:
    """A cassette that no longer matches its own builder cannot be replayed, and would rot silently.

    Checked as fingerprints rather than by replaying, so this fails with "the request changed"
    rather than with a miss deep inside a proposer.
    """
    cassette = load_cassette(LIVE_CASSETTE_PATH)
    recorded = {i.request_fingerprint for i in cassette.interactions}
    alias = json.loads(LIVE_RUN_PATH.read_text(encoding="utf-8"))["model_alias"]

    subjects = build_subjects()
    from ledger_exception_control_plane.llm.evidence import assemble_evidence
    from ledger_exception_control_plane.llm.prompt import build_prompt
    from ledger_exception_control_plane.matching import DEFAULT_POLICY

    proposer = OpenAIToolProposer(_Unusable(), model_id=alias)
    rebuilt = {
        request_fingerprint(
            proposer.build_request(
                build_prompt(
                    subjects[exception_id][0],
                    assemble_evidence(*subjects[exception_id], DEFAULT_POLICY),
                )
            )
        )
        for exception_id in (
            json.loads(line)["exception_id"]
            for line in LIVE_PROPOSALS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    assert rebuilt <= recorded


def test_no_credential_reached_any_committed_artefact() -> None:
    """The scrubber runs inside the recorder; this asserts the result, over the real files.

    **Structural, not a substring sweep, and that was a correction.** The first version failed the
    word ``authorization`` anywhere in the text — and hit a model rationale discussing ledger
    *authorization entries*, which is domain vocabulary and not a header. A secret scan that fires
    on the word "authorization" in prose is a scan people learn to wave through, which is exactly
    how a real one gets missed.

    So: credential *shapes* are refused anywhere, and credential-carrying *keys* are refused
    structurally, wherever they appear in the parsed document.
    """
    banned_keys = {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "api_key",
        "apikey",
        "cookie",
        "set-cookie",
    }

    def walk(node: object, path: str = "") -> list[str]:
        if isinstance(node, dict):
            found = [f"{path}/{k}" for k in node if str(k).lower() in banned_keys]
            for k, v in node.items():
                found += walk(v, f"{path}/{k}")
            return found
        if isinstance(node, list):
            return [f for n, item in enumerate(node) for f in walk(item, f"{path}[{n}]")]
        return []

    for path in (LIVE_CASSETTE_PATH, LIVE_RUN_PATH):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert walk(document) == [], f"{path.name} carries a credential-bearing key"

    for path in (LIVE_CASSETTE_PATH, LIVE_PROPOSALS_PATH, LIVE_RUN_PATH):
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text
        assert "Bearer " not in text
        assert "LLM_API_KEY=" not in text
