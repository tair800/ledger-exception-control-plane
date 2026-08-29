"""Fixture-generator tests — deterministic, Docker-free.

The generator's whole value is that it is reproducible and that it stays out of M2's way.
Both are properties that decay silently, so both are asserted rather than assumed: the
determinism tests regenerate and compare, and the scope tests walk this package's AST rather
than trusting a reviewer to notice a matcher appearing inside it.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import hashlib
import json
import math
import pathlib
import re
from fractions import Fraction

import pytest
from pydantic import SecretStr
from sqlalchemy import String

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_MAX_SCALE,
    Base,
)
from ledger_exception_control_plane.db.control import ExceptionClassification
from ledger_exception_control_plane.fixtures import fixtures_module_paths
from ledger_exception_control_plane.fixtures.__main__ import DEFAULT_SEED, main, write_corpus
from ledger_exception_control_plane.fixtures.catalogue import (
    CATALOGUE,
    TOTAL_WEIGHT,
    BuiltScenario,
    declared_classification_weights,
)
from ledger_exception_control_plane.fixtures.determinism import FIXTURE_EPOCH, Draw, fixture_uuid
from ledger_exception_control_plane.fixtures.generator import (
    BULK_DEFAULT_INSTANCES,
    MANIFEST_PATH,
    GeneratedCorpus,
    _instance_counts,
    _reject_duplicates,
    generate,
    residual_mix,
)
from ledger_exception_control_plane.fixtures.invalid import INVALID_FIXTURES
from ledger_exception_control_plane.fixtures.loader import (
    CorpusIntegrityError,
    UnsafeTargetError,
    assert_target_is_disposable,
    corpus_files,
    read_corpus,
)
from ledger_exception_control_plane.fixtures.money import BY_CODE, EUR, JPY, money
from ledger_exception_control_plane.fixtures.schema import MatchIntent, Profile
from ledger_exception_control_plane.fixtures.serialise import content_digest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMITTED_CORPUS = REPO_ROOT / "fixtures" / "canonical"

#: The seed the committed corpus is generated with. Duplicated from the CLI deliberately: if
#: the default changes without the corpus being regenerated, the drift test should fail rather
#: than silently follow the new default.
COMMITTED_SEED = 20260829


def _scale(value: decimal.Decimal) -> int:
    """Fractional digits of an exact Decimal.

    ``as_tuple().exponent`` is typed as ``int | Literal['n', 'N', 'F']`` because it is one of
    those strings for NaN and infinity. Every amount here is finite by construction, and the
    assertion says so rather than casting the problem away.
    """
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int), f"{value} is not a finite decimal"
    return -exponent


# --------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------


def test_the_same_seed_produces_byte_identical_output() -> None:
    """The plan's exit criterion, stated as bytes rather than as semantics."""
    first = generate(COMMITTED_SEED, Profile.CANONICAL)
    second = generate(COMMITTED_SEED, Profile.CANONICAL)
    assert first.files == second.files
    assert first.manifest.content_sha256 == second.manifest.content_sha256


def test_the_committed_corpus_matches_the_generator() -> None:
    """Regenerating must reproduce what is checked in, byte for byte.

    This is the test that makes the committed artifacts trustworthy: without it they are
    just files someone once produced, and nothing would notice them drifting away from the
    code that claims to generate them.
    """
    regenerated = generate(COMMITTED_SEED, Profile.CANONICAL).files
    committed = corpus_files(COMMITTED_CORPUS)
    assert set(committed) == set(regenerated)
    differing = sorted(path for path in committed if committed[path] != regenerated[path])
    assert not differing, f"committed corpus has drifted: {differing}"


def test_a_different_seed_changes_the_data_that_should_vary() -> None:
    other = generate(COMMITTED_SEED + 1, Profile.CANONICAL)
    canonical_corpus = generate(COMMITTED_SEED, Profile.CANONICAL)

    assert other.manifest.content_sha256 != canonical_corpus.manifest.content_sha256
    references = {
        line.psp_reference for batch in canonical_corpus.corpus.batches for line in batch.lines
    }
    other_references = {
        line.psp_reference for batch in other.corpus.batches for line in batch.lines
    }
    assert not references & other_references, "references must not repeat across seeds"

    amounts = {line.amount for batch in canonical_corpus.corpus.batches for line in batch.lines}
    other_amounts = {line.amount for batch in other.corpus.batches for line in batch.lines}
    assert amounts != other_amounts


def test_a_different_seed_does_not_change_the_scenario_structure() -> None:
    """Amounts and references vary with the seed. The corpus's *shape* must not.

    A seed change that silently altered which conditions the corpus contains, or how many
    lines each produces, would make every scenario-addressed test seed-dependent — and the
    failure would look like a logic bug rather than a fixture one.
    """
    baseline = generate(COMMITTED_SEED, Profile.CANONICAL)
    other = generate(COMMITTED_SEED + 7, Profile.CANONICAL)

    def shape(corpus: GeneratedCorpus) -> list[tuple[str, str, str, str | None, int, int]]:
        return [
            (
                scenario.scenario_id,
                scenario.kind.value,
                scenario.intent.value,
                scenario.intended_classification.value
                if scenario.intended_classification
                else None,
                len(scenario.settlement_references),
                len(scenario.ledger_references),
            )
            for scenario in corpus.scenarios.scenarios
        ]

    assert shape(baseline) == shape(other)
    assert baseline.manifest.residual_mix == other.manifest.residual_mix
    assert baseline.manifest.settlement_line_count == other.manifest.settlement_line_count
    assert [batch.raw_payload_path for batch in baseline.corpus.batches] == [
        batch.raw_payload_path for batch in other.corpus.batches
    ]


def test_value_dates_and_timestamps_do_not_move_with_the_seed() -> None:
    """Dates are structure, not data. Only amounts and references are seeded."""
    baseline = generate(COMMITTED_SEED, Profile.CANONICAL)
    other = generate(COMMITTED_SEED + 11, Profile.CANONICAL)

    assert [(batch.raw_payload_path, batch.received_at) for batch in baseline.corpus.batches] == [
        (batch.raw_payload_path, batch.received_at) for batch in other.corpus.batches
    ]
    assert [line.value_date for batch in baseline.corpus.batches for line in batch.lines] == [
        line.value_date for batch in other.corpus.batches for line in batch.lines
    ]


def test_every_timestamp_is_anchored_to_the_fixture_epoch() -> None:
    """No wall clock anywhere. Every instant is timezone-aware and derived from the anchor."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    instants = [batch.received_at for batch in result.corpus.batches]
    instants += [entry.booked_at for entry in result.corpus.ledger_entries]
    for instant in instants:
        assert instant.tzinfo is not None, "a naive timestamp in a financial fixture is a bug"
        assert FIXTURE_EPOCH.date() <= instant.date() <= FIXTURE_EPOCH.date().replace(month=9)


def test_ordering_is_total_and_stable() -> None:
    """Line numbers and file order are assigned, never inherited from iteration order."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    for batch in result.corpus.batches:
        numbers = [line.line_number for line in batch.lines]
        assert numbers == list(range(1, len(numbers) + 1))
    paths = [batch.raw_payload_path for batch in result.corpus.batches]
    assert paths == sorted(paths)
    refs = [entry.external_ref for entry in result.corpus.ledger_entries]
    assert refs == sorted(refs)


