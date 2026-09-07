"""Can this gate fail? — the mutation battery for the chaos suite's own instrumentation (4.5).

A kill test is worth exactly as much as its ability to go red. `PROJECT_SPEC.md` §19 states the
consequence plainly: *"A suite that passes on both branches proves nothing and is a defect."* But a
suite can be theatre in quieter ways than passing on both branches, and each of them looks like
success:

1. **The fault never fired.** ``post`` was called, the wrapper's condition was wrong, the adapter
   answered ``Confirmed`` — and every "applied exactly once" assertion passed for the worst possible
   reason.
2. **The measurement came from our own records.** §19.1 forbids this by name: *"it does not infer
   the outcome from application state, since inferring from our own records is exactly what fails
   here."* An assertion reading ``posting_attempt`` would have agreed with itself while the ledger
   held two postings.
3. **The double is too forgiving.** An adapter that suppresses duplicates internally whatever it
   declares cannot double-book, so *nothing* driven through it can be shown to double-post, and the
   RED column would be structurally unreachable.
4. **The counter is the wrapper's opinion.** A tally maintained by the fault injector rather than by
   the ledger is the same category of evidence as (2), one layer out.
5. **The expectations were fitted to the run.** The tables in :mod:`tests.chaos.scenarios` are read
   by both runners and written by neither, which is only meaningful if a wrong table actually breaks
   the suite.

Every item above is *planted here and observed to fail*. These tests pass by watching a mutant lose:
each one builds the defect deliberately, drives the same scenario the real suite drives, and asserts
the number comes out wrong. If a mutant ever stops producing a wrong number, the corresponding
assertion in the real suite has stopped being able to detect that defect — which is the only thing
this module is for.

**It lives outside ``tests/chaos/`` and needs no database**, which is deliberate on both counts.
The mutants concern the ledger boundary, the fault port and the expectation tables — all
in-process — so the battery runs in the default unit suite on every CI build, rather than only when
a PostgreSQL service is up. The chaos package's fixtures migrate a real schema per module; a battery
that inherited them would cost minutes to prove something with no database in it.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Final

import pytest

from ledger_exception_control_plane.ledger import (
    Confirmed,
    Fault,
    FaultInjectingLedger,
    IdempotencyMode,
    LedgerAdapterCapabilities,
    Linearizable,
    NonIdempotentLedger,
    PostingInstruction,
    PostingOutcome,
    PostingQueryMode,
    QueryableNonIdempotentLedger,
    SimulatedLedger,
    Unknown,
    capabilities_for,
)
from tests.chaos.scenarios import (
    NAIVE_DOUBLE_POSTS,
    Capability,
    Expectation,
    Scenario,
    adapter_for,
    expectation,
)

OPERATION: Final = uuid.uuid5(uuid.NAMESPACE_OID, "mutation").hex * 2

INSTRUCTION: Final = PostingInstruction(
    adjustment_id=uuid.UUID("44444444-4444-5444-8444-444444444444"),
    amount=decimal.Decimal("2799.9700"),
    currency="EUR",
    account_code="4100",
    period="2026-06",
)


# ======================================================================================
# 1. A fault that does not fire
# ======================================================================================


class _SilentlyBrokenInjector(FaultInjectingLedger):
    """The mutant: the fault is configured, and ``post`` forgets to apply it.

    Not a contrived shape. It is one inverted condition away from the real wrapper, and the
    difference is invisible from the outside — the adapter still answers, the scenario still runs,
    every applied-count is still 1.
    """

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        return await self.wrapped.post(operation_id, instruction)


@pytest.mark.asyncio
async def test_a_fault_that_never_fires_is_caught_by_the_injection_count() -> None:
    """**Why every §19.1 assertion checks ``injections`` before it checks anything else.**

    The mutant produces a *correct-looking* result: one posting applied, a ``Confirmed`` returned,
    nothing ambiguous anywhere. Under the real wrapper the client is told ``Unknown``; under the
    mutant it is told the truth, and the scenario silently stopped being the scenario.

    The applied-count cannot tell them apart — it is 1 either way, which is the whole trap. The
    injection count can, and it is the reason it exists.
    """
    real = FaultInjectingLedger(SimulatedLedger(), fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    mutant = _SilentlyBrokenInjector(SimulatedLedger(), fault=Fault.COMMIT_THEN_LOSE_RESPONSE)

    honest = await real.post(OPERATION, INSTRUCTION)
    silent = await mutant.post(OPERATION, INSTRUCTION)

    assert isinstance(honest, Unknown), "the real wrapper loses the response"
    assert isinstance(silent, Confirmed), "the mutant quietly ran a different scenario"

    assert real.applied_count(OPERATION) == mutant.applied_count(OPERATION) == 1, (
        "the applied-count is identical, so it cannot be what detects this"
    )
    assert real.injections == 1
    assert mutant.injections == 0, "the count the suite asserts on is the one that sees the defect"


# ======================================================================================
# 2. A measurement taken from our own records
# ======================================================================================


class _CountsWhatItWasTold:
    """The mutant: a "ledger" that reports what it was *asked* to apply.

    This is the lost-response defect expressed as a measuring instrument. It agrees with itself
    perfectly and knows nothing about the books — precisely the reasoning §19.1 forbids, which is
    why the real suite reads the inner ledger's applied-count and never an attempt row.
    """

    name = "counts-requests"

    def __init__(self) -> None:
        self.requests = 0

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.ENFORCES_KEY,
            idempotency_window=dt.timedelta(days=1),
            posting_identity_query=PostingQueryMode.NONE,
            query_consistency=Linearizable(),
            max_inflight_window=dt.timedelta(seconds=30),
        )

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        self.requests += 1
        return Confirmed(posting_ref=f"REQ-{self.requests}")

    def applied_count(self, operation_id: str) -> int:
        return self.requests


@pytest.mark.asyncio
async def test_inferring_from_requests_rather_than_from_the_books_reports_the_wrong_number() -> (
    None
):
    """**The two numbers diverge, and the suite must read the one that comes from the books.**

    Under an enforcing ledger the same identifier sent twice is *received* twice and *applied* once.
    An instrument that counts requests says 2 and is wrong; the ledger says 1 and is right. A suite
    built on the first would report a double-post that never happened — and, run against the
    baseline, would report duplicates it had not demonstrated.

    So this mutant fails in the flattering direction, which is the dangerous one: it would have made
    the RED column look stronger than the evidence.
    """
    honest = SimulatedLedger()
    mutant = _CountsWhatItWasTold()

    for ledger in (honest, mutant):
        await ledger.post(OPERATION, INSTRUCTION)
        await ledger.post(OPERATION, INSTRUCTION)

    assert honest.applied_count(OPERATION) == 1, "suppressed at the books"
    assert honest.posts_received == 2, "and received twice — the two numbers are not the same"
    assert mutant.applied_count(OPERATION) == 2, (
        "the mutant reports a duplicate the ledger never committed"
    )


# ======================================================================================
# 3. A double too forgiving to double-book
# ======================================================================================


@pytest.mark.asyncio
async def test_a_ledger_that_suppresses_internally_cannot_demonstrate_the_failure() -> None:
    """**Why 4.5 needed a third reference adapter instead of relabelling the reference one.**

    ``SimulatedLedger`` suppresses a repeated identifier *internally*, whatever its declaration
    says. Configure it ``NONE``/``NONE`` and the label is weak while the behaviour stays strong — so
    every scenario driven through it applies once, on both branches, and §19's comparison has
    nothing left to measure. That is the mislabelling §19 warns about in its own words: *"a suite
    that tests only the strong adapter proves only the easy case."*

    The two adapters written for the weak configurations genuinely double-book, and this test is
    what says so rather than the label on them.
    """
    mislabelled = SimulatedLedger(
        capabilities=LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.NONE, posting_identity_query=PostingQueryMode.NONE
        )
    )
    assert capabilities_for(mislabelled).idempotency is IdempotencyMode.NONE, "declares nothing"

    for _ in range(2):
        await mislabelled.post(OPERATION, INSTRUCTION)
    assert mislabelled.applied_count(OPERATION) == 1, (
        "and suppresses anyway — a double this forgiving makes the RED column unreachable"
    )

    for genuine in (NonIdempotentLedger(), QueryableNonIdempotentLedger()):
        for _ in range(2):
            await genuine.post(OPERATION, INSTRUCTION)
        assert genuine.applied_count(OPERATION) == 2, (
            f"{genuine.name} must genuinely double-book, or nothing driven through it can fail"
        )


# ======================================================================================
# 4. A count maintained by the wrapper
# ======================================================================================


class _KeepsItsOwnTally(FaultInjectingLedger):
    """The mutant: the wrapper answers the applied-count from a number it maintains itself.

    It even looks more careful than the real thing — an explicit counter incremented exactly where
    the fault decides the books moved. And it is one layer of the same defect as (2): the assertion
    would be reading the wrapper's belief about the ledger instead of the ledger.
    """

    def __init__(self, adapter: SimulatedLedger, *, fault: Fault) -> None:
        super().__init__(adapter, fault=fault)
        self.tally = 0

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        outcome = await super().post(operation_id, instruction)
        self.tally += 1
        return outcome

    def applied_count(self, operation_id: str) -> int:
        return self.tally


@pytest.mark.asyncio
async def test_a_wrapper_maintained_count_disagrees_with_the_books() -> None:
    """The tally counts sends; the ledger counts postings. Under suppression they differ.

    Two sends of one identifier through an enforcing ledger: the books hold one posting and the
    wrapper's tally says two. The real wrapper delegates ``applied_count`` for exactly this reason,
    and the docstring there says so — this test is what makes that sentence checkable.
    """
    inner = SimulatedLedger()
    mutant = _KeepsItsOwnTally(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)

    await mutant.post(OPERATION, INSTRUCTION)
    await mutant.post(OPERATION, INSTRUCTION)

    assert inner.applied_count(OPERATION) == 1, "the books"
    assert mutant.applied_count(OPERATION) == 2, "the wrapper's opinion of the books"
    assert FaultInjectingLedger(inner).applied_count(OPERATION) == 1, (
        "the real wrapper reports the books, because it does not keep a number of its own"
    )


# ======================================================================================
# 5. Expectations fitted to the run
# ======================================================================================


def test_a_mutated_expectation_changes_what_the_runners_would_assert_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The tables decide the assertions, so a wrong table must be able to fail them.**

    If a mutated table were absorbed — if a runner asserted a hard-coded literal instead of the
    table, or computed its expectation from the observation it just made — then declaring the
    expectations up front would be decoration and the suite could be made to agree with any result.

    **This test was itself a mutant once, and a reviewer caught it.** The first version asserted
    that ``dataclasses.replace(row, applied=2).applied != 1``, which exercises the standard library
    and no runner: it would have passed unchanged after any edit that made ``test_chaos_main.py``
    assert a literal. What actually establishes the property is patching the table and calling the
    *runners' own* lookup helpers, which is what happens below — both of them, because they read
    different tables through different signatures.

    ``monkeypatch`` restores the table afterwards, so no other test sees the mutation.
    """
    from tests.chaos import scenarios as scenario_tables
    from tests.chaos import test_chaos_main as main_runner
    from tests.chaos import test_chaos_naive as naive_runner

    scenario = Scenario.LOST_RESPONSE_AFTER_COMMIT
    capability = Capability.ENFORCES_KEY

    assert main_runner._expect(scenario, capability) == 1, "§19.1 on `main`, before the mutation"
    monkeypatch.setitem(
        scenario_tables._MAIN, (scenario, capability), Expectation(applied=99, note="mutant")
    )
    assert main_runner._expect(scenario, capability) == 99, (
        "the runner asserts a literal, not the declared table: the expectations decide nothing"
    )

    assert naive_runner._expect(scenario) == 2, "§19.1 on the baseline, before the mutation"
    monkeypatch.setitem(scenario_tables._NAIVE, scenario, Expectation(applied=98, note="mutant"))
    assert naive_runner._expect(scenario) == 98, (
        "the baseline runner asserts a literal, not the declared table"
    )


