"""M3.2 — the closed proposal contract and the provider port.

The plan's exit criterion for this increment is two guards that pass **and are proven to fail when
deliberately violated**: a schema guard walking the JSON Schema for numeric types, amount-like
field names and additional properties, and a boundary guard asserting the calculator does not
import the proposal model. Both are here, with the mutations that kill them.

The thing being defended is one sentence: a model may choose a treatment, and nothing else. M3.1
proved the treatment set closes. This proves that the *only other things* a model can say are a
confidence band, a rationale nobody parses, some evidence identifiers, and whether it abstained —
and that a provider cannot widen that list by returning more.

Every guard takes the schema as an argument rather than reading it from the module, so the same
function can be run against a deliberately broken copy. A guard that can only be pointed at the
real thing can never be shown to work.
"""

from __future__ import annotations

import decimal
import inspect
import json
import pathlib
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pydantic
import pytest

from ledger_exception_control_plane.db.control import ConfidenceBand, TreatmentCode
from ledger_exception_control_plane.llm import (
    EvidenceRef,
    ProposalPrompt,
    ProviderId,
    ProviderRequest,
    ProviderResponseError,
    TreatmentProposal,
    TreatmentProposer,
    proposal_wire_schema,
)
from ledger_exception_control_plane.llm.providers import ANTHROPIC_MODEL_ID, OPENAI_MODEL_ID
from ledger_exception_control_plane.llm.providers.anthropic_messages import (
    AnthropicMessagesProposer,
)
from ledger_exception_control_plane.llm.providers.openai_chat import OpenAIChatProposer

PACKAGE_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"
)

#: The field list from `PROJECT_SPEC.md` §6.1, written out rather than read off the model. Derived
#: from the class it checks, this test would agree with any change to that class, which is the one
#: thing it must not do.
AUTHORITATIVE_FIELDS: Final = frozenset(
    {"treatment", "confidence", "rationale", "evidence_refs", "abstained"}
)

#: §6.1's amount-like pattern list, verbatim, plus the machine fields §6 puts on the system side of
#: the boundary: the account to post to, the period to post into, the operation identity and the
#: posting payload. A model that could name any of those would be deciding the instruction, not
#: proposing a treatment.
FORBIDDEN_TOKENS: Final = frozenset(
    {
        # §6.1, exactly as written there.
        "amount", "value", "total", "sum", "qty", "quantity", "rate",
        "pct", "percent", "balance", "delta", "fee", "price", "cost",
        # Deterministic, system-owned, and never the model's to choose (§7).
        "percentage", "account", "period", "posting", "debit", "credit", "operation",
        # Synonyms a reviewer got past the list above. `gl_code` is `account_code` by another name.
        "gl", "net", "gross", "charge", "settlement", "money", "monies", "tariff",
    }
)  # fmt: skip

A_VALID_PROPOSAL: Final = {
    "treatment": "rebook",
    "confidence": "high",
    "rationale": "The settlement line reverses ledger entry LE-77 and no entry offsets it.",
    "evidence_refs": [{"evidence_id": "EV-1"}, {"evidence_id": "EV-2"}],
    "abstained": False,
}


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload)


# ======================================================================================
# Walking the schema
# ======================================================================================


def _objects(schema: Any) -> Iterator[Mapping[str, Any]]:
    """Every object-shaped node in the tree: root, ``$defs``, arrays, and every combinator.

    Recursion rather than a top-level scan, because the interesting failures are nested. A numeric
    field inside ``EvidenceRef`` is exactly as fatal as one on the proposal, and it lives two levels
    down behind a ``$ref``.
    """
    if not isinstance(schema, Mapping):
        return
    yield schema
    for key in ("properties", "$defs", "definitions", "patternProperties"):
        for child in (schema.get(key) or {}).values():
            yield from _objects(child)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for child in schema.get(key) or []:
            yield from _objects(child)
    for key in ("items", "additionalProperties", "not", "contains"):
        child = schema.get(key)
        if isinstance(child, Mapping):
            yield from _objects(child)


def _declared_types(node: Mapping[str, Any]) -> set[str]:
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    return set()


def _property_names(schema: Mapping[str, Any]) -> set[str]:
    return {
        name
        for node in _objects(schema)
        for name in (node.get("properties") or {})
        if isinstance(name, str)
    }