def test_draws_are_position_independent() -> None:
    """A label's value must not depend on how many draws preceded it.

    This is the property that lets a scenario be added to the catalogue without perturbing
    every other scenario's data — and therefore without a one-line change producing a
    thousand-line corpus diff.
    """
    draw = Draw(COMMITTED_SEED, "domain")
    first = draw.integer("alpha", 0, 10**9)
    draw.integer("beta", 0, 10**9)
    draw.integer("gamma", 0, 10**9)
    assert draw.integer("alpha", 0, 10**9) == first
    assert Draw(COMMITTED_SEED, "domain").integer("alpha", 0, 10**9) == first
    assert Draw(COMMITTED_SEED, "other").integer("alpha", 0, 10**9) != first


# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------


def test_every_fixture_identifier_is_version_5() -> None:
    """Deterministic, and visibly not the version 4 the application generates (ADR-022).

    A fixture row must never be mistakable for one the system produced, and the UUID version
    field carries that distinction without needing a naming convention anyone could forget.
    """
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    identifiers = [batch.id for batch in result.corpus.batches]
    identifiers += [line.id for batch in result.corpus.batches for line in batch.lines]
    identifiers += [entry.id for entry in result.corpus.ledger_entries]
    assert identifiers, "the corpus must contain identifiers for this test to mean anything"
    for identifier in identifiers:
        assert identifier.version == 5, f"{identifier} is not a deterministic fixture identifier"


def test_identifiers_are_unique_across_the_corpus() -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    identifiers = [batch.id for batch in result.corpus.batches]
    identifiers += [line.id for batch in result.corpus.batches for line in batch.lines]
    identifiers += [entry.id for entry in result.corpus.ledger_entries]
    assert len(identifiers) == len(set(identifiers))


def test_identifiers_are_stable_and_name_derived() -> None:
    assert fixture_uuid("a", "b") == fixture_uuid("a", "b")
    assert fixture_uuid("a", "b") != fixture_uuid("b", "a")
    with pytest.raises(ValueError, match="at least one name part"):
        fixture_uuid()


# --------------------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------------------


def _all_amounts(result: GeneratedCorpus) -> list[decimal.Decimal]:
    amounts = [line.amount for batch in result.corpus.batches for line in batch.lines]
    amounts += [entry.amount for entry in result.corpus.ledger_entries]
    return amounts


@pytest.mark.parametrize("profile", list(Profile))
def test_every_amount_is_an_exact_decimal_within_the_schema_contract(profile: Profile) -> None:
    """The corpus must not be able to produce a value M1.1 would reject."""
    result = generate(COMMITTED_SEED, profile, BULK_DEFAULT_INSTANCES)
    amounts = _all_amounts(result)
    assert amounts
    for amount in amounts:
        assert isinstance(amount, decimal.Decimal), "money must never be binary floating point"
        assert amount.is_finite()
        assert _scale(amount) <= MONEY_MAX_SCALE
        assert abs(amount) < MONEY_MAGNITUDE_EXCLUSIVE_BOUND


def test_every_amount_uses_its_currency_s_real_minor_unit() -> None:
    """A JPY amount with decimal places would be a fiction about the currency."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    for batch in result.corpus.batches:
        for line in batch.lines:
            assert _scale(line.amount) == BY_CODE[line.currency].minor_digits
    for entry in result.corpus.ledger_entries:
        assert _scale(entry.amount) == BY_CODE[entry.currency].minor_digits


def test_every_amount_carries_an_explicit_valid_currency() -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    codes = {line.currency for batch in result.corpus.batches for line in batch.lines}
    codes |= {entry.currency for entry in result.corpus.ledger_entries}
    assert codes, "the corpus must carry currencies for this test to mean anything"
    for code in codes:
        assert re.fullmatch(r"[A-Z]{3}", code), f"{code} is not an ISO 4217 alphabetic code"
        assert code in BY_CODE


def test_money_construction_is_exact_and_never_rounds() -> None:
    assert money(12345, EUR) == decimal.Decimal("123.45")
    assert str(money(12345, EUR)) == "123.45"
    assert str(money(-5, EUR)) == "-0.05"
    assert str(money(12345, JPY)) == "12345"
    assert str(money(0, EUR)) == "0.00"
    with pytest.raises(ValueError, match="magnitude bound"):
        money(10**18, EUR)


def test_no_float_appears_in_any_serialised_amount() -> None:
    """JSON numbers become floats on the way back in. Amounts are strings, deliberately."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    payload = json.loads(result.files["records.json"])
    for batch in payload["batches"]:
        for line in batch["lines"]:
            assert isinstance(line["amount"], str)
    for entry in payload["ledger_entries"]:
        assert isinstance(entry["amount"], str)


# --------------------------------------------------------------------------------------
# Conformance with the live schema
# --------------------------------------------------------------------------------------


def _string_limit(table: str, column: str) -> int:
    column_type = Base.metadata.tables[table].columns[column].type
    assert isinstance(column_type, String)
    assert column_type.length is not None
    return column_type.length


def test_generated_records_fit_the_columns_they_will_be_loaded_into() -> None:
    """Read the limits from the live metadata, so this cannot drift from the schema."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    for batch in result.corpus.batches:
        assert re.fullmatch(r"[0-9a-f]{64}", batch.content_hash)
        assert len(batch.source) <= _string_limit("settlement_batch", "source")
        for line in batch.lines:
            assert line.line_number > 0
            assert len(line.psp_reference) <= _string_limit("settlement_line", "psp_reference")
            if line.merchant_reference is not None:
                assert len(line.merchant_reference) <= _string_limit(
                    "settlement_line", "merchant_reference"
                )
    for entry in result.corpus.ledger_entries:
        assert len(entry.external_ref) <= _string_limit("ledger_entry", "external_ref")
        assert len(entry.account_code) <= _string_limit("ledger_entry", "account_code")


def test_business_keys_that_carry_a_unique_constraint_are_unique() -> None:
    """``ledger_entry.external_ref`` is unique and ``(batch, line_number)`` is unique."""
    result = generate(COMMITTED_SEED, Profile.BULK, BULK_DEFAULT_INSTANCES)
    refs = [entry.external_ref for entry in result.corpus.ledger_entries]
    assert len(refs) == len(set(refs))
    for batch in result.corpus.batches:
        pairs = [(batch.id, line.line_number) for line in batch.lines]
        assert len(pairs) == len(set(pairs))


def test_a_batch_content_hash_is_the_hash_of_its_own_file() -> None:
    """FR-1's re-delivery guard is built on this, so it must not merely look plausible."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    for batch in result.corpus.batches:
        payload = result.files[batch.raw_payload_path]
        assert batch.content_hash == hashlib.sha256(payload).hexdigest()


