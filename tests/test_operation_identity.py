"""M4.1 — the retry-independent operation identifier.

The plan asks for six things and this module carries five of them: determinism and stability; an
explicit assertion that the derivation contains no attempt counter, timestamp, clock reading, random
value, hostname or process id; a collision test over differing input tuples; an assertion that
mutating any component of the posting instruction changes the identifier; and an assertion that
changing only the approver does not. The sixth — two workers, one residual — needs a real database
and lives in ``test_operations_postgres.py``, together with the end-to-end approver test that this
module can only make structurally.

**Why these tests are worth more than they look.** The defect they exist to catch is silent. An
identifier that varies with the attempt, or with how a `Decimal` happened to be spelled, produces a
system that behaves perfectly until the first retry — at which point provider-side suppression and
reconciliation-by-query both stop working, and the failure surfaces as a duplicate financial
posting. There is no partial version of this to discover later: either the identifier is stable or
every guarantee resting on it is void.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import hashlib
import pathlib
import time
import uuid
from typing import Any, Final

import pytest

from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_QUANTUM,
)
from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.money import (
    DEMO_LEDGER_CONTEXT,
    AdjustmentInstruction,
)
from ledger_exception_control_plane.operations import (
    INSTRUCTION_DOMAIN_TAG,
    OPERATION_DOMAIN_TAG,
    PAYLOAD_COMPONENTS,
    AmountNotStorableError,
    canonical_amount,
    derive_identity,
    instruction_payload_hash,
    operation_id,
)

OPERATIONS_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "ledger_exception_control_plane"
    / "operations"
)

IDENTITY_MODULE: Final = OPERATIONS_ROOT / "identity.py"

EXCEPTION_ID: Final = uuid.UUID("11111111-1111-5111-8111-111111111111")
OTHER_EXCEPTION_ID: Final = uuid.UUID("22222222-2222-5222-8222-222222222222")

#: The exact digests the derivation produces for :func:`_golden_instruction` at version 1.
#:
#: **Literals, deliberately, and this is the most load-bearing constant in the module.** Every other
#: test here asserts a *property* — that two derivations agree, that a mutation changes the answer —
#: and a reviewer showed what that leaves open: changing ``OPERATION_DOMAIN_TAG`` to ``v2``, or
#: altering the framing in ``_labelled``, re-keys every identifier this system will ever produce and
#: leaves all 68 tests green. Property tests cannot catch it, because both sides of every comparison
#: move together.
#:
#: An ``operation_id`` is persisted, is the value ``uq_adjustment_operation_id`` enforces, and is
#: what a provider will eventually be asked to deduplicate on. A silent change to the derivation
#: orphans every stored identifier and re-sends every in-flight operation under a new key. If this
#: assertion ever fails, the correct response is almost never to update the constant.
GOLDEN_PAYLOAD_HASH: Final = "094f2975eb9b4c782bb2f3f2db3cd5a49e3e1a32f52bbf68a78aec149e4c9a97"
GOLDEN_OPERATION_ID: Final = "ecdbb17b884e5f5ca6a87987a797be1e3a9c54b7b330d995a82be81d0594d730"
GOLDEN_OPERATION_ID_V2: Final = "42ac3fe30cc5f5c58dbc25efc0e0fd7b8d8029d655cd851f05717c1e9da05873"


def _golden_instruction() -> AdjustmentInstruction:
    """The fixed instruction the golden vectors above are computed from.

    Every value is a literal and none is derived from a constant the production code also reads —
    which is the point. ``account_code`` and ``rounding`` are spelled out rather than taken from the
    money policy, so a change there cannot move both the expectation and the result together.
    """
    return AdjustmentInstruction(
        exception_id=EXCEPTION_ID,
        treatment=TreatmentCode.REBOOK,
        amount=decimal.Decimal("2799.97"),
        currency="EUR",
        account_code="4100",
        period="2026-06",
        quantum=decimal.Decimal("0.0001"),
        rounding="ROUND_HALF_UP",
        ledger_context_version="demo-2026-06",
    )


def _instruction(**overrides: Any) -> AdjustmentInstruction:
    """A realistic priced instruction, with any field replaced."""
    base = AdjustmentInstruction(
        exception_id=EXCEPTION_ID,
        treatment=TreatmentCode.REBOOK,
        amount=decimal.Decimal("2799.97"),
        currency="EUR",
        account_code="4000-REVENUE",
        period="2026-06",
        quantum=MONEY_QUANTUM,
        rounding=decimal.ROUND_HALF_EVEN,
        ledger_context_version=DEMO_LEDGER_CONTEXT.version,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


# ======================================================================================
# The shape the database will accept
# ======================================================================================


def test_both_digests_are_lower_case_sha256_hex() -> None:
    """``adjustment`` refuses anything else, by check constraint, on both columns."""
    identity = derive_identity(_instruction(), exception_id=EXCEPTION_ID, resolution_version=1)

    for digest in (identity.operation_id, identity.instruction_payload_hash):
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


def test_the_digest_is_the_full_sha256_never_truncated() -> None:
    """§12.1 says full digest. A truncated key is a collision surface with a shorter fuse."""
    payload = instruction_payload_hash(_instruction())
    expected = hashlib.sha256(
        OPERATION_DOMAIN_TAG
        + b"".join(
            len(part).to_bytes(8, "big") + part
            for part in (str(EXCEPTION_ID).encode(), b"1", payload.encode())
        )
    ).hexdigest()

    assert (
        operation_id(
            exception_id=EXCEPTION_ID, resolution_version=1, instruction_payload_hash=payload
        )
        == expected
    )


def test_the_two_digests_are_domain_separated() -> None:
    """Both are 64 hex characters and both satisfy the same column check.

    Without distinct tags a payload digest stored in the ``operation_id`` column would be
    indistinguishable from a real identifier — storable, wrong, and undetectable afterwards.
    """
    # Compared as plain bytes: mypy narrows the constants to literals and would otherwise
    # call the comparison non-overlapping, which is true only because they differ today.
    assert bytes(OPERATION_DOMAIN_TAG) != bytes(INSTRUCTION_DOMAIN_TAG)

    # Hashed over the *same* body, which is the only way to test the tags rather than the bodies.
    # Comparing a real operation id with a real payload digest was the first version, and it can
    # only fail on a SHA-256 collision — it stayed green with both tags set to the same value.
    body = b"identical bytes under two domains"
    assert (
        hashlib.sha256(OPERATION_DOMAIN_TAG + body).hexdigest()
        != hashlib.sha256(INSTRUCTION_DOMAIN_TAG + body).hexdigest()
    )


# ======================================================================================
# The golden vectors — the only tests here that pin a value rather than a property
# ======================================================================================


def test_the_derivation_produces_its_pinned_digests() -> None:
    """**Change the derivation and this fails. Nothing else in the suite does.**

    A reviewer ran three production mutants against the previous version of this module — a bumped
    domain tag, an altered framing, a changed component order — and all 68 tests passed every time.
    They had to: every assertion compared two derivations, and both sides moved together.
    """
    identity = derive_identity(
        _golden_instruction(), exception_id=EXCEPTION_ID, resolution_version=1
    )

    assert identity.instruction_payload_hash == GOLDEN_PAYLOAD_HASH
    assert identity.operation_id == GOLDEN_OPERATION_ID


def test_the_pinned_digests_are_reached_through_the_public_entry_points_too() -> None:
    """The two halves separately, so a change to either is attributable rather than merely
    visible."""
    payload = instruction_payload_hash(_golden_instruction())
    assert payload == GOLDEN_PAYLOAD_HASH
    assert (
        operation_id(
            exception_id=EXCEPTION_ID, resolution_version=1, instruction_payload_hash=payload
        )
        == GOLDEN_OPERATION_ID
    )


def test_the_pinned_identifier_moves_with_the_resolution_version() -> None:
    """A second vector, so the version is pinned as a *component* and not merely as an input."""
    assert (
        operation_id(
            exception_id=EXCEPTION_ID,
            resolution_version=2,
            instruction_payload_hash=GOLDEN_PAYLOAD_HASH,
        )
        == GOLDEN_OPERATION_ID_V2
    )
    # Compared as plain strings: mypy narrows both constants to literals and would otherwise
    # call this non-overlapping, which is true only because they differ today.
    assert str(GOLDEN_OPERATION_ID_V2) != str(GOLDEN_OPERATION_ID)


# ======================================================================================
# Determinism and stability — the plan's second obligation
# ======================================================================================


def test_the_identifier_is_identical_across_repeated_derivations() -> None:
    """Attempt one and attempt five of the same approved resolution produce one value."""
    derivations = {
        derive_identity(
            _instruction(), exception_id=EXCEPTION_ID, resolution_version=3
        ).operation_id
        for _ in range(5)
    }
    assert len(derivations) == 1


def test_the_identifier_is_stable_across_equal_but_differently_spelled_instructions() -> None:
    """Two instructions that compare equal must identify identically.

    Not a tautology, and the first draft of this test got the reason backwards. ``Decimal`` compares
    by value, so the two instructions below are ``==`` — but they are distinct objects holding
    distinct digit tuples, and ``str`` renders them differently. An identifier built from ``str``,
    ``repr`` or object identity would pass every other test in this module and fail this one, which
    is exactly the failure mode: two attempts at one posting acquiring two keys because a value was
    reparsed somewhere along the way.
    """
    first = _instruction(amount=decimal.Decimal("2799.97"))
    second = _instruction(amount=decimal.Decimal("2799.9700"))

    assert first == second, "the same economic instruction"
    assert first.amount.as_tuple() != second.amount.as_tuple(), "spelled differently"
    assert str(first.amount) != str(second.amount), "and rendered differently by str"

    assert instruction_payload_hash(first) == instruction_payload_hash(second)
    assert derive_identity(
        first, exception_id=EXCEPTION_ID, resolution_version=1
    ) == derive_identity(second, exception_id=EXCEPTION_ID, resolution_version=1)


def test_the_identifier_does_not_depend_on_the_decimal_context() -> None:
    """A caller elsewhere in the process must not be able to change what an identifier is.

    ``quantize`` and ``scaleb`` are context operations, and this repository has already been bitten
    once by using one: an amount with 29 decimal places scaled to something integral under the
    default 28-digit precision and was accepted. The derivation reads digits instead, so a
    deliberately hostile precision changes nothing.
    """
    reference = derive_identity(
        _instruction(), exception_id=EXCEPTION_ID, resolution_version=1
    ).operation_id

    with decimal.localcontext() as context:
        context.prec = 1
        context.rounding = decimal.ROUND_UP
        under_hostile_context = derive_identity(
            _instruction(), exception_id=EXCEPTION_ID, resolution_version=1
        ).operation_id

    assert under_hostile_context == reference


# ======================================================================================
# The canonical amount — where representation-dependence would enter
# ======================================================================================


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("2799.97", "2799.9700"),
        ("2799.9700", "2799.9700"),
        ("2799.970000", "2799.9700"),
        ("0", "0.0000"),
        ("0.0000", "0.0000"),
        ("-0", "0.0000"),
        ("-0.0000", "0.0000"),
        ("-12.5", "-12.5000"),
        ("1E+2", "100.0000"),
        ("1.0E+3", "1000.0000"),
        ("0.0001", "0.0001"),
        ("-0.0001", "-0.0001"),
    ],
)
def test_one_economic_value_has_one_canonical_spelling(spelling: str, expected: str) -> None:
    """Every way of writing a value collapses to one string, including exponent forms.

    Negative zero is the case worth naming: ``Decimal("-0.0000")`` is a real value a subtraction
    produces, it compares equal to zero, and a sign carried into the digest would give one economic
    outcome two identifiers.
    """
    assert canonical_amount(decimal.Decimal(spelling)) == expected


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("more precision than the column stores", "1.23456"),
        ("far more precision", "0." + "0" * 28 + "1"),
        ("not a number", "NaN"),
        ("infinite", "Infinity"),
        ("negative infinite", "-Infinity"),
        ("at the magnitude bound", str(MONEY_MAGNITUDE_EXCLUSIVE_BOUND)),
        ("beyond the magnitude bound", str(MONEY_MAGNITUDE_EXCLUSIVE_BOUND + 1)),
    ],
)
def test_an_amount_the_column_would_refuse_never_acquires_an_identifier(
    label: str, value: str
) -> None:
    """Refused, not rounded.

    An identifier for an unstorable posting is worse than no identifier: it names an operation that
    can never be recorded, and rounding here would be the database-rounds-on-the-way-in failure M1.1
    removed, relocated one layer up.
    """
    with pytest.raises(AmountNotStorableError):
        canonical_amount(decimal.Decimal(value))


@pytest.mark.parametrize(
    "spelling", ["0E+400000", "0E-400000", "-0E+400000", "0E+5000000", "0E-5000000"]
)
def test_a_zero_at_an_extreme_exponent_is_canonicalised_immediately(spelling: str) -> None:
    """**A regression test for unbounded work on a ten-character input.**

    ``Decimal`` admits an arbitrary exponent, and every spelling of zero passes both the scale and
    the magnitude check — so the shift below them would materialise ``10**400004``. A reviewer
    measured 42 ms for ``0E+400000`` and 3.5 s for ``0E-5000000``, growing without limit. Zero is
    the only value that can reach a large shift, so short-circuiting it bounds the whole function.

    Timed rather than merely asserted, because the defect was never a wrong answer: the old code
    returned ``0.0000`` too, eventually.
    """
    started = time.perf_counter()
    assert canonical_amount(decimal.Decimal(spelling)) == "0.0000"
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05, f"canonicalising {spelling} took {elapsed:.3f}s; the shift is unbounded"


@pytest.mark.parametrize("quantum", ["0.0001", "0.000001", "1E-12", "0.01"])
def test_a_finer_quantum_than_the_money_column_stores_is_still_hashable(quantum: str) -> None:
    """**A regression test for judging the quantisation by the amount's rules.**

    ``quantum`` is a ``Decimal`` and is not an amount. The first encoder dispatched on type, so a
    declared quantisation finer than four places was refused with a message about the ``adjustment``
    column — which it has nothing to do with. Nothing failed at the time because ``MONEY_QUANTUM``
    happens to be ``0.0001``; the coupling was found by reading, before it could bite.
    """
    digest = instruction_payload_hash(_instruction(quantum=decimal.Decimal(quantum)))
    assert len(digest) == 64


def test_two_spellings_of_one_quantum_hash_identically() -> None:
    """The same canonicalisation obligation the amount has, for the field beside it."""
    assert instruction_payload_hash(
        _instruction(quantum=decimal.Decimal("0.0001"))
    ) == instruction_payload_hash(_instruction(quantum=decimal.Decimal("1E-4")))


def test_the_magnitude_bound_is_exclusive_on_the_permitted_side() -> None:
    """The control for the two bound cases above — one below the bound must still work."""
    assert canonical_amount(decimal.Decimal(MONEY_MAGNITUDE_EXCLUSIVE_BOUND - 1)).endswith(".0000")


# ======================================================================================
# Instruction binding — the plan's fifth obligation
# ======================================================================================


def test_every_field_of_the_instruction_is_hashed() -> None:
    """**The guard that survives a future field being added.**

    The plan requires that mutating *any* component of the posting instruction changes the
    identifier. A hard-coded list of components satisfies that today and quietly stops satisfying it
    the moment someone adds a field — the new one would be unbound, and two genuinely different
    postings would share a key. Comparing the hashed set against the dataclass itself is what makes
    the obligation hold for fields nobody has written yet.
    """
    declared = {field.name for field in dataclasses.fields(AdjustmentInstruction)}
    assert set(PAYLOAD_COMPONENTS) == declared
    assert len(PAYLOAD_COMPONENTS) == len(declared), "the component list repeats a field"


#: One altered value per instruction field. Declared here rather than inline so the coverage
#: control below reads the same list the sweep runs, instead of a second list that can drift.
FIELD_MUTATIONS: Final = (
    ("exception_id", OTHER_EXCEPTION_ID),
    ("treatment", TreatmentCode.WRITE_OFF),
    ("amount", decimal.Decimal("2799.98")),
    ("currency", "USD"),
    ("account_code", "4900-WRITE-OFFS"),
    ("period", "2026-07"),
    ("quantum", decimal.Decimal("0.01")),
    ("rounding", decimal.ROUND_DOWN),
    ("ledger_context_version", "demo-2026-07"),
)


@pytest.mark.parametrize(
    ("field", "value"), FIELD_MUTATIONS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_mutating_any_component_of_the_instruction_changes_the_identifier(
    field: str, value: object
) -> None:
    """§12.1's central rule: if the financial effect could differ, the identifier must.

    If account mapping or period configuration changes between a first attempt and a re-send, the
    instruction is genuinely different and must produce a different identifier — otherwise, under
    ``ENFORCES_KEY``, the provider suppresses the second posting while this system records
    ``CONFIRMED`` for something that was never applied.
    """
    baseline = _instruction()
    mutated = _instruction(**{field: value})
    assert mutated != baseline, "the parametrised value must actually differ"

    assert instruction_payload_hash(mutated) != instruction_payload_hash(baseline)


def test_the_mutation_sweep_covers_every_field() -> None:
    """The control for the sweep above. A field dropped from it would go untested silently.

    Both this and :func:`test_every_field_of_the_instruction_is_hashed` compare against
    ``dataclasses.fields``, which is what makes a newly added field fail twice: once because it is
    not hashed, and once because nothing tries to mutate it.
    """
    swept = {field for field, _ in FIELD_MUTATIONS}
    assert swept == {field.name for field in dataclasses.fields(AdjustmentInstruction)}
    assert len(FIELD_MUTATIONS) == len(swept), "a field is mutated twice"


def test_a_field_type_without_a_canonical_encoding_is_refused() -> None:
    """No ``str(value)`` fallback, deliberately.

    A field type the encoder does not recognise must fail at the moment it is added, rather than
    acquire whatever ``str`` happens to produce — which for a container or a float would be
    representation-dependent and would destabilise every identifier derived afterwards.
    """
    hostile = dataclasses.replace(_instruction(), period=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="no canonical encoding"):
        instruction_payload_hash(hostile)


# ======================================================================================
# Collision resistance — the plan's fourth obligation
# ======================================================================================


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"currency": "EU", "account_code": "R4000"}, {"currency": "EUR", "account_code": "4000"}),
        (
            {"account_code": "4000-", "period": "2026-06"},
            {"account_code": "4000", "period": "-2026-06"},
        ),
        (
            {"account_code": "4000-REVENUE2026", "period": "-06"},
            {"account_code": "4000-REVENUE", "period": "2026-06"},
        ),
    ],
)
def test_components_that_would_collide_under_plain_concatenation_do_not(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    """§12.1 bans unprefixed concatenation by name, and this is why.

    Each pair joins to the identical byte string without a length prefix — ``"EU" + "R4000"`` and
    ``"EUR" + "4000"`` are both ``EUR4000``. Two different postings sharing one identifier is the
    precise failure the identifier exists to prevent, so a collision here is not a hash weakness but
    an encoding one, and no amount of SHA-256 fixes it.

    Only *adjacent* components can be ambiguous under concatenation, which is why every pair moves a
    boundary between neighbours in :data:`PAYLOAD_COMPONENTS`. A first draft used two fields that
    are not adjacent and was not testing the property at all — the assertion below is what caught
    it, and it stays as the control.
    """
    left, right = _instruction(**first), _instruction(**second)
    assert left != right

    joined_left = "".join(str(getattr(left, name)) for name in PAYLOAD_COMPONENTS)
    joined_right = "".join(str(getattr(right, name)) for name in PAYLOAD_COMPONENTS)
    assert joined_left == joined_right, "the pair must actually be concatenation-ambiguous"

    assert instruction_payload_hash(left) != instruction_payload_hash(right)


def test_differing_resolution_versions_never_share_an_identifier() -> None:
    """A corrected resolution is a *different* operation, never a silent overwrite."""
    payload = instruction_payload_hash(_instruction())
    identifiers = {
        operation_id(
            exception_id=EXCEPTION_ID, resolution_version=version, instruction_payload_hash=payload
        )
        for version in (1, 2, 3, 11, 12)
    }
    assert len(identifiers) == 5


def test_differing_exceptions_never_share_an_identifier() -> None:
    """Two residuals priced identically are still two operations."""
    payload = instruction_payload_hash(_instruction())
    first = operation_id(
        exception_id=EXCEPTION_ID, resolution_version=1, instruction_payload_hash=payload
    )
    second = operation_id(
        exception_id=OTHER_EXCEPTION_ID, resolution_version=1, instruction_payload_hash=payload
    )
    assert first != second


def test_a_large_sweep_of_input_tuples_produces_no_collision() -> None:
    """Breadth, over the three top-level components at once.

    Not a proof — no test collides SHA-256 — but it is what would catch an encoding that dropped a
    component, reused one, or folded two together.
    """
    identifiers = {
        operation_id(
            exception_id=uuid.UUID(int=index),
            resolution_version=version,
            instruction_payload_hash=instruction_payload_hash(
                _instruction(amount=decimal.Decimal(index) / 100)
            ),
        )
        for index in range(60)
        for version in (1, 2)
    }
    assert len(identifiers) == 120


# ======================================================================================
# Retry independence — the plan's third obligation
# ======================================================================================

#: Every source of non-determinism §12.1 bans by name, plus the modules that carry them.
BANNED_CALLS: Final = frozenset(
    {
        "time",
        "monotonic",
        "perf_counter",
        "now",
        "today",
        "utcnow",
        "random",
        "randint",
        "choice",
        "token_bytes",
        "token_hex",
        "urandom",
        "uuid1",
        "uuid4",
        "getpid",
        "gethostname",
        "getfqdn",
        "gethostbyname",
        "node",
    }
)

BANNED_IMPORTS: Final = frozenset(
    {"random", "secrets", "socket", "time", "datetime", "os", "platform", "getpass"}
)

#: Names that would mean an attempt counter had reached the derivation.
BANNED_IDENTIFIERS: Final = frozenset(
    {"attempt", "attempt_no", "attempts", "retry", "retries", "sent_at", "occurred_at"}
)


def _identity_tree() -> ast.Module:
    return ast.parse(IDENTITY_MODULE.read_text(encoding="utf-8"))


def _operations_trees() -> dict[str, ast.Module]:
    """Every module that participates in producing an identifier, not just the one that hashes.

    A reviewer pointed out the gap: ``service.py`` supplies two of the identifier's three
    components, so ``resolution_version=approval.resolution_version + int(time.time()) % 2`` made
    the identifier retry-dependent while the fence — which parsed ``identity.py`` alone — stayed
    green through the entire default suite.
    """
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(OPERATIONS_ROOT.glob("*.py"))
    }


def assert_derivation_reads_no_variable_input(tree: ast.Module, *, minimum: int = 100) -> int:
    """The derivation names no clock, counter, host, process or random source.

    Walked as syntax rather than grepped, following ADR-032: a grep matches prose in a docstring —
    and this module's docstring names every banned input in order to explain the ban.
    """
    inspected = 0
    for node in ast.walk(tree):
        inspected += 1
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in BANNED_IMPORTS, f"imports {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in BANNED_IMPORTS, f"imports {node.module}"
        elif isinstance(node, ast.Call):
            called = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            assert called not in BANNED_CALLS, f"calls {called}()"
        elif isinstance(node, ast.Name):
            assert node.id.lower() not in BANNED_IDENTIFIERS, f"names {node.id}"
        elif isinstance(node, ast.arg):
            assert node.arg.lower() not in BANNED_IDENTIFIERS, f"takes a parameter {node.arg}"

    assert inspected > minimum, "the walk inspected almost nothing; it is not seeing the module"
    return inspected


def test_the_derivation_contains_no_attempt_counter_clock_random_host_or_process_id() -> None:
    """The plan's third obligation, and the one every other guarantee rests on."""
    assert_derivation_reads_no_variable_input(_identity_tree())


