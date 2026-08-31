"""M3.1 — the kill-test gate. Does the treatment vocabulary genuinely close?

The plan puts this increment *before* any model exists and calls it a gate for a reason: if a real
case needed the model to propose an amount, the type-level containment claim would be false and
would have to be dropped rather than softened. So this file tries to break the claim rather than
illustrate it.

Three things are proved.

**The vocabulary is closed.** Four members, no aliases, no ``other``, no free-form path, and nothing
carrying a number. Arbitrary strings are refused — including at runtime, at the one boundary where a
treatment enters the money path, because a ``StrEnum`` member compares and hashes equal to its own
value and a bare string therefore used to get through.

**Every fixture exception is answered inside it.** Over the corpus, at three sizes: each exception
is priced by some treatment or refused by all of them for an enumerated reason, which makes
``escalate`` the answer — and no priced amount was contributed by a treatment, every one being the
settlement movement's own. The counts are pinned to what the pipeline actually produces, because
the first version of this assertion was a tautology that passed against a fabricated corpus.

**The guards fire.** Each structural guard is re-run against a deliberately mutated copy of what it
inspects, and must fail; then against a clean copy, and must pass. Mutations are made to in-memory
copies — a parsed AST, a throwaway enum — so a crashed test cannot leave one in the money path.

A note on what a gate is worth. Two rounds of adversarial review broke things this file was written
to protect: the exit criterion could not fail, one structural guard stopped looking one directory
short of a real caller, two live assertions had no mutation exercising them, and the runtime
membership check was weak enough that an instance of the class which is not a member got priced into
the wrong period. All of that is fixed here, and the fixes are themselves shown failing. A guard
nobody has attacked is a comment.
"""

from __future__ import annotations

import ast
import collections
import copy
import dataclasses
import datetime as dt
import decimal
import enum
import pathlib
import pickle
import re
import uuid
from collections.abc import Callable
from typing import Final

import pytest

from ledger_exception_control_plane.classification import (
    SettlementMovement,
    accounting_period,
    classify,
    movement_type,
)
from ledger_exception_control_plane.db.control import ExceptionClassification, TreatmentCode
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    match,
)
from ledger_exception_control_plane.money import (
    DEMO_ACCOUNT_POLICY,
    DEMO_LEDGER_CONTEXT,
    AdjustmentInstruction,
    ExceptionFacts,
    LedgerContext,
    NonCalculable,
    account_policy,
    compute_adjustment,
)

PACKAGE_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"
)
CONTROL_SOURCE: Final = PACKAGE_ROOT / "db" / "control.py"

#: The authoritative vocabulary, written out rather than derived from the enum.
#:
#: Deriving it would make the test agree with whatever the code says, which is the one thing it must
#: not do. This comes from ``PROJECT_SPEC.md`` §6.1 and ``IMPLEMENTATION_PLAN.md`` §3.1, both of
#: which name exactly ``REBOOK | ACCRUE | WRITE_OFF | ESCALATE``.
AUTHORITATIVE: Final = frozenset({"rebook", "accrue", "write_off", "escalate"})

#: The money modules that actually branch on a treatment. Re-export and namespace modules do not,
#: and requiring them to mention the type would be noise rather than a guarantee.
#: Where the values are declared, and the one module the literal scan below exempts.
CANONICAL_MODULE: Final = "db/control.py"

TREATMENT_AWARE: Final = ("money/calculator.py", "money/policy.py")

SEED: Final = 20260829

#: Books open far enough back that nothing in the corpus refuses on a closed period — that path is
#: exercised in ``test_money.py`` and would only obscure the question here.
OPEN_BOOKS: Final = dataclasses.replace(DEMO_LEDGER_CONTEXT, earliest_open_period="2026-01")


# ======================================================================================
# The vocabulary is exactly the authoritative set
# ======================================================================================


def assert_vocabulary_is_closed(vocabulary: type[enum.StrEnum]) -> None:
    """The closure assertion, taking the enum as an argument so a mutation can be run through it.

    A guard that could only ever inspect the real enum could never be shown to work. This one is
    handed a rogue vocabulary in the kill test below and must reject it.
    """
    values = {member.value for member in vocabulary}
    assert values == set(AUTHORITATIVE), (
        f"the treatment vocabulary is {sorted(values)}, "
        f"and the specification says {sorted(AUTHORITATIVE)}"
    )


def assert_no_numeric_payload(vocabulary: type[enum.StrEnum]) -> None:
    """No member may carry a number, in its name or its value.

    A treatment is a categorical instruction. A member like ``write_off_125_50`` would be an amount
    smuggled through the one channel a model is ever allowed to use, and the containment argument
    would be over.
    """
    for member in vocabulary:
        assert not re.search(r"\d", member.value), f"{member.value} carries a number"
        assert not re.search(r"\d", member.name), f"{member.name} carries a number"