def test_every_reference_in_the_metadata_resolves_to_real_data() -> None:
    """Scenario metadata that pointed at nothing would be provenance for nothing."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    settlement = {line.psp_reference for batch in result.corpus.batches for line in batch.lines}
    ledger = {entry.external_ref for entry in result.corpus.ledger_entries}
    for scenario in result.scenarios.scenarios:
        assert set(scenario.settlement_references) <= settlement, scenario.scenario_id
        assert set(scenario.ledger_references) <= ledger, scenario.scenario_id


def test_every_record_names_a_scenario_that_exists() -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    known = {scenario.scenario_id for scenario in result.scenarios.scenarios}
    for batch in result.corpus.batches:
        for line in batch.lines:
            assert line.scenario_id in known
    for entry in result.corpus.ledger_entries:
        assert entry.scenario_id in known


# --------------------------------------------------------------------------------------
# Scenario coverage and the declared mix
# --------------------------------------------------------------------------------------


def test_the_canonical_corpus_holds_exactly_one_of_every_scenario() -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    ids = [scenario.scenario_id for scenario in result.scenarios.scenarios]
    assert ids == [entry.scenario_id for entry in CATALOGUE]
    assert len(ids) == len(set(ids))


def test_every_taxonomy_class_is_represented() -> None:
    """FR-4's taxonomy is closed, and a class with no fixture is a class nobody can test."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    covered = {
        scenario.intended_classification
        for scenario in result.scenarios.scenarios
        if scenario.intended_classification is not None
    }
    assert covered == set(ExceptionClassification)


def test_every_scenario_explains_itself() -> None:
    """The plan requires each scenario to be explicable. Empty prose would defeat that."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    for scenario in result.scenarios.scenarios:
        assert len(scenario.why_it_exists) > 60, scenario.scenario_id
        assert scenario.distinguishing_fields, scenario.scenario_id
        assert scenario.settlement_references, scenario.scenario_id


def test_the_awkward_cases_the_plan_names_all_exist() -> None:
    """Missing references, ambiguous memos and cross-period refunds, named explicitly."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    awkwardness = {
        value for scenario in result.scenarios.scenarios for value in scenario.awkwardness
    }
    assert "missing_merchant_reference" in awkwardness
    assert "ambiguous_memo" in awkwardness
    assert "cross_period_dates" in awkwardness

    lines = [line for batch in result.corpus.batches for line in batch.lines]
    assert any(line.merchant_reference is None for line in lines)
    periods = {(line.value_date.year, line.value_date.month) for line in lines}
    assert len(periods) > 1, "a cross-period case must actually span periods"


def test_a_residual_scenario_always_declares_its_intended_classification() -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    for scenario in result.scenarios.scenarios:
        if scenario.intent is MatchIntent.RESIDUAL:
            assert scenario.intended_classification is not None, scenario.scenario_id
        else:
            assert scenario.intended_classification is None, scenario.scenario_id


def test_the_bulk_mix_matches_the_declared_distribution_exactly() -> None:
    """At the default size the counts are the declared weights, so the mix is exact.

    The weights are a design parameter of the synthetic corpus, not a measurement of a real
    settlement feed — but they are a *declared* parameter, and a generator that quietly
    produced a different mix would invalidate every later measurement that cites it.
    """
    result = generate(COMMITTED_SEED, Profile.BULK, BULK_DEFAULT_INSTANCES)
    assert result.manifest.scenario_count == BULK_DEFAULT_INSTANCES == TOTAL_WEIGHT

    expected = {
        "matched": 147 + 16,
        "tolerance_policy_dependent": 12,
        "partial_capture": 6 + 1,
        "fee_split": 4,
        "chargeback_reversal": 4,
        "fx_rounding": 4,
        "cross_period_refund": 2,
        "unclassified": 2 + 1 + 1,
    }
    assert result.manifest.residual_mix == dict(sorted(expected.items()))
    assert sum(expected.values()) == TOTAL_WEIGHT


def test_a_bulk_corpus_smaller_than_the_catalogue_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        generate(COMMITTED_SEED, Profile.BULK, 3)


def test_a_small_bulk_corpus_still_covers_every_scenario() -> None:
    """Proportional allocation would zero out the rare scenarios; the floor prevents it."""
    result = generate(COMMITTED_SEED, Profile.BULK, len(CATALOGUE))
    covered = {line.scenario_id for batch in result.corpus.batches for line in batch.lines}
    assert covered == {entry.scenario_id for entry in CATALOGUE}


def test_residual_mix_rejects_an_incoherent_scenario() -> None:
    """A residual scenario with no classification is a fixture with no ground truth."""
    broken = BuiltScenario(
        scenario_id="SC-999-broken",
        kind=CATALOGUE[0].kind,
        intent=MatchIntent.RESIDUAL,
        intended_classification=None,
        awkwardness=(),
        why_it_exists="x",
        distinguishing_fields=(),
        lines=(),
        entries=(),
    )
    assert dataclasses.is_dataclass(broken)
    with pytest.raises(ValueError, match="residual with no classification"):
        residual_mix((broken,))


# --------------------------------------------------------------------------------------
# Deliberately invalid artifacts
# --------------------------------------------------------------------------------------