def test_no_module_that_feeds_the_derivation_reads_a_variable_input() -> None:
    """The same fence over the whole package, because the hash is not the whole derivation.

    ``identity.py`` turns three components into a digest; ``service.py`` decides what two of those
    components are. A clock reading in either makes the identifier retry-dependent, and only one of
    them was being watched.
    """
    trees = _operations_trees()
    assert set(trees) >= {"identity.py", "service.py", "claim.py"}, trees.keys()

    # The per-module floor is nominal — `__init__.py` is a re-export list and legitimately small —
    # so the coverage claim is made on the total instead, which is what a filtered or empty module
    # list would collapse.
    inspected = 0
    for name, tree in trees.items():
        try:
            inspected += assert_derivation_reads_no_variable_input(tree, minimum=0)
        except AssertionError as exc:  # pragma: no cover - only on a real violation
            raise AssertionError(f"{name}: {exc}") from exc

    assert inspected > 800, "the package walk is not seeing the operations modules"


@pytest.mark.parametrize(
    ("label", "injection"),
    [
        ("a clock reading", "import time\nSTAMP = time.time()\n"),
        ("a datetime import", "import datetime\n"),
        ("a random value", "import random\nNONCE = random.random()\n"),
        ("a fresh uuid", "NONCE = uuid.uuid4()\n"),
        ("a process id", "import os\nPID = os.getpid()\n"),
        ("a hostname", "import socket\nHOST = socket.gethostname()\n"),
        (
            "an attempt counter parameter",
            "def _derive(attempt_no: int) -> int:\n    return attempt_no\n",
        ),
        ("an attempt counter reference", "def _derive() -> int:\n    return attempt\n"),
    ],
)
def test_kill_a_variable_input_in_the_derivation_is_detected(label: str, injection: str) -> None:
    """**Six mutations, one per banned input.** A guard nobody has seen fail is a comment.

    Each injection is the smallest thing that would actually break retry-independence, and each must
    turn the guard red.
    """
    mutated = ast.parse(IDENTITY_MODULE.read_text(encoding="utf-8") + "\n" + injection)
    with pytest.raises(AssertionError):
        assert_derivation_reads_no_variable_input(mutated)