def _tokens(field: str) -> set[str]:
    """Split a property name into words, then also offer each word's singular.

    Both halves are corrections a reviewer forced. ``postingAmount`` — a perfectly ordinary alias,
    and Pydantic emits aliases into the schema by default — was one token to the original splitter
    and matched nothing. ``amounts`` was likewise its own word. A guard that a rename defeats is a
    guard against typos.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", field)
    words = {part for part in spaced.lower().replace("-", "_").split("_") if part}
    return words | {word[:-1] for word in words if word.endswith("s") and len(word) > 3}


# ======================================================================================
# The guards. Each takes a schema, so each can be run against a mutated one.
# ======================================================================================


def assert_no_numeric_type(schema: Mapping[str, Any]) -> None:
    """No ``number``, no ``integer``, and no numeric value inside an enum.

    Both halves matter. A field typed ``string`` whose enum is ``[1, 2, 3]`` is a numeric channel
    with a non-numeric type annotation, and confidence is precisely where someone would put it.
    """
    for node in _objects(schema):
        numeric = _declared_types(node) & {"number", "integer"}
        assert not numeric, f"the schema declares a numeric type: {sorted(numeric)} in {node}"

        for key in ("enum", "default", "const", "examples"):
            candidate = node.get(key)
            values = candidate if isinstance(candidate, list) else [candidate]
            for value in values:
                if value is None:
                    continue
                assert not isinstance(value, (int, float, decimal.Decimal)) or isinstance(
                    value, bool
                ), f"the schema carries a numeric {key}: {value!r}"


def assert_every_object_is_closed(schema: Mapping[str, Any]) -> None:
    """``additionalProperties: false`` at every object boundary the contract owns.

    Not "Pydantic forbids extras at runtime" — the exported schema is what a provider is given and
    what a reviewer reads, and the two must say the same thing. A permissive schema with a strict
    validator is a contract that lies about itself.
    """
    for node in _objects(schema):
        if "properties" not in node:
            continue
        closed = node.get("additionalProperties")
        assert closed is False, (
            f"an object boundary is open: additionalProperties={closed!r} on "
            f"{sorted(node.get('properties', {}))}"
        )


def assert_no_financial_field(schema: Mapping[str, Any]) -> None:
    """No property whose name names money, an account, a period, or a posting.

    Structural: property *names* only. Descriptions are prose and prose legitimately discusses the
    words this list forbids — this very module does. Matching on prose would make the guard fire on
    its own documentation, which is how a guard gets weakened until it is switched off.
    """
    for name in _property_names(schema):
        offending = _tokens(name) & FORBIDDEN_TOKENS
        assert not offending, (
            f"the proposal schema carries a financial field {name!r} "
            f"(forbidden: {sorted(offending)})"
        )


def assert_no_free_form_container(schema: Mapping[str, Any]) -> None:
    """No object without declared properties, and no ``additionalProperties`` schema.

    An escape hatch does not need a numeric type to be an escape hatch. ``metadata: dict[str, Any]``
    declares no property names for the other guards to inspect, so everything above would pass
    while the model returned whatever it liked.
    """
    for node in _objects(schema):
        extra = node.get("additionalProperties")
        assert not isinstance(extra, Mapping), (
            f"additionalProperties is a schema, which is an open map: {extra}"
        )
        if "object" in _declared_types(node):
            assert node.get("properties"), f"an object declares no properties: {node}"


def assert_every_node_is_constrained(schema: Mapping[str, Any]) -> None:
    """Every schema node says what it accepts. An empty one accepts everything.

    This is the hole a reviewer drove a lorry through. ``metadata: dict[str, Any]`` is caught by the
    open-map check — but ``metadata: Any`` emits ``{}``, and a node with no ``type``, no ``enum``
    and no ``$ref`` slips past every guard that keys off one, while accepting any JSON value.
    The narrow escape hatch was guarded and the widest one was not.

    So the rule is stated positively: a schema node must constrain something.
    """
    for node in _objects(schema):
        if node.get("$ref") or node.get("enum") is not None or node.get("const") is not None:
            continue
        if any(node.get(key) for key in ("anyOf", "oneOf", "allOf", "not")):
            continue
        declared = _declared_types(node)
        assert declared, f"an unconstrained schema node accepts any value: {node}"

        if "array" in declared:
            items = node.get("items")
            assert isinstance(items, Mapping) and items, (
                f"an array declares no item schema, so its elements are unconstrained: {node}"
            )


def assert_required_fields_exist(schema: Mapping[str, Any]) -> None:
    """Everything ``required`` names must be a declared property.

    An unsatisfiable schema is not a safe schema. With ``additionalProperties: false`` no reply
    can ever match one, so the provider fails every call and the failure looks like a model problem.
    A reviewer produced exactly that by deleting a property while leaving its name in ``required``.
    """
    for node in _objects(schema):
        required = node.get("required")
        if not isinstance(required, list):
            continue
        declared = set(node.get("properties") or {})
        missing = {name for name in required if name not in declared}
        assert not missing, f"required names properties that do not exist: {sorted(missing)}"


def assert_treatment_is_the_canonical_enum(schema: Mapping[str, Any]) -> None:
    """``treatment`` resolves to exactly the four M3.1 values, and nothing else.

    Checked against the values written out in this file's sibling closure suite rather than against
    ``TreatmentCode`` itself, for the same reason: a guard that reads the enum agrees with the enum.
    """
    values = _enum_values_for(schema, "treatment")
    assert values == ["rebook", "accrue", "write_off", "escalate"], (
        f"the treatment vocabulary in the schema is {values}"
    )


def assert_confidence_is_a_closed_non_numeric_band(schema: Mapping[str, Any]) -> None:
    """``confidence`` is a closed enum of strings. The moment it is a number, §6.1 is broken."""
    values = _enum_values_for(schema, "confidence")
    assert values == ["low", "medium", "high"], f"the confidence vocabulary is {values}"
    for value in values:
        assert isinstance(value, str), f"confidence band {value!r} is not a string"


def _enum_values_for(schema: Mapping[str, Any], field: str) -> list[Any]:
    """The enum a named property resolves to, following one ``$ref`` into ``$defs``."""
    prop = (schema.get("properties") or {}).get(field)
    assert isinstance(prop, Mapping), f"the schema has no {field} property"

    ref = prop.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        prop = (schema.get("$defs") or {}).get(name)
        assert isinstance(prop, Mapping), f"{field} references a definition that is not there"

    enum = prop.get("enum")
    assert isinstance(enum, list), f"{field} is not a closed enum: {prop}"
    return list(enum)


ALL_GUARDS: Final = (
    assert_no_numeric_type,
    assert_every_object_is_closed,
    assert_no_financial_field,
    assert_no_free_form_container,
    assert_every_node_is_constrained,
    assert_required_fields_exist,
    assert_treatment_is_the_canonical_enum,
    assert_confidence_is_a_closed_non_numeric_band,
)

#: Both copies. The annotated one is what a reviewer reads; the stripped one is what a provider is
#: sent. A numeric field could not be in one and absent from the other, and checking both means the
#: stripping step can never become a place to hide something.
SCHEMAS: Final = {
    "annotated": TreatmentProposal.model_json_schema(),
    "wire": proposal_wire_schema(),
}


# ======================================================================================
# The contract is exactly what §6.1 says
# ======================================================================================


def test_the_proposal_has_exactly_the_authoritative_fields() -> None:
    assert set(TreatmentProposal.model_fields) == AUTHORITATIVE_FIELDS


def test_the_evidence_ref_is_an_opaque_identifier_and_nothing_else() -> None:
    """§6.1: ``EvidenceRef = { evidence_id: str }``. It carries no value."""
    assert set(EvidenceRef.model_fields) == {"evidence_id"}
    assert EvidenceRef.model_fields["evidence_id"].annotation is str


def test_the_treatment_vocabulary_is_the_canonical_one() -> None:
    """Not a copy of it. The same object M2.4 accepts and M1.2 stores."""
    assert TreatmentProposal.model_fields["treatment"].annotation is TreatmentCode


def test_the_confidence_vocabulary_is_the_canonical_band() -> None:
    assert TreatmentProposal.model_fields["confidence"].annotation is ConfidenceBand
    assert [band.value for band in ConfidenceBand] == ["low", "medium", "high"]


@pytest.mark.parametrize("name", sorted(SCHEMAS))
@pytest.mark.parametrize("guard", ALL_GUARDS, ids=lambda g: g.__name__)
def test_every_guard_passes_on_the_real_schema(
    guard: Callable[[Mapping[str, Any]], None], name: str
) -> None:
    guard(SCHEMAS[name])


def test_a_valid_proposal_round_trips() -> None:
    proposal = TreatmentProposal.model_validate_json(_json(A_VALID_PROPOSAL))
    assert proposal.treatment is TreatmentCode.REBOOK
    assert proposal.confidence is ConfidenceBand.HIGH
    assert [ref.evidence_id for ref in proposal.evidence_refs] == ["EV-1", "EV-2"]
    assert proposal.abstained is False

    again = TreatmentProposal.model_validate_json(proposal.model_dump_json())
    assert again == proposal


def test_a_validated_proposal_cannot_be_changed_afterwards() -> None:
    """``frozen=True`` blocks assignment. It does nothing about a container a field holds.

    So ``evidence_refs`` is a tuple, and that was a correction: as a list, a validated proposal
    could have its citations appended to or cleared **after** validation and after the citation
    check — by anything holding a reference to it. A reviewer emptied one.

    It matters here more than as tidiness. The citation check refuses a proposal whole rather than
    trimming it, precisely so that nobody rewrites a provenance record; a mutable list let a caller
    do afterwards exactly what that check exists to forbid. Third occurrence of the shape in this
    project, after ``AccountPolicy`` and ``ProviderRequest``.
    """
    proposal = TreatmentProposal.model_validate_json(_json(A_VALID_PROPOSAL))

    with pytest.raises(pydantic.ValidationError):
        proposal.treatment = TreatmentCode.WRITE_OFF

    assert isinstance(proposal.evidence_refs, tuple)
    for forbidden in ("append", "clear", "extend", "insert", "pop", "remove", "__setitem__"):
        assert not hasattr(proposal.evidence_refs, forbidden), (
            f"citations can be changed with {forbidden}"
        )

    # And the elements are closed too, so the escape is not one level down.
    with pytest.raises(pydantic.ValidationError):
        proposal.evidence_refs[0].evidence_id = "EV-9"


def test_the_json_representation_is_stable() -> None:
    """The serialised form is the enum *values*, which is what the database column stores."""
    dumped = json.loads(
        TreatmentProposal.model_validate_json(_json(A_VALID_PROPOSAL)).model_dump_json()
    )
    assert dumped == A_VALID_PROPOSAL


def test_the_wire_schema_is_stable() -> None:
    """Pinned. A provider is configured against this shape and cassettes are recorded under it."""
    assert proposal_wire_schema() == {
        "$defs": {
            "ConfidenceBand": {"enum": ["low", "medium", "high"], "type": "string"},
            "EvidenceRef": {
                "additionalProperties": False,
                "properties": {"evidence_id": {"type": "string"}},
                "required": ["evidence_id"],
                "type": "object",
            },
            "TreatmentCode": {
                "enum": ["rebook", "accrue", "write_off", "escalate"],
                "type": "string",
            },
        },
        "additionalProperties": False,
        "properties": {
            "treatment": {"$ref": "#/$defs/TreatmentCode"},
            "confidence": {"$ref": "#/$defs/ConfidenceBand"},
            "rationale": {"type": "string"},
            "evidence_refs": {"items": {"$ref": "#/$defs/EvidenceRef"}, "type": "array"},
            "abstained": {"type": "boolean"},
        },
        "required": ["treatment", "confidence", "rationale", "evidence_refs", "abstained"],
        "type": "object",
    }


def test_the_wire_schema_carries_no_internal_documentation() -> None:
    """The annotated schema embeds every docstring in the tree. A provider gets none of it.

    Not tidiness: those docstrings are the engineering argument behind a financial control, they
    would be sent on every call, and one of them contains the worked example of the numeric escape
    hatch this contract exists to refuse.
    """
    rendered = json.dumps(proposal_wire_schema())
    assert "description" not in rendered
    assert "write_off_125_50" not in rendered
    assert "escape hatch" not in rendered

    annotated = json.dumps(TreatmentProposal.model_json_schema())
    assert "description" in annotated, "the annotated copy is the one that keeps the prose"


# ======================================================================================
# Strict validation — a provider's JSON is untrusted input
# ======================================================================================


def _rejected(**overrides: object) -> pydantic.ValidationError:
    payload = {**A_VALID_PROPOSAL, **overrides}
    with pytest.raises(pydantic.ValidationError) as caught:
        TreatmentProposal.model_validate_json(_json(payload))
    return caught.value


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("an integer treatment", {"treatment": 1}),
        ("a boolean treatment", {"treatment": True}),
        ("an unknown treatment", {"treatment": "auto_post"}),
        ("a treatment in the wrong case", {"treatment": "REBOOK"}),
        ("a treatment with whitespace", {"treatment": " rebook"}),
        ("a numeric confidence", {"confidence": 0.9}),
        ("an integer confidence", {"confidence": 1}),
        ("an unknown confidence", {"confidence": "very_high"}),
        ("a stringy boolean", {"abstained": "true"}),
        ("a numeric boolean", {"abstained": 1}),
        ("a rationale that is a list", {"rationale": ["text"]}),
        ("a rationale that is a number", {"rationale": 125.50}),
        ("evidence refs as a bare string", {"evidence_refs": "EV-1"}),
        ("evidence refs as bare strings", {"evidence_refs": ["EV-1"]}),
        ("an evidence ref that is a number", {"evidence_refs": [{"evidence_id": 1}]}),
    ],
)
def test_a_malformed_provider_value_is_refused(label: str, overrides: dict[str, object]) -> None:
    """None of these may be coerced into something plausible.

    Strict mode is the point. In lax mode Pydantic would turn ``"true"`` into ``True`` and ``1``
    into an enum member, and a provider that answered nonsense would produce a domain object that
    looks like a decision. The failure has to be loud at the boundary, because after the boundary
    everything downstream is entitled to assume the object is real.
    """
    assert _rejected(**overrides)


@pytest.mark.parametrize(
    "extra",
    [
        {"amount": 125.50},
        {"amount": "125.50"},
        {"account": "4900"},
        {"account_code": "4900"},
        {"period": "2026-01"},
        {"operation_id": "abc"},
        {"metadata": {"amount": 1}},
        {"confidence_score": 0.9},
    ],
)
def test_an_extra_field_is_refused_rather_than_ignored(extra: dict[str, object]) -> None:
    """Silently dropping an unexpected field is the dangerous behaviour, not the safe one.

    A provider that returns ``amount`` is a provider whose output nobody should trust for this
    exception, and the response that carried it should fail loudly rather than be quietly trimmed
    into something acceptable.
    """
    assert _rejected(**extra)


def test_a_nested_extra_field_is_refused() -> None:
    assert _rejected(evidence_refs=[{"evidence_id": "EV-1", "amount": "125.50"}])


def test_a_missing_field_is_refused() -> None:
    payload = dict(A_VALID_PROPOSAL)
    del payload["abstained"]
    with pytest.raises(pydantic.ValidationError):
        TreatmentProposal.model_validate_json(_json(payload))


def test_a_validated_treatment_is_the_canonical_member_not_a_lookalike() -> None:
    """Identity, not equality — the defect M3.1 found, checked at the boundary that will feed it.

    ``TreatmentCode`` is a ``StrEnum``, so ``"rebook" == TreatmentCode.REBOOK``. If validation
    returned the bare string this would still compare equal everywhere and still be refused by the
    calculator's identity check, which would look like a calculator bug rather than a parser one.
    """
    proposal = TreatmentProposal.model_validate_json(_json(A_VALID_PROPOSAL))
    assert proposal.treatment is TreatmentCode.REBOOK
    assert any(proposal.treatment is member for member in TreatmentCode)


def test_a_validated_proposal_cannot_be_edited() -> None:
    """Provenance that later code can rewrite is not provenance."""
    proposal = TreatmentProposal.model_validate_json(_json(A_VALID_PROPOSAL))
    with pytest.raises(pydantic.ValidationError):
        proposal.treatment = TreatmentCode.WRITE_OFF


# ======================================================================================
# Abstention
# ======================================================================================


def test_an_abstaining_proposal_must_escalate() -> None:
    for treatment in TreatmentCode:
        if treatment is TreatmentCode.ESCALATE:
            continue
        with pytest.raises(pydantic.ValidationError, match="must escalate"):
            TreatmentProposal.model_validate_json(
                _json({**A_VALID_PROPOSAL, "treatment": treatment.value, "abstained": True})
            )


def test_abstaining_while_escalating_is_valid() -> None:
    proposal = TreatmentProposal.model_validate_json(
        _json({**A_VALID_PROPOSAL, "treatment": "escalate", "abstained": True})
    )
    assert proposal.abstained is True


def test_escalating_without_abstaining_is_valid() -> None:
    """The implication is one-directional, and this is the case that proves it.

    A model that read the evidence and concluded a human must decide has *made* a decision. It is
    not the same event as declining to answer, the database constraint
    (``NOT abstained OR treatment = 'escalate'``) permits it, and ADR-048 says so explicitly.
    Enforcing an equivalence here would reject valid model output and contradict the schema the
    proposal is eventually stored in.
    """
    proposal = TreatmentProposal.model_validate_json(
        _json({**A_VALID_PROPOSAL, "treatment": "escalate", "abstained": False})
    )
    assert proposal.treatment is TreatmentCode.ESCALATE
    assert proposal.abstained is False


def test_the_abstention_rule_agrees_with_the_database_constraint() -> None:
    """The two must not drift. The SQL is the authority the contract is checked against."""
    source = (PACKAGE_ROOT / "db" / "control.py").read_text(encoding="utf-8")
    assert f"NOT abstained OR treatment = '{TreatmentCode.ESCALATE.value}'" in source


def test_abstention_is_not_a_fifth_treatment() -> None:
    assert "abstain" not in {member.value for member in TreatmentCode}
    assert len(TreatmentCode) == 4


# ======================================================================================
# The provider port
# ======================================================================================


class _FakeTransport:
    """A transport that returns a canned body and records what it was asked to send."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = payload
        self.sent: list[ProviderRequest] = []

    async def send(self, request: ProviderRequest) -> Mapping[str, object]:
        self.sent.append(request)
        return self._payload