def test_invalid_fixtures_are_labelled_and_never_loadable() -> None:
    """They exist for M2.1's quarantine path and must never enter the valid-load path."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    invalid_paths = {fixture.record.path for fixture in INVALID_FIXTURES}
    assert invalid_paths, "the corpus claims to carry invalid fixtures"

    loadable = {batch.raw_payload_path for batch in result.corpus.batches}
    assert not (loadable & invalid_paths), "an invalid artifact reached the loadable corpus"

    index = json.loads(result.files["invalid/index.json"])
    described = {fixture["path"] for fixture in index["fixtures"]}
    assert described == invalid_paths, "every invalid artifact must be labelled"
    for fixture in index["fixtures"]:
        assert fixture["defect"], fixture["path"]
        assert len(fixture["why_it_exists"]) > 40, fixture["path"]


def test_invalid_fixtures_really_are_invalid() -> None:
    """A file that would in fact load cleanly is not a quarantine fixture, it is a bug."""
    defects = {fixture.record.path: fixture.payload.decode("utf-8") for fixture in INVALID_FIXTURES}
    assert "120.12345" in defects["invalid/over-precise-amount.csv"]
    assert "currency" not in defects["invalid/missing-column.csv"].splitlines()[0]
    assert ",eur," in defects["invalid/bad-currency.csv"]
    assert ",EURO," in defects["invalid/bad-currency.csv"]
    assert "not-a-number" in defects["invalid/unparseable-amount.csv"]


# --------------------------------------------------------------------------------------
# Containment: the corpus carries no model output, and no secrets
# --------------------------------------------------------------------------------------


def test_the_corpus_contains_no_model_output_at_all() -> None:
    """No proposal, no approval, no adjustment — those are M3 and M5 outputs.

    A fixture corpus that shipped a treatment proposal would be shipping an answer, and the
    numeric-containment rule (CLAUDE.md rule 1) would then have a second place to leak from.
    """
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    payload = json.loads(result.files["records.json"])
    assert set(payload) == {
        "fixture_schema_version",
        "generator_version",
        "profile",
        "seed",
        "batches",
        "ledger_entries",
    }
    forbidden = (
        "treatment",
        "proposal",
        "confidence",
        "rationale",
        "approval",
        "adjustment",
        "operation_id",
        "evidence",
    )
    text = result.files["records.json"].decode("utf-8").lower()
    for term in forbidden:
        assert term not in text, f"records.json mentions {term}"


def test_no_artifact_contains_credential_shaped_material() -> None:
    """§17: no secret in code, fixtures or cassettes. Asserted over the real bytes."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    patterns = (
        re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
        re.compile(r"\bAKIA[0-9A-Z]{12,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"\b(?:password|passwd|api[_-]?key|secret|token)\s*[=:]\s*\S+", re.IGNORECASE),
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@"),
    )
    for path, payload in sorted(result.files.items()):
        text = payload.decode("utf-8")
        for pattern in patterns:
            assert not pattern.search(text), f"{path} matches {pattern.pattern}"


def test_no_artifact_contains_a_filesystem_path_or_machine_identity() -> None:
    """A corpus must be reproducible anywhere, so it cannot carry where it was made."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    manifest = json.loads(result.files[MANIFEST_PATH])
    assert set(manifest) == {
        "fixture_schema_version",
        "generator_version",
        "profile",
        "seed",
        "scenario_count",
        "batch_count",
        "settlement_line_count",
        "ledger_entry_count",
        "residual_mix",
        "content_sha256",
    }
    rendered = json.dumps(manifest)
    for marker in ("C:\\", "/home/", "/Users/", "\\\\", "://"):
        assert marker not in rendered


def test_the_manifest_digest_covers_every_other_artifact() -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    without_manifest = {
        path: payload for path, payload in result.files.items() if path != MANIFEST_PATH
    }
    assert result.manifest.content_sha256 == content_digest(without_manifest)


def test_the_digest_changes_when_any_artifact_changes() -> None:
    """Otherwise the manifest is decoration rather than an integrity claim."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    files = {path: payload for path, payload in result.files.items() if path != MANIFEST_PATH}
    baseline = content_digest(files)
    target = sorted(files)[0]
    assert content_digest({**files, target: files[target] + b"x"}) != baseline
    assert content_digest({f"renamed/{target}": files[target], **files}) != baseline


# --------------------------------------------------------------------------------------
# Scope: the generator must not become M2
# --------------------------------------------------------------------------------------


def _fixture_sources() -> list[tuple[str, ast.Module]]:
    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8"))) for path in fixtures_module_paths()
    ]


def test_the_generator_imports_nothing_that_could_make_it_a_matcher() -> None:
    """An allowlist, walked over the AST rather than grepped.

    The risk this guards is specific: a fixture generator that starts comparing settlement
    lines to ledger entries has quietly become the matcher M2.2 owns, and every test built on
    its output becomes circular. Nothing here may import a reconciliation module — and since
    those do not exist yet, the guard is an allowlist rather than a denylist, so it will fail
    when one is added rather than needing to be updated to notice.
    """
    permitted_stdlib = {
        "__future__",
        "argparse",
        "ast",
        "asyncio",
        "collections",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "hashlib",
        "io",
        "json",
        "pathlib",
        "re",
        "sys",
        "urllib",
        "uuid",
        "typing",
    }
    permitted_third_party = {"pydantic", "sqlalchemy"}
    permitted_internal_prefixes = (
        "ledger_exception_control_plane.fixtures",
        "ledger_exception_control_plane.db",
        "ledger_exception_control_plane.config",
    )

    for name, tree in _fixture_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".")[0]
                if module.startswith("ledger_exception_control_plane"):
                    assert module.startswith(permitted_internal_prefixes), (
                        f"{name} imports {module}, which is outside the fixture boundary"
                    )
                else:
                    assert root in permitted_stdlib | permitted_third_party, (
                        f"{name} imports {module}, which is not on the fixture allowlist"
                    )


def test_the_generator_never_reads_a_clock_or_a_random_source() -> None:
    """Determinism enforced structurally, not by convention.

    Attribute names rather than imports: ``datetime`` and ``uuid`` are legitimately imported
    here, and it is ``now()``, ``today()`` and ``uuid4()`` specifically that would make the
    corpus irreproducible.
    """
    forbidden = {"now", "today", "utcnow", "uuid4", "uuid1", "urandom", "monotonic", "perf_counter"}
    for name, tree in _fixture_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, f"{name} uses {node.attr}, which is not stable"
            if isinstance(node, ast.Import | ast.ImportFrom):
                imported = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for module in imported:
                    assert module.split(".")[0] not in {"random", "secrets", "time", "os"}, (
                        f"{name} imports {module}, which would make the corpus irreproducible"
                    )


def test_the_generator_contains_no_matching_or_classification_vocabulary() -> None:
    """A weaker check than the import guard, aimed at logic written inline rather than imported.

    Deliberately narrow: it looks for function *definitions* whose names claim to match,
    classify, normalise or parse. A helper called ``_classify_line`` inside the fixture package
    would be M2.3 wearing a fixture's clothes.
    """
    # Verbs, anchored at the start of the name. A scenario builder called ``_exact_match``
    # names a *condition* and is exactly what this package should contain; a function called
    # ``match_lines`` names an *action* and would be M2.2 living in the wrong module.
    verbs = (
        "match_",
        "classify",
        "normalise",
        "normalize",
        "parse_",
        "reconcile",
        "evaluate_tolerance",
        "apply_tolerance",
        "compute_",
        "propose_",
    )
    for name, tree in _fixture_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                lowered = node.name.lstrip("_").lower()
                assert not lowered.startswith(verbs), (
                    f"{name} defines {node.name}, which names an action M2 owns"
                )


# --------------------------------------------------------------------------------------
# Loader safety — the one part of this system that writes to a database
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["lecp_test", "lecp_demo", "lecp_fixtures"])
def test_the_loader_accepts_a_disposable_database(name: str) -> None:
    settings = Settings(postgres_dsn=SecretStr(f"postgresql://u:p@localhost:15432/{name}"))
    assert assert_target_is_disposable(settings) == name