def test_the_vocabulary_is_exactly_the_authoritative_set() -> None:
    assert_vocabulary_is_closed(TreatmentCode)
    assert len(TreatmentCode) == 4


def test_no_treatment_carries_an_amount() -> None:
    assert_no_numeric_payload(TreatmentCode)


def test_there_is_no_generic_catch_all_member() -> None:
    """No ``other``, no ``custom``, no ``manual`` — a bucket is how a closed set stops being one."""
    for member in TreatmentCode:
        assert member.value not in {"other", "custom", "manual", "unknown", "none", "n_a"}


def test_a_treatment_is_a_bare_value_with_no_parameters() -> None:
    """The members carry no payload of any kind, so there is nowhere for a number to ride along."""
    for member in TreatmentCode:
        assert isinstance(member.value, str)
        assert member.value == member.value.strip().lower()
        assert re.fullmatch(r"[a-z_]+", member.value), member.value


@pytest.mark.parametrize("value", sorted(AUTHORITATIVE))
def test_every_valid_treatment_round_trips(value: str) -> None:
    """Text in, member out, same text back — the stability a persisted column needs."""
    member = TreatmentCode(value)
    assert member.value == value
    assert str(member) == value
    assert TreatmentCode(member.value) is member


@pytest.mark.parametrize(
    "value",
    [
        "nonsense",
        "REBOOK",
        "Rebook",
        "ReBoOk",
        " rebook",
        "rebook ",
        "\trebook",
        "rebook\n",
        "re book",
        "rebook,accrue",
        "",
        "other",
        "write_off_125_50",
        "adjust_by_0_7_percent",
        "custom_amount",
    ],
)
def test_an_arbitrary_string_is_not_a_treatment(value: str) -> None:
    """Case variants and whitespace included: nothing is silently normalised into a member.

    Accepting ``REBOOK`` would mean deciding that two spellings denote one action — a judgement
    nobody recorded, and the sort of leniency that makes a closed set porous the moment a model
    starts emitting text.
    """
    with pytest.raises(ValueError):
        TreatmentCode(value)


# ======================================================================================
# One canonical declaration, and the money path uses it
# ======================================================================================


def _production_sources() -> list[tuple[str, ast.Module]]:
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert len(paths) > 20, "the guards must be walking the real package"
    return [
        (p.relative_to(PACKAGE_ROOT).as_posix(), ast.parse(p.read_text(encoding="utf-8")))
        for p in paths
    ]