def test_the_declared_rows_for_the_required_scenario_differ_between_the_branches() -> None:
    """§19.1, on both branches, read from the tables the runners read.

    Separate from the mutation above because it is a different claim: that the two branches are
    *expected* to disagree here at all. Equal rows would be §19's named defect — a suite that
    passes on both branches — arrived at by arithmetic rather than by an implementation.
    """
    on_main = expectation(
        scenario=Scenario.LOST_RESPONSE_AFTER_COMMIT,
        capability=Capability.ENFORCES_KEY,
        branch="main",
    )
    on_naive = expectation(
        scenario=Scenario.LOST_RESPONSE_AFTER_COMMIT, capability=Capability.NONE, branch="naive"
    )

    assert on_main.applied == 1
    assert on_naive.applied == 2
    assert on_naive.applied > on_main.applied


def test_the_two_branches_are_expected_to_differ_somewhere_in_every_configuration() -> None:
    """**§19's exit criterion as a property of the tables, over the whole matrix.**

    *"`naive/` double-posts. `main` does not."* If some configuration had no scenario where the two
    columns differ, then in that configuration the suite would pass on both branches — §19's
    definition of a defect — and it would do so while every individual row agreed with its table.

    Asserted per configuration rather than globally, because a single differing row somewhere in a
    21-cell matrix would satisfy a global check while leaving two thirds of it uninformative.
    """
    for capability in Capability:
        differing = [
            scenario
            for scenario in Scenario
            if expectation(scenario=scenario, capability=capability, branch="main").applied
            != expectation(scenario=scenario, capability=capability, branch="naive").applied
        ]
        assert differing, f"no scenario distinguishes the branches under {capability.value}"
        assert set(differing) & NAIVE_DOUBLE_POSTS, (
            f"under {capability.value} the branches differ, but never by a duplicated effect"
        )