@pytest.mark.parametrize(
    "name",
    [
        "lecp",  # the project's own primary database
        "postgres",  # the cluster's default
        "production",
        "lecp_test_2",  # close enough to look safe, and deliberately not accepted
        "",
    ],
)
def test_the_loader_refuses_anything_else(name: str) -> None:
    """A fixture loader that can be pointed somewhere by accident is a data-loss tool.

    The developer machine this was built on runs an unrelated PostgreSQL on the default port,
    so 'it will probably be configured correctly' is not a control.
    """
    settings = Settings(postgres_dsn=SecretStr(f"postgresql://u:p@localhost:15432/{name}"))
    with pytest.raises(UnsafeTargetError, match="refusing to load fixtures"):
        assert_target_is_disposable(settings)


def test_the_refusal_never_echoes_the_credential() -> None:
    """An error message is a log line waiting to happen (§17)."""
    settings = Settings(postgres_dsn=SecretStr("postgresql://someone:hunter2@host:15432/lecp"))
    with pytest.raises(UnsafeTargetError) as raised:
        assert_target_is_disposable(settings)
    assert "hunter2" not in str(raised.value)
    assert "someone" not in str(raised.value)


def test_a_tampered_corpus_is_rejected_rather_than_read(tmp_path: pathlib.Path) -> None:
    """A hand-edited corpus is the thing that makes a later test pass against data nobody
    can reproduce, so the manifest digest is recomputed rather than trusted."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    write_corpus(tmp_path, result.files)
    assert read_corpus(tmp_path).manifest.content_sha256 == result.manifest.content_sha256

    target = tmp_path / "ledger" / "snapshot.csv"
    target.write_bytes(
        target.read_bytes() + b"GL-99999999,4100,1.00,EUR,2026-06-01T00:00:00+00:00,\n"
    )
    with pytest.raises(CorpusIntegrityError, match="does not match its manifest digest"):
        read_corpus(tmp_path)


def test_a_corpus_from_an_unknown_schema_version_is_refused(tmp_path: pathlib.Path) -> None:
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    write_corpus(tmp_path, result.files)
    manifest = json.loads((tmp_path / MANIFEST_PATH).read_text(encoding="utf-8"))
    manifest["fixture_schema_version"] = "99"
    (tmp_path / MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")
    # Pydantic rejects it at validation because the version is a Literal; the explicit
    # CorpusIntegrityError below it would catch a version that parsed but was unrecognised.
    # Either refusal is correct — reading it optimistically is not.
    with pytest.raises(Exception, match=r"fixture schema|Input should be"):
        read_corpus(tmp_path)


def test_writing_a_corpus_removes_files_the_generator_no_longer_produces(
    tmp_path: pathlib.Path,
) -> None:
    """A stale artifact would still be hashed on the next read and break the integrity check
    for a reason that has nothing to do with the corpus."""
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    write_corpus(tmp_path, result.files)
    orphan = tmp_path / "settlement" / "psp-settlement-1999-01.csv"
    orphan.write_bytes(b"stale\n")

    write_corpus(tmp_path, result.files)
    assert not orphan.exists()
    assert read_corpus(tmp_path).manifest.content_sha256 == result.manifest.content_sha256


def test_generation_works_into_an_empty_directory(tmp_path: pathlib.Path) -> None:
    """The clean-checkout case: nothing pre-exists and nothing outside the corpus is read."""
    target = tmp_path / "nested" / "corpus"
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    write_corpus(target, result.files)
    assert corpus_files(target) == result.files
    assert read_corpus(target).corpus == result.corpus


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------


def test_generate_then_verify_round_trips(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["generate", "--out", str(tmp_path)]) == 0
    written = capsys.readouterr().out
    assert "scenarios" in written and "content sha256" in written

    assert main(["verify", "--dir", str(tmp_path)]) == 0
    assert "matches the generator byte for byte" in capsys.readouterr().out


def test_verify_fails_on_drift_and_says_how_to_fix_it(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI step's whole value is that a red build tells you what to do next."""
    assert main(["generate", "--out", str(tmp_path)]) == 0
    capsys.readouterr()

    (tmp_path / "records.json").write_bytes(b"{}\n")
    (tmp_path / "settlement" / "orphan.csv").write_bytes(b"stale\n")

    assert main(["verify", "--dir", str(tmp_path)]) == 1
    reported = capsys.readouterr().err
    assert "differs: records.json" in reported
    assert "unexpected: settlement/orphan.csv" in reported
    assert "make fixtures" in reported


def test_verify_reports_a_missing_artifact(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["generate", "--out", str(tmp_path)]) == 0
    capsys.readouterr()
    (tmp_path / "manifest.json").unlink()

    assert main(["verify", "--dir", str(tmp_path)]) == 1
    assert "missing: manifest.json" in capsys.readouterr().err


def test_the_cli_default_seed_is_the_one_the_committed_corpus_uses() -> None:
    """If these drift apart, `make fixtures` would silently rewrite the whole corpus."""
    assert DEFAULT_SEED == COMMITTED_SEED


def test_a_bulk_corpus_can_be_generated_from_the_cli(tmp_path: pathlib.Path) -> None:
    assert main(["generate", "--profile", "bulk", "--instances", "24", "--out", str(tmp_path)]) == 0
    assert read_corpus(tmp_path).manifest.scenario_count == 24


# --------------------------------------------------------------------------------------
# Input validation on the deterministic primitives
# --------------------------------------------------------------------------------------


def test_draw_rejects_incoherent_ranges() -> None:
    draw = Draw(1, "d")
    with pytest.raises(ValueError, match="high must not be below low"):
        draw.integer("x", 10, 1)
    with pytest.raises(ValueError, match="must not be empty"):
        draw.choice("x", ())
    with pytest.raises(ValueError, match="within"):
        draw.chance("x", 3, 2)
    with pytest.raises(ValueError, match="within"):
        draw.chance("x", 1, 0)


def test_draw_children_are_independent_namespaces() -> None:
    parent = Draw(5, "root")
    assert parent.child("a").integer("label", 0, 10**9) != parent.child("b").integer(
        "label", 0, 10**9
    )
    assert parent.child("a").integer("label", 0, 10**9) == parent.child("a").integer(
        "label", 0, 10**9
    )


def test_chance_is_a_pure_function_of_its_label() -> None:
    draw = Draw(9, "d")
    assert draw.chance("always", 1, 1) is True
    assert draw.chance("never", 0, 1) is False


def test_money_refuses_a_currency_the_schema_cannot_hold() -> None:
    """A five-decimal currency would silently violate the M1.1 scale constraint."""
    from ledger_exception_control_plane.fixtures.money import Currency

    with pytest.raises(ValueError, match="more precision than the schema permits"):
        money(1, Currency("XXX", 5))


def test_a_corpus_that_repeated_a_unique_key_would_be_refused() -> None:
    """The generator fails rather than producing a corpus that breaks halfway through a load."""
    with pytest.raises(ValueError, match="repeats ledger external_ref"):
        _reject_duplicates("ledger external_ref", ["GL-1", "GL-1"])