def _anthropic_body(proposal: Mapping[str, object]) -> dict[str, object]:
    """A Messages response: the answer is a text block in a content list."""
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": ANTHROPIC_MODEL_ID,
        "content": [{"type": "text", "text": _json(proposal)}],
        "stop_reason": "end_turn",
    }


def _openai_body(proposal: Mapping[str, object]) -> dict[str, object]:
    """A Chat Completions response: the answer is a JSON string inside a choice's message."""
    return {
        "id": "chatcmpl-01",
        "object": "chat.completion",
        "model": OPENAI_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _json(proposal), "refusal": None},
                "finish_reason": "stop",
            }
        ],
    }


PROMPT: Final = ProposalPrompt(system="You triage settlement exceptions.", user="Exception EX-1.")

PROVIDERS: Final = (
    (ProviderId.ANTHROPIC, AnthropicMessagesProposer, _anthropic_body),
    (ProviderId.OPENAI, OpenAIChatProposer, _openai_body),
)


@pytest.mark.parametrize(("provider", "adapter", "body"), PROVIDERS, ids=lambda p: str(p))
def test_each_adapter_satisfies_the_port(
    provider: ProviderId,
    adapter: type[Any],
    body: Callable[[Mapping[str, object]], dict[str, object]],
) -> None:
    proposer = adapter(_FakeTransport(body(A_VALID_PROPOSAL)))
    assert isinstance(proposer, TreatmentProposer)
    assert proposer.provider is provider
    assert proposer.model_id

    # `isinstance` against a runtime-checkable Protocol only asks whether the attributes exist. A
    # reviewer satisfied it with `provider = "anthropic"`, `model_id = 42` and a nought-argument
    # `propose` returning a dict with an amount in it — so the shape is checked properly here.
    signature = inspect.signature(proposer.propose)
    assert list(signature.parameters) == ["prompt"]
    assert inspect.iscoroutinefunction(proposer.propose), "the port is async"
    assert isinstance(proposer.provider, ProviderId)
    assert isinstance(proposer.model_id, str)