def _code_string_constants(tree: ast.Module) -> list[str]:
    """String literals that are actually *code*, with docstrings excluded.

    The distinction matters more than usual here: this file's own guards name the values they
    forbid, and ``db/control.py`` documents ``write_off_125_50`` as an example of the escape hatch
    it prevents. A scan that could not tell prose from code would fire on its own documentation.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def assert_one_canonical_declaration(sources: list[tuple[str, ast.Module]]) -> None:
    """Exactly one class in the package declares treatment members.

    Found structurally rather than by name: any class whose string members overlap the vocabulary in
    more than one place *is* a treatment vocabulary, whatever it is called. A second one is how two
    halves of a system come to disagree about what an action means.
    """
    declarations: list[str] = []
    for name, tree in sources:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            values = {
                statement.value.value
                for statement in node.body
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            }
            if len(values & AUTHORITATIVE) > 1:
                declarations.append(f"{name}:{node.name}")

    assert len(declarations) == 1, (
        f"a treatment vocabulary is declared in {len(declarations)} places: {declarations}"
    )
    assert declarations[0] == "db/control.py:TreatmentCode", (
        f"the canonical declaration moved to {declarations[0]}"
    )


def assert_no_module_repeats_a_treatment_value(sources: list[tuple[str, ast.Module]]) -> None:
    """No module in the package spells a treatment value in code. One exemption, and it is checked.

    A hardcoded ``"rebook"`` would work today and drift silently the first time the vocabulary
    changed — and the vocabulary changing is exactly the event this gate exists to make loud.

    The first version of this guard inspected ``money/`` only, and a reviewer showed the drift
    escaping one directory over: ``demo/snapshot.py`` calls the calculator and picks the treatment
    it prices with, so a literal there is the same defect somewhere the guard was not looking.
    Naming the modules that may hold a treatment was the mistake; the package-wide rule needs no
    such list and cannot be outgrown. ``db/control.py`` is the sole exemption, because that is where
    the values are *declared* — and the two SQL literals it also carries have their own test.
    """
    inspected = 0
    for name, tree in sources:
        if name == CANONICAL_MODULE:
            continue
        inspected += 1
        for literal in _code_string_constants(tree):
            assert literal not in AUTHORITATIVE, (
                f"{name} hardcodes the treatment literal {literal!r} instead of using TreatmentCode"
            )
    assert inspected > 1, "the package-wide scan inspected nothing"


def assert_money_path_uses_the_canonical_type(sources: list[tuple[str, ast.Module]]) -> None:
    """The modules that *decide* with a treatment reference the canonical type.

    Absence of a literal is not by itself evidence of use — a module could branch on treatments
    through some other spelling entirely. The two modules that price and map must name the type.
    """
    seen: set[str] = set()
    for name, tree in sources:
        if name not in TREATMENT_AWARE:
            continue
        seen.add(name)
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.name.rsplit(".", 1)[-1] for node in ast.walk(tree) if isinstance(node, ast.alias)
        }
        assert "TreatmentCode" in referenced, (
            f"{name} decides on treatments without the canonical type"
        )
    for required in TREATMENT_AWARE:
        assert required in seen, f"{required} was not inspected"


def test_the_vocabulary_is_declared_exactly_once() -> None:
    assert_one_canonical_declaration(_production_sources())


def test_no_module_repeats_a_treatment_value() -> None:
    assert_no_module_repeats_a_treatment_value(_production_sources())


def test_the_money_path_uses_the_canonical_type_rather_than_strings() -> None:
    assert_money_path_uses_the_canonical_type(_production_sources())


def test_the_hand_written_sql_literals_agree_with_the_enum() -> None:
    """Two check constraints spell ``'escalate'`` in SQL rather than generating it.

    They are the one place the vocabulary is repeated outside the enum, and both carry real meaning:
    an abstaining proposal must escalate, and an escalated treatment is never posted. If the member
    were ever renamed, both would quietly stop matching anything.
    """
    source = CONTROL_SOURCE.read_text(encoding="utf-8")
    escalate = TreatmentCode.ESCALATE.value
    assert f"NOT abstained OR treatment = '{escalate}'" in source
    assert f"approved_treatment <> '{escalate}'" in source


def test_abstention_is_a_separate_flag_and_not_a_fifth_treatment() -> None:
    """§6.1 puts ``abstained`` beside the treatment, not inside it.

    A model declining to answer has still not chosen an action. Giving that its own code would let a
    refusal to decide look like a decision — and the schema requires an abstaining proposal to carry
    ``escalate``, which is the same statement enforced from the other side.
    """
    assert "abstain" not in {member.value for member in TreatmentCode}
    source = CONTROL_SOURCE.read_text(encoding="utf-8")
    assert "abstained: Mapped[bool]" in source
    assert "abstention_escalates" in source


# ======================================================================================
# Valid is not the same contract as priceable
# ======================================================================================


def _facts(
    classification: ExceptionClassification,
    amount: str = "100.00",
    currency: str = "EUR",
    originating_period: str | None = "2026-06",
) -> ExceptionFacts:
    return ExceptionFacts(
        exception_id=uuid.UUID("5c0f0000-0000-4000-8000-000000000001"),
        classification=classification,
        amount=decimal.Decimal(amount),
        currency=currency,
        value_date=dt.date(2026, 7, 10),
        originating_period=originating_period,
    )


@pytest.mark.parametrize("treatment", list(TreatmentCode))
def test_every_treatment_is_valid_whatever_the_calculator_makes_of_it(
    treatment: TreatmentCode,
) -> None:
    """Validity and priceability are different contracts, and this is the line between them.

    Every member is a legitimate instruction. Whether M2.4 can price one depends on the exception it
    is applied to — and a treatment it cannot price is not an invalid treatment, it is a case that
    escalates.
    """
    assert TreatmentCode(treatment.value) is treatment
    result = compute_adjustment(
        _facts(ExceptionClassification.CHARGEBACK_REVERSAL), treatment, OPEN_BOOKS
    )
    assert isinstance(result, AdjustmentInstruction | NonCalculable)


@pytest.mark.parametrize("treatment", list(TreatmentCode))
def test_a_valid_treatment_on_an_unclassified_exception_still_fails_closed(
    treatment: TreatmentCode,
) -> None:
    """A valid treatment is not a licence to post.

    ``unclassified`` means the system could not say what the residual is, so no treatment can say
    what to do about it. Being in the vocabulary buys nothing here.
    """
    result = compute_adjustment(_facts(ExceptionClassification.UNCLASSIFIED), treatment, OPEN_BOOKS)
    assert isinstance(result, NonCalculable)


@pytest.mark.parametrize(
    "classification",
    [
        ExceptionClassification.FEE_SPLIT,
        ExceptionClassification.PARTIAL_CAPTURE,
        ExceptionClassification.FX_ROUNDING,
        ExceptionClassification.UNCLASSIFIED,
    ],
)
def test_an_unpriceable_class_escalates_rather_than_needing_a_new_treatment(
    classification: ExceptionClassification,
) -> None:
    """The property that keeps the vocabulary finite.

    Each of these conditions is real and none can be priced. Without ``escalate`` each would want
    its own treatment and the set would grow with the taxonomy; with it, the set of *actions* stays
    at four while the set of *conditions* grows freely.
    """
    for treatment in TreatmentCode:
        assert isinstance(
            compute_adjustment(_facts(classification), treatment, OPEN_BOOKS), NonCalculable
        )
    assert compute_adjustment(_facts(classification), TreatmentCode.ESCALATE, OPEN_BOOKS) is (
        NonCalculable.TREATMENT_IS_ESCALATE
    )


def test_priceability_depends_on_the_ledger_context_not_on_the_vocabulary() -> None:
    """A chargeback reversal is priceable — in the currency the books are kept in.

    Worth separating, because the corpus's chargeback reversals settle in USD against EUR demo books
    and therefore refuse. That is a property of the configuration, not evidence that the treatment
    set is short of a member.
    """
    usd = _facts(ExceptionClassification.CHARGEBACK_REVERSAL, currency="USD")
    assert compute_adjustment(usd, TreatmentCode.REBOOK, OPEN_BOOKS) is (
        NonCalculable.CURRENCY_NOT_FUNCTIONAL
    )
    usd_books: LedgerContext = dataclasses.replace(OPEN_BOOKS, functional_currency="USD")
    assert isinstance(
        compute_adjustment(usd, TreatmentCode.REBOOK, usd_books), AdjustmentInstruction
    )


# ======================================================================================
# No free-form path into the money path
# ======================================================================================


@pytest.mark.parametrize(
    "impostor", ["rebook", "escalate", "write_off", "nonsense", "", 0, 1, None, 12.5]
)
def test_a_value_outside_the_vocabulary_cannot_obtain_a_financial_instruction(
    impostor: object,
) -> None:
    """The hole this increment found and closed.

    ``TreatmentCode`` is a ``StrEnum``, so a member compares and hashes equal to its own value — and
    a bare ``"rebook"`` string therefore walked through a mapping keyed by members and **obtained a
    priced instruction**. ``"escalate"`` was worse: it slipped past the identity check that exists
    to stop escalation ever being priced, so the one treatment that must never produce an
    instruction stopped being recognised as itself.

    mypy rejects all of these, and mypy will not be in the room when M3.2 deserialises a provider's
    JSON. At that boundary a treatment arrives as text.
    """
    result = compute_adjustment(
        _facts(ExceptionClassification.CHARGEBACK_REVERSAL),
        impostor,  # type: ignore[arg-type]
        OPEN_BOOKS,
    )
    assert result is NonCalculable.TREATMENT_NOT_RECOGNISED


def test_a_lookalike_enum_member_is_not_accepted() -> None:
    """Not even a member of an identically-shaped enum declared somewhere else.

    That is the shape a second vocabulary would take, and it must not be interchangeable with the
    canonical one — otherwise "declared once" would be a documentation claim rather than a
    mechanism.
    """

    class Impostor(enum.StrEnum):
        REBOOK = "rebook"

    result = compute_adjustment(
        _facts(ExceptionClassification.CHARGEBACK_REVERSAL),
        Impostor.REBOOK,  # type: ignore[arg-type]
        OPEN_BOOKS,
    )
    assert result is NonCalculable.TREATMENT_NOT_RECOGNISED


def test_an_instance_of_the_class_that_is_not_a_member_is_refused() -> None:
    """The narrowest impostor of all, and the one that beat the first version of the boundary.

    ``str.__new__(TreatmentCode, "accrue")`` is an instance of the class without being any member
    of it. The boundary used to test ``isinstance``, so this passed — and was then priced *into the
    wrong period*, because the calculator compares by identity while the account table resolves by
    equality: an instruction labelled ``accrue`` posted where ``rebook`` posts. Membership is
    identity against the four, which is the only test the two halves agree on.
    """
    impostor = str.__new__(TreatmentCode, "accrue")
    assert isinstance(impostor, TreatmentCode)
    assert not any(impostor is member for member in TreatmentCode)

    facts = _facts(ExceptionClassification.CROSS_PERIOD_REFUND, originating_period="2026-01")
    assert compute_adjustment(facts, impostor, OPEN_BOOKS) is (
        NonCalculable.TREATMENT_NOT_RECOGNISED
    )
    # And the member it imitates is still priced normally, so the guard rejects the impostor rather
    # than the value.
    genuine = compute_adjustment(facts, TreatmentCode.ACCRUE, OPEN_BOOKS)
    assert isinstance(genuine, AdjustmentInstruction)


@pytest.mark.parametrize("treatment", list(TreatmentCode))
def test_every_legitimate_construction_route_yields_the_member(treatment: TreatmentCode) -> None:
    """Identity is only a safe membership test if every honest way in returns the singleton."""
    routes = [
        treatment,
        TreatmentCode(treatment.value),
        TreatmentCode[treatment.name],
        copy.copy(treatment),
        copy.deepcopy(treatment),
        pickle.loads(pickle.dumps(treatment)),
    ]
    for route in routes:
        assert route is treatment


def test_the_account_policy_also_refuses_a_non_member() -> None:
    """The other place a treatment is used as a key. It validates at configuration time."""
    with pytest.raises(TypeError, match="not a treatment"):
        account_policy(
            [(ExceptionClassification.CHARGEBACK_REVERSAL, "rebook", "4900")]  # type: ignore[list-item]
        )


def test_the_account_table_cannot_be_edited_after_construction() -> None:
    """Its checks run at construction, so a live mapping would make them advisory.

    A reviewer assigned into ``DEMO_ACCOUNT_POLICY.rules`` on the frozen singleton and obtained an
    instruction posting to ``NOT-AN-ACCOUNT`` — the dataclass was frozen, the mapping it held was
    not. This matters more than an ordinary immutability nicety: ``adjustment.account_code`` has no
    database constraint behind it, so this table is where account-code shape is enforced at all.
    """
    policy = account_policy(
        [(ExceptionClassification.CHARGEBACK_REVERSAL, TreatmentCode.REBOOK, "4900")]
    )
    with pytest.raises(TypeError):
        policy.rules[  # type: ignore[index]
            (ExceptionClassification.CHARGEBACK_REVERSAL, TreatmentCode.WRITE_OFF)
        ] = "NOT-AN-ACCOUNT"
    with pytest.raises(TypeError):
        DEMO_ACCOUNT_POLICY.rules[  # type: ignore[index]
            (ExceptionClassification.CROSS_PERIOD_REFUND, TreatmentCode.REBOOK)
        ] = "NOT-AN-ACCOUNT"

    # Escalate cannot be smuggled in after the fact either, which is what makes the construction
    # check an invariant rather than an entry formality.
    with pytest.raises(TypeError):
        DEMO_ACCOUNT_POLICY.rules[  # type: ignore[index]
            (ExceptionClassification.CROSS_PERIOD_REFUND, TreatmentCode.ESCALATE)
        ] = "9999"


# ======================================================================================
# The gate: every fixture exception resolves, and no treatment proposes an amount
# ======================================================================================


def _corpus_exceptions(
    profile: Profile, instances: int
) -> list[tuple[ExceptionClassification, ExceptionFacts, decimal.Decimal]]:
    """Run the real pipeline and return one entry per exception it produced."""
    corpus = generate(SEED, profile, instances)
    rows = {row.id: row for batch in corpus.corpus.batches for row in batch.lines}
    outcome = match(
        [
            CandidateLine(r.id, r.line_number, r.amount, r.currency, r.value_date)
            for r in rows.values()
        ],
        [
            CandidateEntry(e.id, e.external_ref, e.amount, e.currency, e.booked_at.date())
            for e in corpus.corpus.ledger_entries
        ],
        DEFAULT_POLICY,
    )
    matched = {pair.line_id for pair in outcome.matches}
    movements = [
        SettlementMovement(
            r.id,
            r.merchant_reference,
            movement_type(r.transaction_type),
            r.amount,
            r.currency,
            r.value_date,
            r.id in matched,
        )
        for r in rows.values()
    ]
    by_id = {m.id: m for m in movements}

    def originating(subject: SettlementMovement) -> str | None:
        if subject.merchant_reference is None:
            return None
        offsets = [
            other
            for other in movements
            if other.id != subject.id
            and other.matched
            and other.merchant_reference == subject.merchant_reference
            and other.currency == subject.currency
            and other.amount == -subject.amount
        ]
        return accounting_period(offsets[0].value_date) if len(offsets) == 1 else None

    collected = []
    for decision in classify([m for m in movements if not m.matched], movements):
        movement = by_id[decision.line_id]
        collected.append(
            (
                decision.classification,
                ExceptionFacts(
                    exception_id=decision.line_id,
                    classification=decision.classification,
                    amount=movement.amount,
                    currency=movement.currency,
                    value_date=movement.value_date,
                    originating_period=originating(movement),
                ),
                movement.amount,
            )
        )
    return collected


#: What the pipeline actually produces, measured rather than asserted into existence.
#:
#: Pinning these was a correction. The first version of the exit criterion read
#: ``assert priced or outcomes[ESCALATE] is TREATMENT_IS_ESCALATE`` — and the right-hand side is a
#: constant, because the calculator answers ``escalate`` before it looks at a single fact. Two
#: reviewers showed the same thing independently: the test passed against a corpus of five
#: fabricated, deliberately unpriceable exceptions, and it would pass against an empty account
#: policy. It could not have returned the verdict the gate exists to be able to return.
#:
#: Counts fix that. They tie the assertions to *this* pipeline's output, and they stop the loops
#: below from silently executing zero comparisons — the failure mode that made the tautology
#: invisible.
CORPUS_SHAPE: Final = {
    # (profile, instances): (exceptions, exceptions priced by some treatment, instructions produced)
    (Profile.CANONICAL, 200): (13, 1, 3),
    (Profile.BULK, 200): (39, 2, 6),
    (Profile.BULK, 1000): (207, 10, 30),
}

#: The refusals a *genuine* member may give over this corpus.
#:
#: ``TREATMENT_NOT_RECOGNISED`` is excluded deliberately: a real member reaching it would mean the
#: money boundary no longer recognises its own vocabulary. ``TREATMENT_IS_ESCALATE`` is excluded
#: because these are the non-escalate treatments.
EXPECTED_REFUSALS: Final = frozenset(NonCalculable) - {
    NonCalculable.TREATMENT_NOT_RECOGNISED,
    NonCalculable.TREATMENT_IS_ESCALATE,
}


@pytest.mark.parametrize(("profile", "instances"), sorted(CORPUS_SHAPE, key=str))
def test_every_fixture_exception_resolves_inside_the_vocabulary(
    profile: Profile, instances: int
) -> None:
    """**The exit criterion**, in a form that can fail.

    Every exception the corpus produces is answered inside the four: some treatment prices it, or
    every treatment refuses for an *enumerated* reason and escalate is the answer. What would end
    the containment claim is a case whose correct handling lies outside the vocabulary — an action
    none of the four names, or a refusal the calculator has no reason for.

    Being priced is not the standard, and the numbers say why: 10 of 207 exceptions price at the
    largest size. That is the demo account policy's coverage, not a property of the vocabulary —
    ``unclassified`` is deliberately mapped to no account, because an exception the system cannot
    even name must not receive an automatic one. The other 197 escalate, which is a resolution.
    """
    exceptions = _corpus_exceptions(profile, instances)
    expected_count, expected_priced, expected_instructions = CORPUS_SHAPE[(profile, instances)]
    assert len(exceptions) == expected_count, "the corpus is not what this gate was measured on"

    priced_exceptions = 0
    instructions = 0
    for _classification, facts, own_amount in exceptions:
        outcomes = {
            treatment: compute_adjustment(facts, treatment, OPEN_BOOKS)
            for treatment in TreatmentCode
            if treatment is not TreatmentCode.ESCALATE
        }
        for treatment, result in outcomes.items():
            if isinstance(result, AdjustmentInstruction):
                instructions += 1
                assert result.amount == own_amount, (
                    f"{treatment.value} produced {result.amount}, not the movement's {own_amount}"
                )
                assert result.currency == facts.currency
                assert result.treatment is treatment
            else:
                assert result in EXPECTED_REFUSALS, (
                    f"{treatment.value} refused {facts.classification.value} with {result}, "
                    "which is not an enumerated refusal"
                )
        priced_exceptions += any(
            isinstance(result, AdjustmentInstruction) for result in outcomes.values()
        )

    assert (priced_exceptions, instructions) == (expected_priced, expected_instructions), (
        f"{profile.value}@{instances} priced {priced_exceptions} exceptions in {instructions} "
        f"instructions, not the measured {expected_priced} in {expected_instructions}"
    )


@pytest.mark.parametrize(("profile", "instances"), sorted(CORPUS_SHAPE, key=str))
def test_escalate_is_never_priced_over_the_corpus(profile: Profile, instances: int) -> None:
    """Escalate refuses every exception in the corpus.

    Honest about what this is: a constant. ``compute_adjustment`` returns ``TREATMENT_IS_ESCALATE``
    before it reads a fact, so no corpus can make it fail — which is exactly why the first version
    of the exit criterion above was able to hide behind it. It is asserted anyway, and separately,
    because the day it stops holding is the day escalation becomes priceable. Treat it as a
    property of the calculator, never as evidence about the corpus.
    """
    for _classification, facts, _own in _corpus_exceptions(profile, instances):
        assert (
            compute_adjustment(facts, TreatmentCode.ESCALATE, OPEN_BOOKS)
            is NonCalculable.TREATMENT_IS_ESCALATE
        )


def test_the_corpus_exercises_every_classification() -> None:
    """A gate run over a corpus containing one condition would prove very little.

    The distribution is pinned for the same reason the counts above are: a corpus that stopped
    covering the taxonomy would otherwise weaken the gate silently.
    """
    counts = collections.Counter(c for c, _f, _a in _corpus_exceptions(Profile.BULK, 1000))
    assert dict(counts) == {
        ExceptionClassification.UNCLASSIFIED: 117,
        ExceptionClassification.FEE_SPLIT: 60,
        ExceptionClassification.CHARGEBACK_REVERSAL: 20,
        ExceptionClassification.CROSS_PERIOD_REFUND: 10,
    }
    # Two classes the taxonomy defines never survive to become exceptions in this corpus:
    # `partial_capture` and `fx_rounding` are, by construction, the cases deterministic matching
    # clears (ADR-042, ADR-046). Their absence is the matcher working, not a gap in the gate — and
    # naming them here means a future corpus that starts leaking them shows up as a failure rather
    # than as a silently broader test.
    assert set(ExceptionClassification) - set(counts) == {
        ExceptionClassification.PARTIAL_CAPTURE,
        ExceptionClassification.FX_ROUNDING,
    }


# ======================================================================================
# Kill tests — every guard, shown failing
# ======================================================================================


def _mutated(
    path: str, replacement: tuple[str, str], count: int = 1
) -> list[tuple[str, ast.Module]]:
    """A parsed copy of the real package with a substitution applied to one file.

    In memory, never on disk. An earlier increment had a reviewer leave a mutation in production
    source, and the money path is the last place that should be possible.

    One substitution by default, because a mutation is a claim about a specific line. ``count=-1``
    renames a symbol throughout, which is the only way to model a module that has stopped
    referencing the canonical type at all.
    """
    sources: list[tuple[str, ast.Module]] = []
    for name, _tree in _production_sources():
        text = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
        if name == path:
            old, new = replacement
            assert old in text, f"the mutation target {old!r} is not in {path}"
            text = text.replace(old, new, count)
        sources.append((name, ast.parse(text)))
    return sources


def test_kill_an_unauthorised_treatment_is_detected() -> None:
    """Mutation 1 — a fifth member appears in the vocabulary."""

    class Rogue(enum.StrEnum):
        REBOOK = "rebook"
        ACCRUE = "accrue"
        WRITE_OFF = "write_off"
        ESCALATE = "escalate"
        AUTO_POST = "auto_post"

    with pytest.raises(AssertionError, match=r"auto_post|specification says"):
        assert_vocabulary_is_closed(Rogue)

    assert_vocabulary_is_closed(TreatmentCode)


def test_kill_a_numeric_treatment_parameter_is_detected() -> None:
    """Mutation 2 — a member smuggles an amount into the one channel a model may use."""

    class Rogue(enum.StrEnum):
        REBOOK = "rebook"
        WRITE_OFF_AMOUNT = "write_off_125_50"

    with pytest.raises(AssertionError, match="carries a number"):
        assert_no_numeric_payload(Rogue)

    assert_no_numeric_payload(TreatmentCode)


def test_kill_a_second_vocabulary_declaration_is_detected() -> None:
    """Mutation 3 — a second treatment enum is declared elsewhere in the package."""
    mutated = _mutated(
        "money/policy.py",
        (
            "class AccountPolicy:",
            "class LocalTreatment(enum.StrEnum):\n"
            '    REBOOK = "rebook"\n'
            '    ACCRUE = "accrue"\n'
            "\n\nclass AccountPolicy:",
        ),
    )
    with pytest.raises(AssertionError, match=r"declared in 2 places|LocalTreatment"):
        assert_one_canonical_declaration(mutated)

    assert_one_canonical_declaration(_production_sources())


def test_kill_calculator_vocabulary_drift_is_detected() -> None:
    """Mutation 4 — the money path hardcodes a treatment string instead of using the type."""
    mutated = _mutated(
        "money/calculator.py",
        ("if treatment is TreatmentCode.ESCALATE:", 'if treatment == "escalate":'),
    )
    with pytest.raises(AssertionError, match="hardcodes the treatment literal"):
        assert_no_module_repeats_a_treatment_value(mutated)

    assert_no_module_repeats_a_treatment_value(_production_sources())


def test_kill_drift_outside_the_money_directory_is_detected() -> None:
    """Mutation 4b — the same drift, one directory over, where the first guard was not looking.

    ``demo/snapshot.py`` picks the treatment it prices with and calls the calculator, so a literal
    there is the identical defect. This is the reviewer's escape, kept as a test so the scan can
    never quietly narrow back to ``money/``.
    """
    mutated = _mutated(
        "demo/snapshot.py",
        ("DEMO_TREATMENT: Final = TreatmentCode.REBOOK", 'DEMO_TREATMENT: Final = "rebook"'),
    )
    with pytest.raises(AssertionError, match="hardcodes the treatment literal"):
        assert_no_module_repeats_a_treatment_value(mutated)


def test_kill_a_module_deciding_without_the_canonical_type_is_detected() -> None:
    """Mutation 4c — the calculator branches on treatments without naming the type at all.

    The other half of the pair, and previously the half nothing exercised: a module could shed
    every reference to ``TreatmentCode`` and still hold no literal, which is drift the scan above
    cannot see.
    """
    mutated = _mutated("money/calculator.py", ("TreatmentCode", "TreatmentKind"), count=-1)
    with pytest.raises(AssertionError, match="decides on treatments without the canonical type"):
        assert_money_path_uses_the_canonical_type(mutated)


def test_kill_a_guard_that_inspects_nothing_is_detected() -> None:
    """Mutation 4d — the source list stops containing what the guards claim to inspect.

    A structural guard reading an empty or filtered list passes for the worst possible reason. Both
    guards assert their own coverage, and this is what shows those assertions can fail.
    """
    complete = _production_sources()
    with pytest.raises(AssertionError, match="was not inspected"):
        assert_money_path_uses_the_canonical_type(
            [pair for pair in complete if pair[0] != "money/policy.py"]
        )
    with pytest.raises(AssertionError, match="inspected nothing"):
        assert_no_module_repeats_a_treatment_value([])


def test_kill_a_free_form_treatment_path_is_detected() -> None:
    """Mutation 5 — the runtime boundary is removed and a bare string gets priced again.

    The most valuable of the five, because it reproduces the defect this increment actually found:
    before the guard existed, ``"rebook"`` obtained a real instruction. Simulated by doing what the
    boundary did before M3.1 — trust the annotation and carry on.
    """
    facts = _facts(ExceptionClassification.CHARGEBACK_REVERSAL)

    def without_the_guard(treatment: object) -> object:
        if treatment is TreatmentCode.ESCALATE:
            return NonCalculable.TREATMENT_IS_ESCALATE
        return OPEN_BOOKS.accounts.account_for(
            facts.classification,
            treatment,  # type: ignore[arg-type]
        )

    assert without_the_guard("rebook") == "4900", (
        "the mutation must reproduce the original defect, or it proves nothing"
    )
    assert compute_adjustment(facts, "rebook", OPEN_BOOKS) is (  # type: ignore[arg-type]
        NonCalculable.TREATMENT_NOT_RECOGNISED
    ), "the guard must reject what the mutation accepted"


@pytest.mark.parametrize(
    "guard",
    [
        assert_one_canonical_declaration,
        assert_money_path_uses_the_canonical_type,
        assert_no_module_repeats_a_treatment_value,
    ],
)
def test_the_structural_guards_pass_on_the_real_package(
    guard: Callable[[list[tuple[str, ast.Module]]], None],
) -> None:
    """The control. A guard that raised unconditionally would sail through every kill test above."""
    guard(_production_sources())


def test_no_mutation_reached_disk() -> None:
    """Every mutation above is applied to a parsed copy. The files themselves are untouched.

    Asserted rather than assumed, because a previous increment had exactly this go wrong — and
    checked against *code* rather than raw text, since ``db/control.py`` legitimately documents
    ``write_off_125_50`` as an example of the escape hatch it forbids.
    """
    for name in ("money/calculator.py", "money/policy.py", "db/control.py"):
        tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
        classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        assert "LocalTreatment" not in classes, f"{name} carries a leftover mutation"
        for literal in _code_string_constants(tree):
            assert literal not in {"auto_post", "write_off_125_50", "adjust_by_0_7_percent"}, (
                f"{name} carries a leftover mutation: {literal!r}"
            )

    calculator = (PACKAGE_ROOT / "money" / "calculator.py").read_text(encoding="utf-8")
    assert "if treatment is TreatmentCode.ESCALATE:" in calculator
    assert 'treatment == "escalate"' not in calculator
    assert "any(treatment is member for member in TreatmentCode)" in calculator, (
        "the runtime membership check is what mutation 5 removes"
    )
    assert "TreatmentKind" not in calculator, "the rename from mutation 4c is still in place"

    # And the caller one directory over, which mutation 4b rewrites.
    snapshot = (PACKAGE_ROOT / "demo" / "snapshot.py").read_text(encoding="utf-8")
    assert "DEMO_TREATMENT: Final = TreatmentCode.REBOOK" in snapshot


# ======================================================================================
# Scope: M3.2 and beyond do not exist
# ======================================================================================


def test_no_provider_or_model_code_exists() -> None:
    """M3.1 calls nothing. The provider port, the response schema and the prompt are all M3.2."""
    assert not (PACKAGE_ROOT / "llm").exists()
    assert not (PACKAGE_ROOT / "providers").exists()

    providers = {
        "openai",
        "anthropic",
        "litellm",
        "instructor",
        "langchain",
        "cohere",
        "pydantic_ai",
    }
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert module.split(".")[0] not in providers, f"{path.name} imports {module}"


def test_no_proposal_generation_workflow_exists() -> None:
    """``treatment_proposal`` is a table M1.2 built for M3.2 to write. Nothing writes it yet."""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name != "control.py":
            assert "TreatmentProposal(" not in text, f"{path.name} constructs a proposal"
        for forbidden in ("prompt_hash=", "cassette_id=", "rationale="):
            assert forbidden not in text, f"{path.name} builds proposal provenance"