@pytest.mark.parametrize("instances", [12, 13, 24, 47, 100, 200, 401])
def test_a_bulk_corpus_contains_exactly_the_number_of_scenarios_requested(instances: int) -> None:
    """``--instances`` is a count, not a suggestion.

    The every-scenario floor takes its unit from the largest bucket rather than adding one, so
    guaranteeing coverage of the rare conditions cannot quietly inflate the corpus past the
    size the caller asked for.
    """
    result = generate(COMMITTED_SEED, Profile.BULK, instances)
    assert result.manifest.scenario_count == instances
    assert sum(result.manifest.residual_mix.values()) == instances
    covered = {line.scenario_id for batch in result.corpus.batches for line in batch.lines}
    assert covered == {entry.scenario_id for entry in CATALOGUE}


# --------------------------------------------------------------------------------------
# Corrections from the adversarial review
# --------------------------------------------------------------------------------------


def test_the_scope_guards_see_modules_in_subpackages() -> None:
    """The guards walk what is there, not what someone remembered to enumerate.

    ``glob`` rather than ``rglob`` left a hole exactly the shape of the thing the guards exist
    to catch: a ``fixtures/matching/`` subpackage would have been parsed by none of them.
    """
    package_root = pathlib.Path(fixtures_module_paths()[0]).parent
    discovered = {path.resolve() for path in fixtures_module_paths()}
    on_disk = {path.resolve() for path in package_root.rglob("*.py")}
    assert discovered == on_disk
    assert len(discovered) >= 10