@pytest.mark.asyncio
async def test_swapping_the_provider_does_not_change_the_proposal() -> None:
    """**The portability claim.** Two vendors, two response shapes, one identical domain object.

    This is what the port is for. The two payloads below are genuinely different — a content-block
    list against a JSON string nested in a choice — and if either adapter leaked any part of that
    difference into its return value, the equality here would fail.
    """
    proposals = [
        await adapter(_FakeTransport(body(A_VALID_PROPOSAL))).propose(PROMPT)
        for _provider, adapter, body in PROVIDERS
    ]
    assert proposals[0] == proposals[1]
    assert all(isinstance(p, TreatmentProposal) for p in proposals)
    assert proposals[0].treatment is TreatmentCode.REBOOK


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "adapter", "body"), PROVIDERS, ids=lambda p: str(p))
async def test_each_adapter_sends_the_closed_schema_as_the_output_constraint(
    provider: ProviderId,
    adapter: type[Any],
    body: Callable[[Mapping[str, object]], dict[str, object]],
) -> None:
    """The schema on the wire is generated from the class, so the two cannot drift apart."""
    transport = _FakeTransport(body(A_VALID_PROPOSAL))
    await adapter(transport).propose(PROMPT)

    (request,) = transport.sent
    rendered = json.dumps(request.body)
    assert json.dumps(proposal_wire_schema()) in rendered, (
        "the request does not carry the closed schema verbatim"
    )
    assert "amount" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "adapter", "body"), PROVIDERS, ids=lambda p: str(p))