def test_kill_a_derivation_guard_that_walks_nothing_is_detected() -> None:
    """An empty tree satisfies every assertion above for the worst possible reason."""
    with pytest.raises(AssertionError, match="not seeing the module"):
        assert_derivation_reads_no_variable_input(ast.parse(""))


# ======================================================================================
# The approver is not an input — the plan's sixth obligation, structurally
# ======================================================================================


def test_no_derivation_function_accepts_an_approver() -> None:
    """§12.1 excludes the approver, and the strongest local proof is that it cannot be passed.

    §16 permits the approver to differ for the same economic event, so an identifier varying with
    them would vary with a non-financial input — the mirror image of retry-dependence, failing just
    as silently. The end-to-end version of this test lives in ``test_operations_postgres.py``, where
    a real approval's principal is changed and the identifier is shown not to move.
    """
    forbidden = {"approver", "approver_id", "principal", "approving_principal", "approved_by"}

    for node in ast.walk(_identity_tree()):
        if isinstance(node, ast.arg):
            assert node.arg.lower() not in forbidden, f"the derivation takes {node.arg}"
        if isinstance(node, ast.Name):
            assert node.id.lower() not in forbidden, f"the derivation names {node.id}"


def test_the_identifier_is_a_function_of_exactly_its_three_components() -> None:
    """Exhaustive from the other direction: only three things can move the identifier.

    Its predecessor called ``operation_id`` three times with identical arguments and asserted the
    results matched, which is strictly weaker than the determinism test above and did not touch the
    property the docstring claimed. This varies each component in turn over a small grid and
    asserts the identifier is injective across it — so a component that were ignored, or two that
    were folded together, would collapse the grid and fail.
    """
    payloads = [instruction_payload_hash(_instruction(amount=decimal.Decimal(n))) for n in (1, 2)]
    exceptions = [EXCEPTION_ID, OTHER_EXCEPTION_ID]
    versions = [1, 2, 3]

    grid = {
        (exception, version, payload): operation_id(
            exception_id=exception, resolution_version=version, instruction_payload_hash=payload
        )
        for exception in exceptions
        for version in versions
        for payload in payloads
    }

    assert len(set(grid.values())) == len(grid) == 12, "two input tuples share an identifier"

    # And re-deriving the whole grid reproduces it exactly, so nothing outside the three inputs
    # participated.
    again = {
        key: operation_id(
            exception_id=key[0], resolution_version=key[1], instruction_payload_hash=key[2]
        )
        for key in grid
    }
    assert again == grid


