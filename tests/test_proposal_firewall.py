"""M3.2 — the AI/money firewall, and the mutations that prove the guards work.

`PROJECT_SPEC.md` §23 makes two of these acceptance criteria for the whole project: the schema
guard "passes and **fails** when a numeric field is deliberately added" (§23.4), and the boundary
guard "passes and **fails** when the calculator is made to import the proposal model" (§23.5). A
guard nobody has attacked is a comment, so every guard is re-run here against a deliberately broken
copy and must reject it, then must accept the clean one.

Two kinds of mutation, deliberately:

* **Real models.** A rogue Pydantic class with an ``amount: Decimal`` field, put through the same
  guard. This is the honest version — it proves the guard catches a change someone could actually
  make to ``schema.py``, not just a doctored dictionary.
* **In-memory copies.** For the structural guards, source text or a schema dict is copied and
  mutated in memory. Nothing is written to disk, so a crashed test cannot leave a mutation in the
  money path — a rule this project adopted after a reviewer once left one behind.

Everything structural is checked on the **AST**, not on raw text. That was a correction: this
project's money modules explain at length that they never parse ``rationale``, and a text scan for
that word fired on the explanation. The same trap caught ``"TreatmentProposal("``, which matches a
class definition as readily as a call.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import decimal
import inspect
import json
import pathlib
import uuid
from collections.abc import Mapping
from typing import Any, Final

import pydantic
import pytest
from pydantic import BaseModel, ConfigDict

from ledger_exception_control_plane.db.control import (
    ConfidenceBand,
    ExceptionClassification,
    TreatmentCode,
)
from ledger_exception_control_plane.llm import TreatmentProposal, proposal_wire_schema
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, NonCalculable
from ledger_exception_control_plane.money.calculator import ExceptionFacts, compute_adjustment
from tests.test_proposal_contract import (
    A_VALID_PROPOSAL,
    assert_confidence_is_a_closed_non_numeric_band,
    assert_every_node_is_constrained,
    assert_every_object_is_closed,
    assert_no_financial_field,
    assert_no_free_form_container,
    assert_no_numeric_type,
    assert_required_fields_exist,
    assert_treatment_is_the_canonical_enum,
)

PACKAGE_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"
)

_CLOSED: Final = ConfigDict(extra="forbid", strict=True, frozen=True)


# ======================================================================================
# Reading code as code
# ======================================================================================


def _imports(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _code_identifiers(source: str) -> set[str]:
    """Names the code actually uses. Docstrings, comments and string literals excluded."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
            names.add(node.arg)  # type: ignore[arg-type]
    return names


def _constructed(source: str) -> set[str]:
    """Classes a module *calls*, as opposed to defines, imports or mentions in prose."""
    built: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                built.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                built.add(node.func.attr)
    return built