def test_no_configuration_expects_main_to_apply_more_than_once() -> None:
    """The other half of the criterion, stated over the table so it cannot be eroded row by row.

    A future scenario added with ``applied=2`` for `main` would be the reliability claim being
    withdrawn in a data structure, which is the quietest possible place to withdraw it.
    """
    for scenario in Scenario:
        for capability in Capability:
            row = expectation(scenario=scenario, capability=capability, branch="main")
            assert row.applied <= 1, f"main is expected to double-post: {scenario} / {capability}"


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", list(Capability))
async def test_each_configuration_is_the_capability_it_claims_to_be(capability: Capability) -> None:
    """**The labels in the results table are the adapters' verified capabilities, not prose.**

    §19 requires each scenario against three configurations, and the value of that is entirely in
    the three being genuinely different. This asserts the *effective* capability — what a caller
    branching on §13.5 actually sees, after every unproven claim is downgraded — through the fault
    wrapper, which is how the runners hold it.

    It is also the regression test for the defect that collapsed all three into one: the fault
    injector was absent from the unwrap list, so every configuration presented as ``NONE``/``NONE``
    and the suite exercised the weak branch three times.
    """
    wrapped = FaultInjectingLedger(adapter_for(capability), fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    effective = capabilities_for(wrapped)

    expected = {
        Capability.ENFORCES_KEY: (IdempotencyMode.ENFORCES_KEY, PostingQueryMode.NONE),
        Capability.BY_OPERATION_ID: (IdempotencyMode.NONE, PostingQueryMode.BY_OPERATION_ID),
        Capability.NONE: (IdempotencyMode.NONE, PostingQueryMode.NONE),
    }[capability]

    assert (effective.idempotency, effective.posting_identity_query) == expected
    assert effective.permits_effectively_once_claim is (capability is not Capability.NONE), (
        "§13.5's conditional claim is available in exactly two of the three configurations"
    )


# ======================================================================================
# The gate's two standing claims — neither needs a database, and that is why they are here
# ======================================================================================
#
# Both were written inside ``tests/chaos/test_chaos_naive.py``, where a module-level
# ``pytestmark = pytest.mark.integration`` swept them up with the fifty-four scenarios that
# genuinely need PostgreSQL. The default suite carries ``-m "not integration"``, so they were
# deselected by ``uv run pytest``, ``make test``, ``make gate`` and the CI `quality` job, and ran
# only inside the thirty-minute database job. A reviewer found it by collecting the module: 26
# deselected, 0 collected.
#
# That mattered most for the import guard, which is the *only* enforcement anywhere of
# `CLAUDE.md`'s one-way ``src/`` -> ``naive/`` dependency rule. An ``import naive`` added to the
# shipped package would have left ruff, mypy and the default test run green — mypy already has
# `naive` in its graph, and the repository root is on ``sys.path`` under pytest, so there would have
# been no ImportError to notice either.


def test_the_baseline_is_expected_to_double_post_somewhere() -> None:
    """**§19's exit criterion, as a claim about the expectation table rather than about a run.**

    *"It must double-post."* Asserted separately from the individual rows so that an edit which
    quietly turned every naive expectation into ``1`` would fail here rather than pass everywhere:
    the rows would all agree with the table, and the table would have stopped saying anything.

    §19.1 is named explicitly because it is the one scenario the specification requires by name.
    """
    assert NAIVE_DOUBLE_POSTS, "the RED baseline is expected to fail nowhere; the suite is theatre"
    assert Scenario.LOST_RESPONSE_AFTER_COMMIT in NAIVE_DOUBLE_POSTS
    assert len(NAIVE_DOUBLE_POSTS) >= 4, (
        "one failing row would make the gate rest on a single scenario"
    )


def test_nothing_in_the_shipped_package_imports_the_baseline() -> None:
    """`CLAUDE.md`: *"`naive/` holds the RED baseline and is never imported by `src/`."*

    The dependency runs one way. The baseline may read the ledger port, the reference adapters and
    the fault vocabulary — that is what makes both columns face the same world — and the shipped
    system may not know this directory exists.
    **Parsed, not grepped.** A substring scan for ``naive`` matches the word in a docstring — and
    this package's docstrings discuss the baseline at length, which is a feature. What must not
    exist is an *import*, so the check walks the actual import statements.
    """
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"

    # **The scan is asserted to have something to scan.** Moving this test one directory up
    # silently invalidated the relative path, ``rglob`` then yielded nothing, and the guard passed
    # while an ``import naive`` sat in the shipped package — found by planting exactly that and
    # watching it pass. A guard whose subject can go missing is a guard that reports success for
    # having looked nowhere.
    assert package.is_dir(), f"the shipped package is not where this test looks: {package}"
    modules = sorted(package.rglob("*.py"))
    assert len(modules) > 50, f"only {len(modules)} module(s) found; the scan is not reaching them"

    offenders: list[str] = []
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            if any(name == "naive" or name.startswith("naive.") for name in imported):
                offenders.append(f"{path.relative_to(package).as_posix()}:{node.lineno}")

    assert offenders == [], f"the shipped package imports the RED baseline: {offenders}"


def test_no_scenario_builds_a_posting_of_its_own() -> None:
    """A guard on the comparison rather than on the system, and it had to be rewritten.

    §19's table counts *adjustments posted*. That count is a count of the same thing only if every
    scenario posts the same instruction — otherwise "two" could mean two postings that were never
    duplicates of each other.

    **The first version asserted that ``instruction(...)`` returns the constants it is built from**,
    which is a tautology about the chaos package's own helper: it would have passed unchanged after
    someone added a scenario that constructed its own instruction with a different amount, which is
    the only thing that could actually break the count. A reviewer said so, and the verifier
    declined to confirm it; the mechanism was checked and the reviewer was right.

    So the property is checked where it can fail: **no module in the chaos suite may construct a
    posting instruction itself.** Both runners go through ``instruction`` and ``enqueued`` in
    ``conftest.py``, which read one ``AMOUNT``, and the baseline's ``_instruction`` reads the amount
    off the row it was handed. Parsed rather than grepped, so a docstring mentioning the type is not
    a finding.
    """
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[1] / "tests" / "chaos"
    assert package.is_dir(), f"the chaos package is not where this test looks: {package}"
    forbidden = {"PostingInstruction", "AdjustmentInstruction"}
    offenders: list[str] = []
    for path in sorted(package.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden
            ):
                offenders.append(f"{path.name}:{node.lineno} builds {node.func.id}")

    assert offenders == [], (
        "a scenario builds its own posting, so §19's counts may not be counting one thing: "
        f"{offenders}"
    )