# ======================================================================================
# The StrEnum asymmetry that a database round trip exposes
# ======================================================================================


def test_a_str_enum_compares_and_hashes_equal_to_its_value_but_carries_no_value_attribute() -> None:
    """**The trap that cost this increment a four-minute integration run to find.**

    ``approval.decision`` and ``approval.approved_treatment`` are ``String(16)`` columns with check
    constraints and no type decorator, so a round trip through the database returns a plain ``str``
    however the ORM model is annotated. A ``StrEnum`` compares *and hashes* equal to its own value,
    so every membership test and every ``==`` kept working on the raw string — and ``.value``, the
    one operation that needs a real enum member, raised ``AttributeError`` on a code path only
    reached when an approval was being refused.

    That asymmetry is exactly what the M3.1 closure gate was written for, met again one layer down:
    the things that silently work are what stop you noticing the things that do not.
    ``operations/service.py`` therefore re-types both columns at the boundary rather than trusting
    the annotation, and this pins why.
    """
    from ledger_exception_control_plane.db.control import ApprovalDecision

    stored = "approved"  # what the driver actually hands back

    assert stored == ApprovalDecision.APPROVED, "equality is why the bug hid"
    assert stored in frozenset({ApprovalDecision.APPROVED}), "and so is hashing"
    assert not hasattr(stored, "value"), "while this is what actually broke"

    assert ApprovalDecision(stored) is ApprovalDecision.APPROVED
    assert ApprovalDecision(stored).value == "approved"