def test_writing_a_corpus_refuses_a_directory_that_is_not_one(tmp_path: pathlib.Path) -> None:
    """``write_corpus`` deletes. Pointed at a repository root it would take the source tree.

    ``--out`` is free-form and ``.`` is a plausible slip, so the destructive writer is guarded
    the way ADR-035 guards the loader — which is the *less* dangerous of the two.
    """
    working_tree = tmp_path / "someones-work"
    (working_tree / ".git").mkdir(parents=True)
    (working_tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (working_tree / "README.md").write_text("important\n", encoding="utf-8")
    before = sorted(p.name for p in working_tree.rglob("*") if p.is_file())

    with pytest.raises(UnsafeTargetError, match="not a corpus this command may overwrite"):
        write_corpus(working_tree, generate(COMMITTED_SEED, Profile.CANONICAL).files)

    assert sorted(p.name for p in working_tree.rglob("*") if p.is_file()) == before


def test_writing_a_corpus_accepts_an_empty_directory_and_an_existing_corpus(
    tmp_path: pathlib.Path,
) -> None:
    """The complement: a guard that refused every directory would be useless."""
    files = generate(COMMITTED_SEED, Profile.CANONICAL).files
    empty = tmp_path / "fresh"
    empty.mkdir()
    write_corpus(empty, files)
    write_corpus(empty, files)  # now an existing corpus, and still writable
    assert corpus_files(empty) == files


def test_the_generate_command_refuses_an_unsafe_output_directory(tmp_path: pathlib.Path) -> None:
    (tmp_path / "notes.txt").write_text("mine\n", encoding="utf-8")
    with pytest.raises(UnsafeTargetError):
        main(["generate", "--out", str(tmp_path)])
    assert (tmp_path / "notes.txt").exists()


def test_a_malformed_dsn_cannot_leak_a_credential_through_the_refusal() -> None:
    """A password with an unencoded slash pushes credential material past ``urlsplit``'s netloc.

    The parsed 'name' is then not a database name at all, and it is exactly the case someone
    is debugging with the message in front of them (§17).
    """
    settings = Settings(
        postgres_dsn=SecretStr("postgresql://someone:hunter2/extra@host:15432/lecp_test")
    )
    with pytest.raises(UnsafeTargetError) as raised:
        assert_target_is_disposable(settings)
    message = str(raised.value)
    assert "hunter2" not in message
    assert "someone" not in message
    assert "not a valid database name" in message


@pytest.mark.parametrize("instances", [12, 13, 24, 47, 100, 200, 401, 1000])
def test_the_bulk_allocation_is_exact_ordered_and_complete(instances: int) -> None:
    """Three properties that hold at every size, checked against the weights directly.

    PROJECT_STATUS previously claimed the *mix* was verified at each of these sizes when only
    the count and the coverage were. What actually holds everywhere is: the total is exact,
    every scenario appears, and a heavier scenario never gets fewer instances than a lighter
    one. Strict proportionality is asserted separately, at the sizes where it can hold.
    """
    counts = _instance_counts(Profile.BULK, instances)
    assert sum(counts) == instances, "the requested size is a count, not a suggestion"
    assert all(count >= 1 for count in counts), "every declared scenario must appear"
    assert list(counts) == sorted(counts, reverse=True), (
        "the catalogue is ordered by descending weight, so the allocation must be too"
    )


@pytest.mark.parametrize("instances", [200, 401, 1000, 4321])
def test_the_bulk_allocation_is_strictly_proportional_once_the_floor_stops_binding(
    instances: int,
) -> None:
    """Largest-remainder allocation: every count is the floor or the ceiling of its ideal.

    Only assertable at ``instances >= TOTAL_WEIGHT``. Below that, proportional allocation gives
    the rarest scenarios less than one instance, and guaranteeing they appear necessarily takes
    those units from the dominant bucket — at 12 instances, ``exact_match`` drops from an ideal
    of 8.8 to 1 so the other eleven conditions can exist at all. That is the floor doing its
    job, not the allocator drifting, and a test that called it drift would be wrong.
    """
    assert instances >= TOTAL_WEIGHT
    counts = _instance_counts(Profile.BULK, instances)
    for entry, count in zip(CATALOGUE, counts, strict=True):
        ideal = instances * entry.weight / TOTAL_WEIGHT
        assert math.floor(ideal) <= count <= math.ceil(ideal), (
            f"{entry.scenario_id}: {count} is not floor/ceil of {ideal:.2f}"
        )


def test_the_fx_scenario_is_arithmetically_consistent() -> None:
    """A rounding scenario whose own numbers disagree by 100x is not a rounding scenario.

    The three values were drawn independently, so the corpus asserted that JPY 683,880 at
    0.00658 settled as EUR 40.77. Now the settled amount is constructed from the recorded rate,
    and the ledger differs by the minor units that make it a rounding case.
    """
    result = generate(COMMITTED_SEED, Profile.CANONICAL)
    rows = [
        row
        for row in result.files["settlement/psp-settlement-2026-06.csv"].decode().splitlines()[1:]
        if ",JPY," in row
    ]
    assert len(rows) == 1, "the canonical corpus holds exactly one cross-currency line"
    fields = rows[0].split(",")
    settled, presentment, rate = (
        decimal.Decimal(fields[3]),
        decimal.Decimal(fields[6]),
        decimal.Decimal(fields[8]),
    )
    expected = (presentment * rate).quantize(decimal.Decimal("0.01"))
    assert settled == expected, f"{presentment} x {rate} is {expected}, not {settled}"

    ledger = next(
        entry for entry in result.corpus.ledger_entries if entry.scenario_id == "SC-007-fx-rounding"
    )
    gap = abs(ledger.amount - settled)
    assert decimal.Decimal("0.01") <= gap <= decimal.Decimal("0.03"), (
        f"the ledger gap of {gap} is not a rounding artefact"
    )


# ======================================================================================
# The declared distribution contract
#
# IMPLEMENTATION_PLAN.md 1.3 requires that "residual mix matches the declared distribution".
# A share of a discrete corpus is rarely an integer, so the requirement is met by stating an
# apportionment rule and testing the exact integer allocation it produces — not by asserting
# that the output looks about right.
#
# The rule (documented on _instance_counts): Hare quota with largest remainder, ties by
# catalogue position, then a coverage floor that raises any zero bucket to one and takes the
# unit from the largest bucket.
#
# These tests do NOT call the implementation to decide what to expect. _hamilton below is an
# independent reimplementation using exact rationals, and _EXPECTED_ALLOCATIONS holds
# hand-computed literals. Comparing the implementation against itself would prove only that it
# is consistent, which is not the property under test.
# ======================================================================================

#: The declared distribution, restated here as literals rather than imported. If someone
#: changes a weight in the catalogue, this test must fail and force the change to be
#: deliberate — importing CATALOGUE.weight would make the test agree with any change silently.
DECLARED_WEIGHTS: dict[str, int] = {
    "SC-001-exact-match": 147,
    "SC-002-reference-mismatch": 16,
    "SC-003-near-amount-difference": 12,
    "SC-004-partial-capture": 6,
    "SC-005-fee-split": 4,
    "SC-006-chargeback-reversal": 4,
    "SC-007-fx-rounding": 4,
    "SC-008-cross-period-refund": 2,
    "SC-009-unclassified": 2,
    "SC-010-missing-merchant-reference": 1,
    "SC-011-ambiguous-memo": 1,
    "SC-012-repeated-psp-reference": 1,
}

#: The declared distribution aggregated to classification level — what the plan calls the
#: residual mix. Also literals: partial_capture is SC-004 + SC-011, unclassified is
#: SC-009 + SC-010 + SC-012, matched is SC-001 + SC-002.
DECLARED_CLASSIFICATION_WEIGHTS: dict[str, int] = {
    "chargeback_reversal": 4,
    "cross_period_refund": 2,
    "fee_split": 4,
    "fx_rounding": 4,
    "matched": 147 + 16,
    "partial_capture": 6 + 1,
    "tolerance_policy_dependent": 12,
    "unclassified": 2 + 1 + 1,
}

DECLARED_TOTAL = 200


def _hamilton(weights: list[int], total_weight: int, size: int) -> list[int]:
    """Hare quota with largest remainder, then the coverage floor. Written independently.

    Exact rationals rather than floats: at large sizes a float remainder can compare equal when
    the true values differ, which would make the tie-break — and therefore the expected
    allocation — depend on binary rounding.
    """
    ideals = [Fraction(size * weight, total_weight) for weight in weights]
    counts = [int(ideal) for ideal in ideals]

    remainders = sorted(
        range(len(weights)),
        key=lambda index: (-(ideals[index] - counts[index]), index),
    )
    for index in remainders[: size - sum(counts)]:
        counts[index] += 1

    for index, count in enumerate(counts):
        if count == 0:
            donor = max(range(len(counts)), key=lambda position: (counts[position], -position))
            counts[donor] -= 1
            counts[index] = 1
    return counts


#: Hand-computed allocations, worked through the rule by hand and pinned as literals. These are
#: the tests that would still catch a change to the apportionment rule itself, which a
#: reimplementation-versus-implementation comparison would not if both were changed together.
_EXPECTED_ALLOCATIONS: dict[int, tuple[int, ...]] = {
    # Exactly the declared weights: every ideal is a whole number, nothing to apportion.
    200: (147, 16, 12, 6, 4, 4, 4, 2, 2, 1, 1, 1),
    # Twice the weights, still exact.
    400: (294, 32, 24, 12, 8, 8, 8, 4, 4, 2, 2, 2),
    # Minimum size. Every ideal below one except exact-match; the floor raises eleven buckets
    # to one and takes all eleven units from the dominant bucket.
    12: (1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
    # Half the weights, and the case that exercises every step of the rule at once. Four
    # buckets carry a .5 remainder (SC-001 at 73.5, and SC-010/011/012 at 0.5 each); the two
    # available units go to the first two in catalogue order, giving SC-001 74 and SC-010 1.
    # SC-011 and SC-012 are then still at zero, so the coverage floor raises each to one and
    # takes both units from the dominant bucket: SC-001 ends at 72, not 74.
    #
    # This literal was written as 74 first. The independent reimplementation disagreed, and it
    # was the literal that was wrong — which is the reason both exist.
    100: (72, 8, 6, 3, 2, 2, 2, 1, 1, 1, 1, 1),
}


def test_the_declared_weights_are_what_the_catalogue_actually_carries() -> None:
    """Pins the business distribution. A weight change must be deliberate, not incidental."""
    assert {entry.scenario_id: entry.weight for entry in CATALOGUE} == DECLARED_WEIGHTS
    assert sum(DECLARED_WEIGHTS.values()) == DECLARED_TOTAL == TOTAL_WEIGHT
    assert declared_classification_weights() == dict(
        sorted(DECLARED_CLASSIFICATION_WEIGHTS.items())
    )
    assert sum(DECLARED_CLASSIFICATION_WEIGHTS.values()) == DECLARED_TOTAL


@pytest.mark.parametrize("size", sorted(_EXPECTED_ALLOCATIONS))
def test_the_allocation_matches_hand_computed_literals(size: int) -> None:
    """The exact integer allocation, worked through the rule by hand."""
    assert _instance_counts(Profile.BULK, size) == _EXPECTED_ALLOCATIONS[size]
    assert sum(_EXPECTED_ALLOCATIONS[size]) == size


@pytest.mark.parametrize(
    "size",
    [
        12,  # smallest permitted: one per scenario class
        13,  # small, and one above the class count
        17,  # small, prime, nothing divides cleanly
        47,  # non-divisible
        100,  # half the declared total: five exact .5 remainders
        199,  # one below the point where the coverage floor stops binding
        200,  # divides cleanly — the declared percentages exactly
        201,  # one above, so a single remainder unit must be placed
        400,  # clean multiple
        1000,  # clean multiple, larger
        4321,  # large and awkward
    ],
)
def test_the_allocation_matches_the_declared_apportionment_rule(size: int) -> None:
    """The implementation is compared against an independent reimplementation of the rule."""
    weights = [DECLARED_WEIGHTS[entry.scenario_id] for entry in CATALOGUE]
    assert list(_instance_counts(Profile.BULK, size)) == _hamilton(weights, DECLARED_TOTAL, size)


@pytest.mark.parametrize("size", [200, 400, 1000, 4000])
def test_a_size_that_divides_cleanly_reproduces_the_declared_percentages_exactly(
    size: int,
) -> None:
    """No remainder to apportion, so the corpus *is* the declared distribution."""
    multiple = size // DECLARED_TOTAL
    expected = tuple(DECLARED_WEIGHTS[entry.scenario_id] * multiple for entry in CATALOGUE)
    assert _instance_counts(Profile.BULK, size) == expected

    for entry, count in zip(CATALOGUE, expected, strict=True):
        share = Fraction(count, size)
        assert share == Fraction(DECLARED_WEIGHTS[entry.scenario_id], DECLARED_TOTAL)


@pytest.mark.parametrize("size", [200, 201, 257, 400, 1000, 4321])
def test_every_share_is_within_one_instance_of_its_ideal_once_the_floor_stops_binding(
    size: int,
) -> None:
    """The mathematically justified bound for largest-remainder apportionment.

    Only claimable at ``size >= TOTAL_WEIGHT``; below that the coverage floor deliberately moves
    units, and the deviation is accounted for separately.
    """
    assert size >= TOTAL_WEIGHT
    counts = _instance_counts(Profile.BULK, size)
    for entry, count in zip(CATALOGUE, counts, strict=True):
        ideal = Fraction(size * DECLARED_WEIGHTS[entry.scenario_id], DECLARED_TOTAL)
        assert abs(Fraction(count) - ideal) < 1, (
            f"{entry.scenario_id}: {count} vs {float(ideal):.3f}"
        )


@pytest.mark.parametrize("size", [12, 13, 17, 47, 100, 199])
def test_below_the_floor_threshold_the_deviation_is_confined_to_the_donor(size: int) -> None:
    """The floor's cost is bounded and lands where the contract says it lands.

    Every bucket the floor did not touch stays within one instance of its ideal; the dominant
    bucket absorbs the entire adjustment. Stating it as a test stops "the floor moved some
    units" from becoming a licence for arbitrary drift.
    """
    assert size < TOTAL_WEIGHT
    counts = _instance_counts(Profile.BULK, size)
    weights = [DECLARED_WEIGHTS[entry.scenario_id] for entry in CATALOGUE]

    ideals = [Fraction(size * weight, DECLARED_TOTAL) for weight in weights]
    unfloored = [int(ideal) for ideal in ideals]
    order = sorted(range(len(weights)), key=lambda i: (-(ideals[i] - unfloored[i]), i))
    for index in order[: size - sum(unfloored)]:
        unfloored[index] += 1

    floored = [index for index, count in enumerate(unfloored) if count == 0]
    donated = sum(counts[i] - unfloored[i] for i in floored)
    assert donated == len(floored), "each floored scenario receives exactly one instance"

    deficit = sum(unfloored[i] - counts[i] for i in range(len(counts)) if i not in floored)
    assert deficit == len(floored), "the units come from elsewhere; none are created"

    for index, entry in enumerate(CATALOGUE):
        if index in floored or counts[index] < unfloored[index]:
            continue
        assert abs(Fraction(counts[index]) - ideals[index]) < 1, entry.scenario_id


@pytest.mark.parametrize("size", [12, 47, 200, 401])
def test_the_generated_residual_mix_matches_the_declared_apportionment(size: int) -> None:
    """End to end, at the level the plan names: the *residual mix* of a generated corpus.

    Scenario counts are apportioned independently, aggregated to classification level from the
    declared weights, and compared with what the generator actually produced.
    """
    weights = [DECLARED_WEIGHTS[entry.scenario_id] for entry in CATALOGUE]
    allocation = _hamilton(weights, DECLARED_TOTAL, size)

    expected: dict[str, int] = {}
    for entry, count in zip(CATALOGUE, allocation, strict=True):
        built = entry.build(Draw(0, "expected"), "")
        key = (
            built.intended_classification.value
            if built.intent is MatchIntent.RESIDUAL and built.intended_classification is not None
            else built.intent.value
        )
        expected[key] = expected.get(key, 0) + count

    result = generate(COMMITTED_SEED, Profile.BULK, size)
    assert result.manifest.residual_mix == dict(sorted(expected.items()))
    assert sum(result.manifest.residual_mix.values()) == size


def test_the_residual_mix_is_exactly_the_declared_percentages_at_a_clean_size() -> None:
    """At 200 instances the mix is not merely close to the declared distribution — it is it."""
    result = generate(COMMITTED_SEED, Profile.BULK, DECLARED_TOTAL)
    assert result.manifest.residual_mix == dict(sorted(DECLARED_CLASSIFICATION_WEIGHTS.items()))


@pytest.mark.parametrize("size", [12, 13, 17, 47, 100, 199, 200, 201, 400, 1000, 4321])
def test_exactly_the_requested_number_of_instances_is_produced(size: int) -> None:
    """``--instances N`` is a count. The coverage floor moves units; it never creates them."""
    counts = _instance_counts(Profile.BULK, size)
    assert sum(counts) == size
    assert min(counts) >= 1


@pytest.mark.parametrize("size", [12, 47, 200])
def test_the_same_seed_profile_and_size_reproduce_byte_identically(size: int) -> None:
    """Reproducibility is a property of all three inputs, not of the seed alone."""
    first = generate(COMMITTED_SEED, Profile.BULK, size)
    second = generate(COMMITTED_SEED, Profile.BULK, size)
    assert first.files == second.files
    assert first.manifest.content_sha256 == second.manifest.content_sha256

    different_size = generate(COMMITTED_SEED, Profile.BULK, size + 1)
    assert different_size.manifest.content_sha256 != first.manifest.content_sha256


def test_the_expected_mix_is_computed_without_any_matching_logic() -> None:
    """Ground-truth independence, restated where the distribution is checked.

    The expected mix above is aggregated from ``intended_classification`` — a field the builder
    *wrote* — and from the declared weights. Nothing compares a settlement line to a ledger
    entry to decide what a scenario is. This test pins that: every classification the corpus
    reports is one a builder declared, and the set of declared classifications is closed.
    """
    result = generate(COMMITTED_SEED, Profile.BULK, 47)
    declared = set(declared_classification_weights())
    assert set(result.manifest.residual_mix) <= declared

    residual_labels = {
        scenario.intended_classification
        for scenario in result.scenarios.scenarios
        if scenario.intended_classification is not None
    }
    assert residual_labels == set(ExceptionClassification)
    # And the corpus carries no match_result, exception or proposal data from which a label
    # could have been derived by computation rather than declaration.
    payload = json.loads(result.files["records.json"])
    assert set(payload) == {
        "fixture_schema_version",
        "generator_version",
        "profile",
        "seed",
        "batches",
        "ledger_entries",
    }