async def test_a_provider_returning_an_amount_cannot_cross_the_port(
    provider: ProviderId,
    adapter: type[Any],
    body: Callable[[Mapping[str, object]], dict[str, object]],
) -> None:
    """The whole point, exercised end to end at the boundary."""
    hostile = {**A_VALID_PROPOSAL, "amount": "125.50", "account": "4900"}
    with pytest.raises(ProviderResponseError):
        await adapter(_FakeTransport(body(hostile))).propose(PROMPT)


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "adapter", "body"), PROVIDERS, ids=lambda p: str(p))
@pytest.mark.parametrize(
    "answer",
    ["not json at all", "{}", '{"treatment": "auto_post"}', '{"treatment": "rebook"}', "null"],
    ids=["not-json", "empty-object", "unknown-treatment", "missing-fields", "null"],
)
async def test_a_malformed_provider_response_is_refused(
    provider: ProviderId,
    adapter: type[Any],
    body: Callable[[Mapping[str, object]], dict[str, object]],
    answer: str,
) -> None:
    """Whatever the envelope, the answer inside it still has to be a proposal."""
    payload = body(A_VALID_PROPOSAL)
    _set_answer(payload, answer)
    with pytest.raises(ProviderResponseError):
        await adapter(_FakeTransport(payload)).propose(PROMPT)