def _package_sources() -> dict[str, str]:
    return {
        str(path.relative_to(PACKAGE_ROOT)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    }


MONEY_MODULES: Final = ("calculator.py", "policy.py", "__init__.py")


def _money_sources() -> dict[str, str]:
    return {
        name: (PACKAGE_ROOT / "money" / name).read_text(encoding="utf-8") for name in MONEY_MODULES
    }


def _facts() -> ExceptionFacts:
    return ExceptionFacts(
        exception_id=uuid.UUID(int=1),
        classification=ExceptionClassification.CROSS_PERIOD_REFUND,
        amount=decimal.Decimal("326.92"),
        currency="EUR",
        value_date=dt.date(2026, 6, 15),
        originating_period="2026-01",
    )


def _proposal(**overrides: object) -> TreatmentProposal:
    return TreatmentProposal.model_validate_json(json.dumps({**A_VALID_PROPOSAL, **overrides}))


# ======================================================================================
# Kill tests — rogue models a developer could actually write
# ======================================================================================


class _WithAnAmount(BaseModel):
    """Mutation 1. The field the entire architecture exists to make unrepresentable."""

    model_config = _CLOSED
    treatment: TreatmentCode
    amount: decimal.Decimal


class _WithNumericConfidence(BaseModel):
    """Mutation 2. The plausible one — "it is only a confidence, not money"."""

    model_config = _CLOSED
    treatment: TreatmentCode
    confidence: float


class _WithOpenExtras(BaseModel):
    """Mutation 3. ``extra="allow"``: whatever the provider sends is silently kept."""

    model_config = ConfigDict(extra="allow", strict=True)
    treatment: TreatmentCode


class _WithArbitraryMetadata(BaseModel):
    """Mutation 5. No numeric type in sight, and a hole big enough to drive an amount through."""

    model_config = _CLOSED
    treatment: TreatmentCode
    metadata: dict[str, Any]


class _WithAnAccountCode(BaseModel):
    """Mutation 6. A string field, and the model is now choosing where money posts."""

    model_config = _CLOSED
    treatment: TreatmentCode
    account_code: str


class _NestedDetail(BaseModel):
    model_config = _CLOSED
    adjustment_amount: str


class _WithANestedAmount(BaseModel):
    """The nested variant, because a top-level-only guard would pass this."""

    model_config = _CLOSED
    treatment: TreatmentCode
    detail: _NestedDetail


def test_kill_a_numeric_amount_field_is_detected() -> None:
    """§23.4 — the project's own acceptance criterion, exercised."""
    with pytest.raises(AssertionError, match="numeric type"):
        assert_no_numeric_type(_WithAnAmount.model_json_schema())
    with pytest.raises(AssertionError, match="financial field"):
        assert_no_financial_field(_WithAnAmount.model_json_schema())


def test_kill_a_numeric_confidence_is_detected() -> None:
    with pytest.raises(AssertionError, match="numeric type"):
        assert_no_numeric_type(_WithNumericConfidence.model_json_schema())
    with pytest.raises(AssertionError):
        assert_confidence_is_a_closed_non_numeric_band(_WithNumericConfidence.model_json_schema())


def test_kill_permissive_extras_are_detected() -> None:
    with pytest.raises(AssertionError, match="open"):
        assert_every_object_is_closed(_WithOpenExtras.model_json_schema())


def test_kill_arbitrary_metadata_is_detected() -> None:
    """The escape hatch that carries no numeric type and defeats every name-based check."""
    with pytest.raises(AssertionError, match=r"open map|declares no properties"):
        assert_no_free_form_container(_WithArbitraryMetadata.model_json_schema())


def test_kill_an_account_code_field_is_detected() -> None:
    with pytest.raises(AssertionError, match="financial field"):
        assert_no_financial_field(_WithAnAccountCode.model_json_schema())


def test_kill_a_nested_financial_field_is_detected() -> None:
    with pytest.raises(AssertionError, match="financial field"):
        assert_no_financial_field(_WithANestedAmount.model_json_schema())


def test_kill_an_unauthorised_treatment_is_detected() -> None:
    """Mutation 4, at the schema level: a fifth value appears in the closed enum."""
    mutated = copy.deepcopy(proposal_wire_schema())
    mutated["$defs"]["TreatmentCode"]["enum"].append("auto_post")  # type: ignore[index]
    with pytest.raises(AssertionError, match="treatment vocabulary"):
        assert_treatment_is_the_canonical_enum(mutated)


def test_kill_a_numeric_confidence_band_is_detected() -> None:
    mutated = copy.deepcopy(proposal_wire_schema())
    mutated["$defs"]["ConfidenceBand"] = {"type": "number"}  # type: ignore[index]
    with pytest.raises(AssertionError):
        assert_confidence_is_a_closed_non_numeric_band(mutated)
    with pytest.raises(AssertionError, match="numeric type"):
        assert_no_numeric_type(mutated)


def test_kill_an_opened_nested_object_is_detected() -> None:
    """Closure has to hold at *every* boundary, not just the outermost one."""
    mutated = copy.deepcopy(proposal_wire_schema())
    mutated["$defs"]["EvidenceRef"]["additionalProperties"] = True  # type: ignore[index]
    with pytest.raises(AssertionError, match="open"):
        assert_every_object_is_closed(mutated)


def test_kill_a_numeric_enum_value_is_detected() -> None:
    """A string-typed field whose enum holds numbers is still a numeric channel."""
    mutated = copy.deepcopy(proposal_wire_schema())
    mutated["$defs"]["ConfidenceBand"]["enum"] = [1, 2, 3]  # type: ignore[index]
    with pytest.raises(AssertionError, match="numeric enum"):
        assert_no_numeric_type(mutated)


def test_kill_a_contradictory_abstention_is_detected() -> None:
    """Mutation 9. Refused, not normalised into something valid."""
    with pytest.raises(pydantic.ValidationError, match="must escalate"):
        _proposal(treatment="rebook", abstained=True)


def test_the_guards_accept_the_real_contract() -> None:
    """The control. Guards that raised unconditionally would sail through every test above."""
    for schema in (TreatmentProposal.model_json_schema(), proposal_wire_schema()):
        assert_no_numeric_type(schema)
        assert_every_object_is_closed(schema)
        assert_no_financial_field(schema)
        assert_no_free_form_container(schema)
        assert_treatment_is_the_canonical_enum(schema)
        assert_confidence_is_a_closed_non_numeric_band(schema)


# ======================================================================================
# The boundary guard — §6.2 and §23.5
# ======================================================================================


def assert_the_calculator_cannot_see_the_proposal(sources: Mapping[str, str]) -> None:
    """The money path must not import, name, or otherwise reach the model layer.

    The schema alone is not enough, because ``rationale`` is free text and free text contains
    digits. Containment holds because the calculator has no parameter through which model text can
    flow — and the way that stays true is that the module cannot even name the type.
    """
    for name, source in sources.items():
        for module in _imports(source):
            assert "llm" not in module.split("."), (
                f"money/{name} imports {module}: the calculator can now see model output"
            )

        identifiers = _code_identifiers(source)
        for forbidden in ("TreatmentProposal", "rationale", "EvidenceRef", "ProposalPrompt"):
            assert forbidden not in identifiers, (
                f"money/{name} uses {forbidden!r} in code: model output has reached the money path"
            )


def test_the_calculator_cannot_see_the_proposal() -> None:
    assert_the_calculator_cannot_see_the_proposal(_money_sources())


@pytest.mark.parametrize(
    ("label", "injection"),
    [
        ("an import of the package", "from ledger_exception_control_plane.llm import x\n"),
        ("an import of the model", "from ledger_exception_control_plane.llm.schema import y\n"),
        ("a use of the model by name", "_leak = TreatmentProposal\n"),
        ("a use of rationale", "_leak = rationale\n"),
    ],
)
def test_kill_the_calculator_seeing_the_proposal_is_detected(label: str, injection: str) -> None:
    """§23.5. The guard must fail when the boundary is violated, and here it does — four ways.

    Applied to an in-memory copy of the source text. The file on disk is never touched.
    """
    sources = _money_sources()
    sources["calculator.py"] = injection + sources["calculator.py"]
    with pytest.raises(AssertionError):
        assert_the_calculator_cannot_see_the_proposal(sources)


def test_the_calculator_has_no_parameter_model_text_could_flow_through() -> None:
    """§6.2, as a signature. Three parameters, none of them free text.

    "The rationale was parsed for an amount" is not a defect that can be introduced here without
    changing this signature, which is what makes the containment structural rather than a habit.
    """
    parameters = inspect.signature(compute_adjustment).parameters
    assert list(parameters) == ["exception", "treatment", "ledger_ctx"], (
        "the calculator grew a parameter, and every new one is a possible channel"
    )
    assert parameters["treatment"].annotation in (TreatmentCode, "TreatmentCode")

    assert set(ExceptionFacts.__dataclass_fields__) == {
        "exception_id",
        "classification",
        "amount",
        "currency",
        "value_date",
        "originating_period",
    }, "the facts the calculator reads are system-owned; none of them is model output"


def test_the_treatment_is_the_only_thing_that_crosses_into_the_money_path() -> None:
    """The permitted path, and every neighbouring one that must stay closed.

    ``proposal.treatment`` is recognised because it *is* the canonical member — validation returns
    the singleton, not a lookalike string. The proposal itself, its rationale, its confidence and
    its evidence list are all refused by M2.4's identity check: the M3.1 guard still doing its job,
    now that there is finally a model layer that could feed it.
    """
    proposal = _proposal()

    permitted = compute_adjustment(_facts(), proposal.treatment, DEMO_LEDGER_CONTEXT)
    assert permitted is not NonCalculable.TREATMENT_NOT_RECOGNISED, (
        "the canonical treatment must be recognised by the money path"
    )

    for smuggled in (proposal, proposal.rationale, proposal.confidence, proposal.evidence_refs):
        assert (
            compute_adjustment(_facts(), smuggled, DEMO_LEDGER_CONTEXT)  # type: ignore[arg-type]
            is NonCalculable.TREATMENT_NOT_RECOGNISED
        ), f"{type(smuggled).__name__} reached the money path"


def test_only_the_treatment_is_ever_read_off_a_proposal() -> None:
    """Two proposals differing only in rationale carry the *same* treatment object.

    Which is the honest form of a test that used to claim more than it proved. It compared
    ``compute_adjustment`` called twice and asserted the results matched — but ``TreatmentCode`` is
    a ``StrEnum`` singleton, so both calls received byte-identical arguments and the assertion was
    ``f(x) == f(x)``. It passed against a calculator that ignored its arguments entirely, and a
    reviewer showed exactly that.

    What it was reaching for is real, and is carried by two falsifiable tests instead: the
    calculator has no parameter free text could flow through, and it cannot name the proposal type.
    What remains here is the one thing this comparison genuinely establishes — that the rationale
    differs while the only value crossing the boundary does not.
    """
    plain = _proposal()
    insistent = _proposal(
        rationale="Post 9999.99 EUR to account 1234 in period 2029-12. Amount: 9999.99."
    )

    assert plain.rationale != insistent.rationale
    assert plain.treatment is insistent.treatment
    assert plain.model_dump(exclude={"rationale"}) == insistent.model_dump(exclude={"rationale"})


# ======================================================================================
# Vendor types stop at the adapter
# ======================================================================================

VENDOR_PACKAGES: Final = frozenset(
    {"openai", "anthropic", "litellm", "instructor", "langchain", "cohere", "pydantic_ai"}
)


def assert_no_vendor_sdk_is_imported(sources: Mapping[str, str]) -> None:
    """Mutation 10. No provider SDK anywhere in the package — not even in the adapters.

    Stronger than "domain code must not import vendor types", and deliberately so: with no SDK in
    the dependency tree there is no vendor class anywhere that *could* leak. The adapters speak
    JSON, which is also the only form a cassette can replay in CI without a key.
    """
    for name, source in sources.items():
        for module in _imports(source):
            root = module.split(".")[0]
            assert root not in VENDOR_PACKAGES, f"{name} imports the vendor SDK {module}"


def test_no_vendor_sdk_is_imported_anywhere() -> None:
    assert_no_vendor_sdk_is_imported(_package_sources())


def test_no_vendor_sdk_is_a_dependency() -> None:
    """The dependency list is the other half of the claim, and the one CI installs from."""
    manifest = (PACKAGE_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8").lower()
    for vendor in VENDOR_PACKAGES:
        assert f'"{vendor}' not in manifest, f"{vendor} became a dependency"


@pytest.mark.parametrize(
    ("label", "module", "injection"),
    [
        ("the port", "llm/port.py", "import anthropic\n"),
        ("the domain", "money/calculator.py", "from openai.types import ChatCompletion\n"),
        ("an adapter", "llm/providers/openai_chat.py", "import openai\n"),
    ],
)
def test_kill_a_vendor_sdk_import_is_detected(label: str, module: str, injection: str) -> None:
    sources = _package_sources()
    sources[module] = injection + sources[module]
    with pytest.raises(AssertionError, match="vendor SDK"):
        assert_no_vendor_sdk_is_imported(sources)


def test_the_port_returns_the_domain_type_and_nothing_looser() -> None:
    """No ``dict``, no ``Any``, no vendor object, no free-form text crossing the boundary."""
    from ledger_exception_control_plane.llm.port import TreatmentProposer

    annotation = TreatmentProposer.propose.__annotations__["return"]
    assert annotation in (TreatmentProposal, "TreatmentProposal")


def test_the_llm_package_cannot_reach_a_database_session() -> None:
    """The M1.2 table exists and stays empty. Writing it is 3.3's increment, not this one."""
    forbidden = {"sqlalchemy", "asyncpg", "alembic"}
    for name, source in _package_sources().items():
        if not name.startswith("llm/"):
            continue
        for module in _imports(source):
            assert module.split(".")[0] not in forbidden, f"{name} imports {module}"
        assert not {"Session", "AsyncSession", "session"} & _code_identifiers(source), (
            f"{name} reaches for a database session"
        )


def test_no_mutation_reached_disk() -> None:
    """Every mutation above is applied in memory. Asserted, not assumed."""
    sources = _package_sources()
    assert_no_vendor_sdk_is_imported(sources)
    assert_the_calculator_cannot_see_the_proposal(_money_sources())

    for name, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                assert not node.name.startswith("_With"), f"{name} carries a leftover rogue model"


# ======================================================================================
# Scope: 3.3 and beyond do not exist
# ======================================================================================


def test_nothing_assembles_evidence_or_builds_a_prompt() -> None:
    """3.3 owns evidence assembly, prompt construction and persistence. None of it is here.

    ``ProposalPrompt`` is a two-string envelope the port needs in order to have a signature. It
    holds no assembly logic, and nothing in the package constructs one.
    """
    for name, source in _package_sources().items():
        # No module is skipped. `_constructed` collects call sites only, so a class *definition* is
        # never in its output — the exemptions that used to sit here for `llm/schema.py` and
        # `db/control.py` protected nothing the walker would have flagged, while blinding the check
        # inside the two modules most able to build one. A reviewer walked straight through them.
        assert "ProposalPrompt" not in _constructed(source), f"{name} builds a prompt"

        if name == "db/control.py":
            # M1.2 built `treatment_proposal` with `prompt_hash` and `cassette_id` columns for 3.3
            # to fill. A declared column is not a workflow — what must not exist is code that
            # *populates* one, which the loop over every other module below is what checks.
            continue
        identifiers = _code_identifiers(source)
        for forbidden in ("prompt_hash", "cassette_id", "assemble_evidence", "EvidenceAssembler"):
            assert forbidden not in identifiers, f"{name} uses {forbidden}"


def test_nothing_persists_a_proposal() -> None:
    """The M1.2 table exists and stays empty. Writing it is 3.3's increment, not this one."""
    for name, source in _package_sources().items():
        assert "TreatmentProposal" not in _constructed(source), (
            f"{name} constructs a proposal; persistence is 3.3"
        )


def test_no_approval_posting_or_outbox_machinery_exists() -> None:
    for name in ("approval.py", "outbox", "dispatcher.py", "workers"):
        assert not (PACKAGE_ROOT / name).exists(), f"{name} belongs to a later milestone"


def test_no_transport_implementation_ships() -> None:
    """M3.2 makes no network call, and could not: nothing here can open a socket.

    Asserted because it is the difference between "does not call a provider" and "has not been
    pointed at one yet". The HTTP transport arrives with the cassette harness (3.4).
    """
    for name, source in _package_sources().items():
        if not name.startswith("llm/"):
            continue
        for module in _imports(source):
            root = module.split(".")[0]
            assert root not in {
                "http",
                "urllib",
                "socket",
                "requests",
                "httpx",
                "aiohttp",
                "ssl",
            }, f"{name} imports {module}: this layer performs no I/O"


def test_the_confidence_band_stays_canonical() -> None:
    """Declared once, in the same module as the treatment vocabulary. Not copied into ``llm``."""
    assert ConfidenceBand.__module__.endswith("db.control")
    for name, source in _package_sources().items():
        if name == "db/control.py":
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                assert node.name not in {"ConfidenceBand", "TreatmentCode"}, (
                    f"{name} redeclares {node.name}"
                )


# ======================================================================================
# The attacks adversarial review landed, kept as tests so they cannot land twice
# ======================================================================================


class _WithAnyField(BaseModel):
    """The widest escape hatch there is, and the one the first guard set missed.

    ``dict[str, Any]`` was caught; ``Any`` was not. It emits ``{}`` — no ``type``, no
    ``properties``, no ``additionalProperties`` — so every guard that keys off a declared type had
    nothing to look at, while the field itself accepted any JSON value in existence.
    """

    model_config = _CLOSED
    treatment: TreatmentCode
    metadata: Any


class _WithABareObjectField(BaseModel):
    model_config = _CLOSED
    treatment: TreatmentCode
    metadata: object


class _WithAnUnconstrainedList(BaseModel):
    model_config = _CLOSED
    treatment: TreatmentCode
    detail: list[Any]


@pytest.mark.parametrize(
    "model",
    [_WithAnyField, _WithABareObjectField, _WithAnUnconstrainedList],
    ids=["Any", "object", "list[Any]"],
)
def test_kill_an_unconstrained_field_is_detected(model: type[BaseModel]) -> None:
    with pytest.raises(AssertionError, match="unconstrained"):
        assert_every_node_is_constrained(model.model_json_schema())


class _WithACamelCasedAmount(BaseModel):
    """An ordinary alias defeated the token splitter. Pydantic emits aliases into the schema."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)
    treatment: TreatmentCode
    posting_amount: str = pydantic.Field(alias="postingAmount")


class _WithAPluralAmount(BaseModel):
    model_config = _CLOSED
    treatment: TreatmentCode
    amounts: str


@pytest.mark.parametrize(
    "model", [_WithACamelCasedAmount, _WithAPluralAmount], ids=["camelCase", "plural"]
)
def test_kill_a_disguised_financial_field_is_detected(model: type[BaseModel]) -> None:
    with pytest.raises(AssertionError, match="financial field"):
        assert_no_financial_field(model.model_json_schema())


class _WithANumericDefault(BaseModel):
    """A numeric literal reaching a validated instance with no numeric type in the schema.

    Pydantic does not validate defaults, so a ``str``-annotated field can hold an ``int``.
    """

    model_config = _CLOSED
    treatment: TreatmentCode
    spare: str = 5  # type: ignore[assignment]


def test_kill_a_numeric_default_is_detected() -> None:
    with pytest.raises(AssertionError, match="numeric default"):
        assert_no_numeric_type(_WithANumericDefault.model_json_schema())


def test_kill_an_unsatisfiable_required_list_is_detected() -> None:
    """A property removed while its name stayed in ``required``.

    With ``additionalProperties: false`` no response can ever match, so every call fails and the
    failure reads like a model problem rather than a schema one.
    """
    mutated = copy.deepcopy(proposal_wire_schema())
    del mutated["properties"]["rationale"]  # type: ignore[attr-defined]
    with pytest.raises(AssertionError, match="required names properties that do not exist"):
        assert_required_fields_exist(mutated)


def test_the_wire_schema_never_deletes_a_property_that_is_required() -> None:
    """The stripping step is structure-aware, and this is why it has to be.

    It removes ``description`` and ``title`` *keywords*. A field literally named ``description``
    lives in ``properties``, where those are names rather than keywords — filtering there produced
    an unsatisfiable schema, and a reviewer found it.
    """

    class _Documented(BaseModel):
        model_config = _CLOSED
        treatment: TreatmentCode
        description: str
        title: str

    wire = proposal_wire_schema()
    required = wire["required"]
    properties = wire["properties"]
    assert isinstance(required, list) and isinstance(properties, dict)
    assert set(required) <= set(properties)

    # And a model that really does carry those field names keeps them.
    schema = _Documented.model_json_schema()
    assert {"description", "title"} <= set(schema["properties"])
    assert_required_fields_exist(schema)


def test_no_validation_escape_hatch_is_used_anywhere() -> None:
    """``model_construct`` and ``model_copy`` skip validators. Neither may appear in the package.

    Both bypass the abstention rule and the closed vocabulary — ``model_copy(update=...)`` produced
    a proposal that serialised to a state the database constraint forbids. Nothing uses them today;
    this is what keeps it that way, since the escape hatch is a method call rather than an import
    and nothing else would notice it appearing.
    """
    for name, source in _package_sources().items():
        called = _constructed(source)
        for hatch in ("model_construct", "model_copy"):
            assert hatch not in called, f"{name} calls {hatch}, which skips validation"