def test_re_typing_a_stored_value_also_rejects_one_outside_the_vocabulary() -> None:
    """The second reason to convert rather than compare: it re-checks the closed set.

    The database's check constraint is the real control. Converting here means a value that somehow
    got past it fails at the boundary rather than flowing on as an unrecognised string.
    """
    from ledger_exception_control_plane.db.control import ApprovalDecision

    with pytest.raises(ValueError):
        ApprovalDecision("rubber-stamped")
    with pytest.raises(ValueError):
        TreatmentCode("post_it_anyway")


# ======================================================================================
# Refusals at the derivation boundary
# ======================================================================================


def test_an_instruction_priced_for_another_exception_is_refused() -> None:
    """**The cross-exception guard no database constraint can provide.**

    ``adjustment`` has no ``exception_id`` column — it reaches the exception only through the
    approval — so nothing downstream would notice exception A's amount being identified, and later
    posted, under exception B's authorisation.
    """
    with pytest.raises(ValueError, match="prices exception"):
        derive_identity(_instruction(), exception_id=OTHER_EXCEPTION_ID, resolution_version=1)


@pytest.mark.parametrize("version", [0, -1, -100])
def test_a_resolution_version_below_one_is_refused(version: int) -> None:
    """``approval.resolution_version >= 1`` is a check constraint; agreeing with it here means an
    identifier can never be derived for a version the database would refuse to store."""
    with pytest.raises(ValueError, match="resolution version"):
        operation_id(
            exception_id=EXCEPTION_ID,
            resolution_version=version,
            instruction_payload_hash="a" * 64,
        )


@pytest.mark.parametrize(
    "payload",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "0x" + "a" * 62],
)
def test_a_payload_hash_that_is_not_a_sha256_digest_is_refused(payload: str) -> None:
    """Upper case is included deliberately: it satisfies "looks like hex" and fails the column's
    check constraint, so accepting it here would defer a refusal to the INSERT."""
    with pytest.raises(ValueError, match="SHA-256"):
        operation_id(
            exception_id=EXCEPTION_ID, resolution_version=1, instruction_payload_hash=payload
        )