def _set_answer(payload: dict[str, object], text: str) -> None:
    """Overwrite the answer text in whichever envelope this is, leaving the envelope intact."""
    if "content" in payload:
        blocks = payload["content"]
        assert isinstance(blocks, list)
        blocks[0] = {"type": "text", "text": text}
        return
    choices = payload["choices"]
    assert isinstance(choices, list)
    choices[0]["message"]["content"] = text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"content": "not a list"},
        {"content": []},
        {"content": [{"type": "thinking", "thinking": "..."}]},
        {"content": [{"type": "text"}]},
    ],
)
def test_anthropic_shape_failures_are_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ProviderResponseError):
        AnthropicMessagesProposer(_FakeTransport({})).parse(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": "nope"},
        {"choices": ["not an object"]},
        {"choices": [{"index": 0, "finish_reason": "length", "message": {"content": "{}"}}]},
        {
            "choices": [
                {"index": 0, "finish_reason": "content_filter", "message": {"content": None}}
            ]
        },
        {"choices": [{"index": 0}]},
        {"choices": [{"index": 0, "message": {"content": None}}]},
        {"choices": [{"index": 0, "message": {"refusal": "I cannot help with that"}}]},
    ],
)
def test_openai_shape_failures_are_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ProviderResponseError):
        OpenAIChatProposer(_FakeTransport({})).parse(payload)


