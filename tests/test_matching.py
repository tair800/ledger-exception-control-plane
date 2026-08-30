"""Matching tests — deterministic, Docker-free.

The decision is a pure function, so almost all of it can be tested without a database: the rules,
their precedence, the tolerance boundaries, ambiguity, and — the property that needed designing for
rather than asserting — independence from the order the inputs arrive in.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import itertools
import json
import pathlib
import uuid

import pytest

import ledger_exception_control_plane.matching as matching_package
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.matching.engine import (
    RULE_PRECEDENCE,
    CandidateEntry,
    CandidateLine,
    match,
)
from ledger_exception_control_plane.matching.policy import (
    DEFAULT_POLICY,
    EXACT_ONLY_POLICY,
    MatchRule,
    TolerancePolicy,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "fixtures" / "canonical"
MATCHING_ROOT = pathlib.Path(matching_package.__file__).resolve().parent

DAY = dt.date(2026, 6, 3)


def line(
    amount: str, *, currency: str = "EUR", day: dt.date = DAY, number: int = 1
) -> CandidateLine:
    return CandidateLine(
        id=uuid.uuid4(),
        line_number=number,
        amount=decimal.Decimal(amount),
        currency=currency,
        value_date=day,
    )


def entry(
    amount: str, *, currency: str = "EUR", day: dt.date = DAY, ref: str | None = None
) -> CandidateEntry:
    return CandidateEntry(
        id=uuid.uuid4(),
        external_ref=ref or f"GL-{uuid.uuid4().hex[:8]}",
        amount=decimal.Decimal(amount),
        currency=currency,
        booked_on=day,
    )


# --------------------------------------------------------------------------------------
# The exact rule
# --------------------------------------------------------------------------------------


def test_an_eligible_pair_matches_exactly() -> None:
    settlement, ledger = line("120.45"), entry("120.45")
    outcome = match([settlement], [ledger], DEFAULT_POLICY)

    assert len(outcome.matches) == 1
    matched = outcome.matches[0]
    assert matched.line_id == settlement.id
    assert matched.entry_id == ledger.id
    assert matched.rule is MatchRule.EXACT_AMOUNT
    # An exact match absorbed nothing, and says so. The pairing constraint on match_result
    # requires the amount and its currency to be both present or both absent.
    assert matched.tolerance_applied is None
    assert matched.tolerance_currency is None


def test_an_amount_difference_beyond_the_band_does_not_match() -> None:
    outcome = match([line("120.45")], [entry("130.45")], DEFAULT_POLICY)
    assert outcome.matches == ()
    assert not outcome.ambiguous_line_ids, "no candidate at all is not ambiguity"
    assert len(outcome.unmatched_line_ids) == 1


def test_signed_amounts_match_only_their_own_sign() -> None:
    """A chargeback of -326.92 is not the same movement as a credit of +326.92."""
    negative = line("-326.92")
    assert len(match([negative], [entry("-326.92")], DEFAULT_POLICY).matches) == 1
    assert match([negative], [entry("326.92")], DEFAULT_POLICY).matches == ()


def test_zero_amounts_are_not_special_cased() -> None:
    """The absolute-difference rule handles zero like any other value."""
    assert len(match([line("0.00")], [entry("0.00")], DEFAULT_POLICY).matches) == 1
    within = match([line("0.00")], [entry("0.01")], DEFAULT_POLICY)
    assert within.matches[0].rule is MatchRule.AMOUNT_WITHIN_TOLERANCE
    assert match([line("0.00")], [entry("0.02")], DEFAULT_POLICY).matches == ()


# --------------------------------------------------------------------------------------
# The tolerance band — boundaries
# --------------------------------------------------------------------------------------


#: ``(currency, settlement amount, strictly inside the band, exactly on it, one step past it)``.
#:
#: The "inside" column uses the storage contract's four decimal places rather than the currency's
#: minor unit, because a difference *strictly between* zero and one minor unit is not otherwise
#: representable. An earlier version put the base amount in that column — so the leg labelled
#: "below the band" was a zero difference, resolved by the exact rule, and nothing in the suite
#: ever exercised a non-zero difference inside the band. Caught by review.
BOUNDARY_CASES = [
    ("EUR", "120.45", "120.4550", "120.46", "120.47"),
    ("USD", "10.00", "10.0050", "10.01", "10.02"),
    ("GBP", "1828.49", "1828.4850", "1828.48", "1828.47"),
    ("JPY", "683880", "683880.5000", "683881", "683882"),
    ("BHD", "14.657", "14.6575", "14.658", "14.659"),
]


@pytest.mark.parametrize(("currency", "base", "inside", "boundary", "above"), BOUNDARY_CASES)
def test_the_band_is_inclusive_at_its_stated_value(
    currency: str, base: str, inside: str, boundary: str, above: str
) -> None:
    """Inside, exactly on, and one step past — for every currency the default policy declares.

    Inclusive at the boundary: the policy states the largest difference that may be absorbed, so
    that value is admissible. An exclusive reading would make the documented number the first one
    refused, which is not what "largest permitted" means.
    """
    settlement = line(base, currency=currency)

    within = match([settlement], [entry(inside, currency=currency)], DEFAULT_POLICY)
    assert len(within.matches) == 1, f"{currency}: a difference inside the band must match"
    assert within.matches[0].rule is MatchRule.AMOUNT_WITHIN_TOLERANCE, (
        f"{currency}: this leg must exercise the band, not the exact rule"
    )
    absorbed = within.matches[0].tolerance_applied
    assert absorbed is not None
    assert 0 < absorbed < DEFAULT_POLICY.amount[currency], (
        f"{currency}: the difference must be strictly inside the band"
    )

    at_edge = match([settlement], [entry(boundary, currency=currency)], DEFAULT_POLICY)
    assert len(at_edge.matches) == 1, f"{currency}: the band is inclusive"
    assert at_edge.matches[0].rule is MatchRule.AMOUNT_WITHIN_TOLERANCE
    assert at_edge.matches[0].tolerance_applied == DEFAULT_POLICY.amount[currency]
    assert at_edge.matches[0].tolerance_currency == currency

    beyond = match([settlement], [entry(above, currency=currency)], DEFAULT_POLICY)
    assert beyond.matches == (), f"{currency}: one unit past the band must not match"


@pytest.mark.parametrize(("currency", "base", "inside", "boundary", "above"), BOUNDARY_CASES)
def test_the_boundary_cases_are_the_differences_they_claim_to_be(
    currency: str, base: str, inside: str, boundary: str, above: str
) -> None:
    """The parametrisation itself, checked. A test table is code, and this one was wrong once."""
    band = DEFAULT_POLICY.amount[currency]
    settlement = decimal.Decimal(base)
    assert 0 < abs(settlement - decimal.Decimal(inside)) < band
    assert abs(settlement - decimal.Decimal(boundary)) == band
    assert abs(settlement - decimal.Decimal(above)) > band
    # Every amount stays inside the four-decimal storage contract the schema enforces.
    for amount in (base, inside, boundary, above):
        exponent = decimal.Decimal(amount).as_tuple().exponent
        assert isinstance(exponent, int)
        assert -exponent <= 4, f"{currency}: {amount} exceeds the money scale"


def test_the_absorbed_difference_is_recorded_exactly() -> None:
    """``tolerance_applied`` is the evidence that makes a historical match interpretable."""
    outcome = match(
        [line("1828.49", currency="GBP")], [entry("1828.48", currency="GBP")], DEFAULT_POLICY
    )
    absorbed = outcome.matches[0].tolerance_applied
    assert absorbed == decimal.Decimal("0.01")
    assert isinstance(absorbed, decimal.Decimal)
    # Non-negative, as match_result's own check constraint requires.
    assert absorbed >= 0


def test_a_currency_with_no_declared_band_gets_no_tolerance() -> None:
    """Fail-closed: undecided is not the same as zero, and neither is 'probably fine'."""
    settlement = line("100.00", currency="CHF")
    assert len(match([settlement], [entry("100.00", currency="CHF")], DEFAULT_POLICY).matches) == 1
    assert match([settlement], [entry("100.01", currency="CHF")], DEFAULT_POLICY).matches == ()
    assert DEFAULT_POLICY.band("CHF") is None


def test_the_tolerance_rule_is_what_admits_a_near_miss() -> None:
    """Proven by removing it: the same pair under an exact-only policy does not match."""
    pair = ([line("120.45")], [entry("120.46")])
    assert len(match(*pair, DEFAULT_POLICY).matches) == 1
    assert match(*pair, EXACT_ONLY_POLICY).matches == ()


# --------------------------------------------------------------------------------------
# Currency and date are hard filters
# --------------------------------------------------------------------------------------


def test_currency_mismatch_never_matches_however_close_the_amounts() -> None:
    """No conversion happens anywhere. Two amounts in different currencies are not near each
    other — they are incomparable, and the presentment and FX columns are not consulted."""
    settlement = line("100.00", currency="EUR")
    for amount in ("100.00", "100.01", "99.99"):
        assert match([settlement], [entry(amount, currency="USD")], DEFAULT_POLICY).matches == ()


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_a_candidate_inside_the_date_window_is_eligible(offset: int) -> None:
    ledger = entry("120.45", day=DAY + dt.timedelta(days=offset))
    assert len(match([line("120.45")], [ledger], DEFAULT_POLICY).matches) == 1


@pytest.mark.parametrize("offset", [-2, 2, 30])
def test_a_candidate_outside_the_date_window_is_not_considered(offset: int) -> None:
    """A hard filter, not a band: an out-of-window candidate is invisible to every rule."""
    ledger = entry("120.45", day=DAY + dt.timedelta(days=offset))
    outcome = match([line("120.45")], [ledger], DEFAULT_POLICY)
    assert outcome.matches == ()
    assert not outcome.ambiguous_line_ids


def test_the_date_window_is_configurable_and_zero_means_same_day() -> None:
    same_day_only = TolerancePolicy(amount=dict(DEFAULT_POLICY.amount), value_date_window_days=0)
    tomorrow = entry("120.45", day=DAY + dt.timedelta(days=1))
    assert len(match([line("120.45")], [tomorrow], DEFAULT_POLICY).matches) == 1
    assert match([line("120.45")], [tomorrow], same_day_only).matches == ()


# --------------------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------------------


def test_rule_precedence_is_declared_explicitly() -> None:
    assert RULE_PRECEDENCE == (MatchRule.EXACT_AMOUNT, MatchRule.AMOUNT_WITHIN_TOLERANCE)


def test_an_exact_candidate_beats_a_near_one() -> None:
    """Both are eligible under the tolerance rule; the exact rule settles it first."""
    settlement = line("120.45")
    exact, near = entry("120.45", ref="GL-exact"), entry("120.46", ref="GL-near")
    outcome = match([settlement], [exact, near], DEFAULT_POLICY)

    assert len(outcome.matches) == 1
    assert outcome.matches[0].entry_id == exact.id
    assert outcome.matches[0].rule is MatchRule.EXACT_AMOUNT


def test_precedence_does_not_depend_on_the_order_candidates_are_supplied() -> None:
    settlement = line("120.45")
    exact, near = entry("120.45"), entry("120.46")
    for candidates in ([exact, near], [near, exact]):
        outcome = match([settlement], candidates, DEFAULT_POLICY)
        assert outcome.matches[0].entry_id == exact.id


def test_a_line_settled_exactly_releases_nothing_for_a_near_miss_to_take() -> None:
    """The consumed entry leaves the pool, so a second line cannot then absorb it by tolerance."""
    first, second = line("120.45", number=1), line("120.46", number=2)
    only_entry = entry("120.45")
    outcome = match([first, second], [only_entry], DEFAULT_POLICY)

    # Both lines want the entry — first exactly, second within tolerance — so at the exact stage
    # the entry has one claimant and the pair is unique from both sides.
    assert len(outcome.matches) == 1
    assert outcome.matches[0].line_id == first.id
    assert second.id in outcome.unmatched_line_ids | outcome.ambiguous_line_ids


# --------------------------------------------------------------------------------------
# Ambiguity is refused, never guessed
# --------------------------------------------------------------------------------------


def test_two_equally_exact_candidates_produce_no_match() -> None:
    settlement = line("120.45")
    outcome = match([settlement], [entry("120.45"), entry("120.45")], DEFAULT_POLICY)

    assert outcome.matches == (), "picking one would be a guess with a financial consequence"
    assert outcome.ambiguous_line_ids == frozenset({settlement.id})


def test_two_equally_near_candidates_produce_no_match() -> None:
    settlement = line("120.45")
    outcome = match([settlement], [entry("120.44"), entry("120.46")], DEFAULT_POLICY)
    assert outcome.matches == ()
    assert outcome.ambiguous_line_ids == frozenset({settlement.id})


def test_two_lines_competing_for_one_entry_match_nothing() -> None:
    """Mutual uniqueness. A greedy matcher would give it to whichever line came first, and the
    answer would then depend on query order — which is a defect even when it looks reasonable."""
    first, second = line("120.45", number=1), line("120.45", number=2)
    outcome = match([first, second], [entry("120.45")], DEFAULT_POLICY)

    assert outcome.matches == ()
    assert outcome.ambiguous_line_ids == frozenset({first.id, second.id})


def test_ambiguity_under_the_exact_rule_is_not_rescued_by_the_tolerance_rule() -> None:
    """Its exact candidates are a subset of its tolerance candidates, so it stays ambiguous —
    precedence must never launder a guess into a lower-priority rule."""
    settlement = line("120.45")
    outcome = match([settlement], [entry("120.45"), entry("120.45")], DEFAULT_POLICY)
    assert outcome.matches == ()
    assert settlement.id in outcome.ambiguous_line_ids


def test_distinct_amounts_pair_up_cleanly() -> None:
    """The complement: mutual uniqueness must not refuse an unambiguous set."""
    a, b = line("10.00", number=1), line("20.00", number=2)
    ea, eb = entry("10.00"), entry("20.00")
    outcome = match([a, b], [ea, eb], DEFAULT_POLICY)

    assert {(m.line_id, m.entry_id) for m in outcome.matches} == {(a.id, ea.id), (b.id, eb.id)}


# --------------------------------------------------------------------------------------
# Order independence
# --------------------------------------------------------------------------------------


def test_the_result_does_not_depend_on_input_order() -> None:
    """Every permutation of a small set must produce the same pairing.

    This is the property the mutual-uniqueness rule exists to give. A greedy implementation passes
    every other test in this file and fails this one.
    """
    lines = [line("10.00", number=1), line("20.00", number=2), line("10.01", number=3)]
    entries = [entry("10.00"), entry("20.00"), entry("30.00")]
    expected = {
        (m.line_id, m.entry_id, m.rule) for m in match(lines, entries, DEFAULT_POLICY).matches
    }
    assert expected, "the fixture must produce at least one match for this to mean anything"

    for permuted_lines in itertools.permutations(lines):
        for permuted_entries in itertools.permutations(entries):
            outcome = match(list(permuted_lines), list(permuted_entries), DEFAULT_POLICY)
            assert {(m.line_id, m.entry_id, m.rule) for m in outcome.matches} == expected


def test_repeated_execution_is_stable() -> None:
    lines = [line("10.00", number=1), line("10.00", number=2)]
    entries = [entry("10.00"), entry("10.01")]
    first = match(lines, entries, DEFAULT_POLICY)
    for _ in range(5):
        again = match(lines, entries, DEFAULT_POLICY)
        assert again == first


def test_every_line_is_accounted_for_exactly_once() -> None:
    """Matched, ambiguous or unmatched — a line cannot fall out of the report."""
    lines = [line("10.00", number=1), line("10.00", number=2), line("99.99", number=3)]
    outcome = match(lines, [entry("10.00")], DEFAULT_POLICY)

    matched = {m.line_id for m in outcome.matches}
    assert matched | outcome.ambiguous_line_ids | outcome.unmatched_line_ids == {
        line_.id for line_ in lines
    }
    assert not matched & outcome.ambiguous_line_ids
    assert not matched & outcome.unmatched_line_ids
    assert not outcome.ambiguous_line_ids & outcome.unmatched_line_ids


# --------------------------------------------------------------------------------------
# The policy object
# --------------------------------------------------------------------------------------


def test_the_default_policy_declares_one_minor_unit_per_currency() -> None:
    """Pinned as literals. A change to a tolerance band must be a deliberate, visible act."""
    assert dict(DEFAULT_POLICY.amount) == {
        "EUR": decimal.Decimal("0.01"),
        "USD": decimal.Decimal("0.01"),
        "GBP": decimal.Decimal("0.01"),
        "JPY": decimal.Decimal("1"),
        "BHD": decimal.Decimal("0.001"),
    }
    assert DEFAULT_POLICY.value_date_window_days == 1


def test_every_declared_band_is_an_exact_decimal() -> None:
    for currency, band in DEFAULT_POLICY.amount.items():
        assert isinstance(band, decimal.Decimal), currency
        assert band > 0, currency


def test_a_policy_with_a_negative_band_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        TolerancePolicy(amount={"EUR": decimal.Decimal("-0.01")}, value_date_window_days=1)
    with pytest.raises(ValueError, match="must not be negative"):
        TolerancePolicy(amount={}, value_date_window_days=-1)


# --------------------------------------------------------------------------------------
# Against the committed corpus
# --------------------------------------------------------------------------------------


def _corpus_candidates() -> tuple[list[CandidateLine], list[CandidateEntry], dict[str, str]]:
    records = json.loads((CORPUS / "records.json").read_text(encoding="utf-8"))
    lines: list[CandidateLine] = []
    scenario_of: dict[str, str] = {}
    for batch in records["batches"]:
        for row in batch["lines"]:
            lines.append(
                CandidateLine(
                    id=uuid.UUID(row["id"]),
                    line_number=row["line_number"],
                    amount=decimal.Decimal(row["amount"]),
                    currency=row["currency"],
                    value_date=dt.date.fromisoformat(row["value_date"]),
                )
            )
            scenario_of[row["id"]] = row["scenario_id"]
    entries = [
        CandidateEntry(
            id=uuid.UUID(row["id"]),
            external_ref=row["external_ref"],
            amount=decimal.Decimal(row["amount"]),
            currency=row["currency"],
            booked_on=dt.datetime.fromisoformat(row["booked_at"]).date(),
        )
        for row in records["ledger_entries"]
    ]
    return lines, entries, scenario_of


def test_the_canonical_corpus_matches_exactly_the_scenarios_it_should() -> None:
    """Per line, not per scenario — and the distinction matters.

    A scenario labelled ``residual`` describes the *condition*, not every line it produces.
    SC-006 constructs a chargeback and its later reversal against a single ledger debit: the
    chargeback line genuinely corresponds to that debit and is matched, and the reversal is the
    residual. Asserting that no line of a residual scenario ever matches would be asserting
    something false, and would have pushed the matcher into agreeing with it.
    """
    lines, entries, scenario_of = _corpus_candidates()
    outcome = match(lines, entries, DEFAULT_POLICY)
    matched_scenarios = sorted(scenario_of[str(m.line_id)] for m in outcome.matches)

    assert matched_scenarios == [
        "SC-001-exact-match",
        "SC-002-reference-mismatch",
        "SC-006-chargeback-reversal",
        "SC-008-cross-period-refund",
    ]
    assert all(m.rule is MatchRule.EXACT_AMOUNT for m in outcome.matches)
    assert not outcome.ambiguous_line_ids


def test_the_near_miss_scenario_resolves_according_to_the_approved_policy() -> None:
    """SC-003 is labelled ``tolerance_policy_dependent`` because M1.3 declined to predict it.

    The expected outcome is derived here from the policy and the actual drawn amounts, not
    hardcoded — the corpus draws a drift of one to three minor units, so a different seed would
    legitimately give a different answer.
    """
    lines, entries, scenario_of = _corpus_candidates()
    outcome = match(lines, entries, DEFAULT_POLICY)

    near_miss = [line_ for line_ in lines if scenario_of[str(line_.id)].startswith("SC-003")]
    assert len(near_miss) == 1
    settlement = near_miss[0]

    closest = min(
        (
            abs(settlement.amount - candidate.amount)
            for candidate in entries
            if candidate.currency == settlement.currency
            and DEFAULT_POLICY.within_window(settlement.value_date, candidate.booked_on)
        ),
        default=None,
    )
    assert closest is not None
    band = DEFAULT_POLICY.band(settlement.currency)
    assert band is not None
    should_match = closest <= band

    actually_matched = settlement.id in {m.line_id for m in outcome.matches}
    assert actually_matched == should_match
    # For the committed seed the gap is two minor units, so the policy leaves it residual.
    assert closest == decimal.Decimal("0.02")
    assert not should_match


def test_the_bulk_profile_is_cleared_deterministically_without_a_model_call() -> None:
    """The plan's exit criterion: the matcher clears the bulk.

    Measured on the ``bulk`` profile, not the canonical corpus. Canonical holds exactly one
    instance of every condition — three of its twelve scenarios are matched-intent — so its
    clearance rate reports the shape of the catalogue rather than the matcher's reach. The bulk
    profile carries the declared distribution, where matched-intent lines are 81.5% of the mix.
    """
    result = generate(20260829, Profile.BULK, 200)
    lines = [
        CandidateLine(row.id, row.line_number, row.amount, row.currency, row.value_date)
        for batch in result.corpus.batches
        for row in batch.lines
    ]
    entries = [
        CandidateEntry(row.id, row.external_ref, row.amount, row.currency, row.booked_at.date())
        for row in result.corpus.ledger_entries
    ]
    outcome = match(lines, entries, DEFAULT_POLICY)
    cleared = len(outcome.matches) / len(lines)

    assert cleared > 0.75, f"only {cleared:.1%} cleared; the matcher must clear the bulk"
    by_tolerance = [m for m in outcome.matches if m.tolerance_applied is not None]
    assert by_tolerance, "the tolerance band must be exercised at volume"
    assert len(by_tolerance) < len(outcome.matches) // 4, "tolerance must not be doing the bulk"


# --------------------------------------------------------------------------------------
# Scope: this package must not become M2.3
# --------------------------------------------------------------------------------------


def _matching_sources() -> list[tuple[str, ast.Module]]:
    paths = sorted(MATCHING_ROOT.rglob("*.py"))
    assert len(paths) >= 4, "the guards must be walking real files"
    return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def test_no_float_appears_anywhere_in_the_matching_package() -> None:
    """Every comparison is Decimal. A float difference of 0.01 is not 0.01."""
    for name, tree in _matching_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "float", f"{name} references float"
            if isinstance(node, ast.Attribute):
                assert node.attr != "float", f"{name} references float"


def test_the_matching_package_imports_nothing_that_could_make_it_a_classifier() -> None:
    """An allowlist.

    ``db.control`` was banned outright until M2.3, on the reasoning that matching has no business
    touching the exception, proposal or approval tables. The ban has been narrowed rather than
    lifted: matching now has one legitimate question for that module — *is this line already under
    exception control* — because a line M2.3 has raised an exception for must not be silently
    matched afterwards (ADR-044). What it must still never do is create or alter that control, and
    the two tests below say so directly instead of leaving a blanket import ban to imply it.
    """
    permitted_stdlib = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "uuid",
        "typing",
    }
    permitted_third_party = {"sqlalchemy"}
    permitted_internal = {
        "ledger_exception_control_plane.matching",
        "ledger_exception_control_plane.db.models",
        "ledger_exception_control_plane.db.control",
    }
    for name, tree in _matching_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("ledger_exception_control_plane"):
                    assert any(module.startswith(p) for p in permitted_internal), (
                        f"{name} imports {module}, which is outside the matching boundary"
                    )
                else:
                    assert module.split(".")[0] in permitted_stdlib | permitted_third_party, (
                        f"{name} imports {module}, which is not on the matching allowlist"
                    )


def test_the_matching_package_never_references_classification_concepts() -> None:
    """Names M2.3 and later own. An exception created here would ship the answer with the
    question."""
    forbidden = {
        "ExceptionClassification",
        "ExceptionStatus",
        "Evidence",
        "TreatmentProposal",
        "TreatmentCode",
        "Approval",
        "Adjustment",
        "classification",
        "intended_classification",
        "scenario_id",
    }
    for name, tree in _matching_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden, f"{name} references {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, f"{name} references {node.attr}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in forbidden, f"{name} contains the literal {node.value!r}"


def test_the_matching_package_only_reads_the_exception_table_never_writes_it() -> None:
    """The narrowed ban, stated as the rule it actually is.

    Matching may observe that a line is under exception control. It may not raise one, resolve one,
    or edit one — creating an exception here would be M2.3 done in the wrong module, and the
    classification would arrive with no rule, no ruleset version and no residual analysis behind
    it. Checked by walking every write-shaped call rather than by trusting the import allowlist,
    which now permits the module the table lives in.
    """
    writers = {"insert", "pg_insert", "update", "delete"}
    for name, tree in _matching_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called not in writers:
                continue
            for argument in node.args:
                assert not (isinstance(argument, ast.Name) and argument.id == "ExceptionRecord"), (
                    f"{name} writes to the exception table via {called}()"
                )


def test_the_matching_package_reads_no_classification_column() -> None:
    """Reading *that* an exception exists is eligibility; reading *what it says* is classification.

    Only the identifying column may be touched. If matching ever consulted the class, the status or
    the assigning rule, it would be branching on M2.3's decision — and a matcher whose behaviour
    depends on how a residual was classified is no longer a deterministic matcher.
    """
    permitted = {"id", "settlement_line_id"}
    for name, tree in _matching_sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ExceptionRecord"
            ):
                assert node.attr in permitted, f"{name} reads ExceptionRecord.{node.attr}"


def test_production_matching_does_not_depend_on_the_fixture_package() -> None:
    """The matcher must decide from settlement and ledger data alone. Reading a construction label
    would make every test that uses the corpus circular."""
    for name, tree in _matching_sources():
        source = ast.dump(tree)
        for label in ("fixtures", "intended_classification", "scenario_id", "MatchIntent"):
            assert label not in source, f"{name} references {label}"


def test_the_matcher_sees_no_free_text_field() -> None:
    """Containment by construction: the candidate types carry no memo, reference or description,
    so no rule can be written against free text however tempting it looks."""
    forbidden = {"memo", "description", "psp_reference", "merchant_reference", "rationale"}
    for candidate in (CandidateLine, CandidateEntry):
        assert not (set(candidate.__dataclass_fields__) & forbidden), candidate.__name__


# ======================================================================================
# Regression: an unresolved higher-tier contest must not be settled by a lower tier
#
# The failure this guards against is subtle and would look like an improvement: a line that
# cannot be matched exactly gets matched "at least approximately". That is precedence
# inverted — the system would resolve an ambiguity by *weakening* the rule that detected it,
# and would consume a ledger entry that a stronger claim was still contesting. Because
# match_result is unique on the ledger entry (ADR-024), that consumption is permanent.
# ======================================================================================


def test_a_line_ambiguous_at_the_exact_tier_is_not_rescued_by_a_tolerance_candidate() -> None:
    """Two exact candidates plus one tolerance-only candidate: the line matches nothing.

    The tolerance candidate is unique *within its tier*, and a matcher that dropped the exact
    tier's unresolved contest before moving on would seize on that uniqueness and match it.
    """
    settlement = line("100.00")
    exact_a = entry("100.00", ref="GL-EXACT-A")
    exact_b = entry("100.00", ref="GL-EXACT-B")
    near = entry("100.01", ref="GL-NEAR")

    outcome = match([settlement], [exact_a, exact_b, near], DEFAULT_POLICY)

    assert outcome.matches == (), "an exact-tier ambiguity must not be resolved by tolerance"
    assert settlement.id in outcome.ambiguous_line_ids


def test_an_entry_contested_at_the_exact_tier_is_not_taken_by_a_lower_tier() -> None:
    """The other half of the rule, and the half that would be a real defect if omitted.

    Two lines contest one entry exactly. A third line would match that entry uniquely under
    tolerance. Withdrawing only the ambiguous *lines* would release the entry and let the
    tolerance match take it — a weaker rule consuming what a stronger one was still arguing
    over, and irreversibly, because the entry can never be released again.
    """
    first, second = line("100.00", number=1), line("100.00", number=2)
    tolerance_only = line("100.01", number=3)
    contested = entry("100.00", ref="GL-CONTESTED")

    outcome = match([first, second, tolerance_only], [contested], DEFAULT_POLICY)

    assert outcome.matches == ()
    assert outcome.ambiguous_line_ids == frozenset({first.id, second.id})
    # The third line is not itself ambiguous — it has no available candidate at all, because the
    # only one it could have used is locked in someone else's unresolved contest.
    assert tolerance_only.id in outcome.unmatched_line_ids


def test_the_symmetric_case_one_entry_two_exact_lines_plus_a_tolerance_line() -> None:
    """Ambiguity on the ledger side blocks the tier as surely as ambiguity on the settlement
    side does."""
    exact_one, exact_two = line("250.00", number=1), line("250.00", number=2)
    near = line("250.01", number=3)
    only = entry("250.00", ref="GL-ONLY")

    outcome = match([exact_one, exact_two, near], [only], DEFAULT_POLICY)
    assert outcome.matches == ()
    assert not any(m.entry_id == only.id for m in outcome.matches)


def test_exact_tier_ambiguity_survives_every_input_order() -> None:
    """The block must not be an artefact of the order the contest happened to be discovered in."""
    settlement = line("100.00")
    candidates = [
        entry("100.00", ref="GL-A"),
        entry("100.00", ref="GL-B"),
        entry("100.01", ref="GL-C"),
    ]
    for permuted in itertools.permutations(candidates):
        outcome = match([settlement], list(permuted), DEFAULT_POLICY)
        assert outcome.matches == ()
        assert settlement.id in outcome.ambiguous_line_ids


def test_exact_tier_ambiguity_is_stable_across_repeated_execution() -> None:
    settlement = line("100.00")
    candidates = [
        entry("100.00", ref="GL-A"),
        entry("100.00", ref="GL-B"),
        entry("100.01", ref="GL-C"),
    ]
    first = match([settlement], candidates, DEFAULT_POLICY)
    for _ in range(5):
        assert match([settlement], candidates, DEFAULT_POLICY) == first


def test_an_unambiguous_line_still_reaches_the_tolerance_tier() -> None:
    """The complement. Blocking must remove only what is genuinely contested.

    A matcher that withdrew too much would pass every test above and quietly stop matching.
    """
    contested = line("100.00", number=1)
    clean = line("500.00", number=2)
    outcome = match(
        [contested, clean],
        [
            entry("100.00", ref="GL-A"),
            entry("100.00", ref="GL-B"),
            entry("500.01", ref="GL-CLEAN"),
        ],
        DEFAULT_POLICY,
    )

    assert len(outcome.matches) == 1
    assert outcome.matches[0].line_id == clean.id
    assert outcome.matches[0].rule is MatchRule.AMOUNT_WITHIN_TOLERANCE
    assert contested.id in outcome.ambiguous_line_ids