def test_a_provider_refusal_is_not_silently_turned_into_an_abstention() -> None:
    """An API declining to answer and a model choosing to abstain are different events.

    Collapsing them would write a model decision into the audit trail that no model made — and it
    would be the *safe-looking* kind of wrong, since escalation is the conservative outcome.
    """
    with pytest.raises(ProviderResponseError, match="declined"):
        OpenAIChatProposer(_FakeTransport({})).parse(
            {"choices": [{"message": {"refusal": "no", "content": None}}]}
        )


@pytest.mark.parametrize(
    ("stop", "expected"),
    [("length", "output ceiling"), ("content_filter", "content filter")],
)
def test_a_truncated_or_filtered_answer_says_which_it_was(stop: str, expected: str) -> None:
    """Both leave ``content`` null, and the diagnosis is the whole value of naming them.

    Unnamed, either arrives as "content is not a JSON string" — which reads as a malformed provider
    and sends an operator to look at the wrong thing. ``content_filter`` was the unhandled one, and
    the module docstring claimed otherwise; a reviewer read the two against each other.
    """
    with pytest.raises(ProviderResponseError, match=expected):
        OpenAIChatProposer(_FakeTransport({})).parse(
            {"choices": [{"index": 0, "finish_reason": stop, "message": {"content": None}}]}
        )


def test_the_adapters_differ_in_the_request_they_build() -> None:
    """Otherwise "two providers" is one provider with two names, which OPEN-5 warns about."""
    anthropic = AnthropicMessagesProposer(_FakeTransport({})).build_request(PROMPT)
    openai = OpenAIChatProposer(_FakeTransport({})).build_request(PROMPT)

    assert anthropic.path != openai.path
    assert anthropic.body["system"] == PROMPT.system
    assert "system" not in openai.body
    assert {"role": "system", "content": PROMPT.system} in openai.body["messages"]  # type: ignore[operator]
    assert "output_config" in anthropic.body
    assert "response_format" in openai.body


def _keys(node: object) -> Iterator[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def test_no_provider_request_carries_a_credential() -> None:
    """Authentication belongs to the transport, so no adapter or cassette can come to hold a key.

    Checked on keys and values rather than on the rendered string. The first version of this test
    matched substrings and failed on ``max_tokens``, which contains "token" — the same
    false-positive trap the financial-field guard is written to avoid, walked straight into two
    hundred lines below the comment warning about it.
    """
    assert set(ProviderRequest.__dataclass_fields__) == {"path", "body"}, (
        "the request grew a place to put headers"
    )

    credential_keys = {
        "api_key", "apikey", "authorization", "auth", "x-api-key",
        "secret", "credential", "password", "bearer",
    }  # fmt: skip
    for _provider, adapter, _body in PROVIDERS:
        body = adapter(_FakeTransport({})).build_request(PROMPT).body
        for key in _keys(body):
            assert key.lower().replace("-", "_") not in credential_keys, (
                f"a provider request carries a credential-shaped key: {key!r}"
            )
        for value in _strings(body):
            assert not value.startswith(("sk-", "Bearer ")), (
                f"a provider request carries a credential-shaped value: {value[:12]!r}"
            )


def test_the_pinned_model_identifiers_are_recorded() -> None:
    """OPEN-5 requires the exact models to be pinned, because a measurement without one is noise.

    Asserted so a re-pin is a deliberate edit that shows up in a diff, not a value someone changed
    while debugging. The two are stamped differently on purpose: OpenAI's identifier carries its
    own snapshot date, Anthropic's does not carry one at all — appending a date to a current Claude
    id produces a model that does not exist — so that pin's date lives in ADR-049.
    """
    assert ANTHROPIC_MODEL_ID == "claude-opus-5"
    assert OPENAI_MODEL_ID == "gpt-5.4-mini-2026-03-17"

    assert AnthropicMessagesProposer(_FakeTransport({})).model_id == ANTHROPIC_MODEL_ID
    assert OpenAIChatProposer(_FakeTransport({})).model_id == OPENAI_MODEL_ID

    #: A caller may override for an evaluation sweep without touching the pin.
    assert AnthropicMessagesProposer(_FakeTransport({}), model_id="other").model_id == "other"
