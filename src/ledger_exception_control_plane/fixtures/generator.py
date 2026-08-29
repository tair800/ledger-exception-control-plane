"""Corpus construction — a pure function of ``(generator version, seed, profile, size)``.

Nothing here reads the clock, the filesystem, the environment or a random source. Given the
same inputs this module returns identical bytes, which is what makes the plan's exit criterion
— *the corpus regenerates byte-identically* — checkable by comparing files.

**Scope boundary, stated plainly.** This module constructs *input conditions*. It contains no
parser, no normaliser, no matcher, no tolerance arithmetic and no classifier. It never
compares a settlement line to a ledger entry in order to decide anything: where a scenario is
described as matching, it matches because the builder wrote both sides that way, and the
scenario metadata records that construction intent. A test walks this package's AST and fails
if it acquires an import that would let it do otherwise.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
from typing import Final

from ledger_exception_control_plane.fixtures.catalogue import (
    CATALOGUE,
    TOTAL_WEIGHT,
    BuiltEntry,
    BuiltLine,
    BuiltScenario,
)
from ledger_exception_control_plane.fixtures.determinism import Draw, fixture_uuid
from ledger_exception_control_plane.fixtures.invalid import INVALID_FIXTURES
from ledger_exception_control_plane.fixtures.schema import (
    FIXTURE_SCHEMA_VERSION,
    GENERATOR_VERSION,
    Corpus,
    InvalidCatalogue,
    LedgerEntryRecord,
    Manifest,
    MatchIntent,
    Profile,
    Scenario,
    ScenarioCatalogue,
    SettlementBatchRecord,
    SettlementLineRecord,
)
from ledger_exception_control_plane.fixtures.serialise import (
    content_digest,
    render_json,
    render_ledger_csv,
    render_settlement_csv,
)

#: Scenario instances the ``bulk`` profile builds by default. Equal to the total catalogue
#: weight, so at the default every scenario's instance count is exactly its declared weight and
#: the mix can be checked without rounding entering the argument.
BULK_DEFAULT_INSTANCES: Final = TOTAL_WEIGHT

#: Where the corpus says its settlement files came from. A fixed label — never a hostname, a
#: path or an account name, because a corpus must not carry the machine that produced it.
SOURCE_LABEL: Final = "psp-settlement-feed"

LEDGER_SNAPSHOT_PATH: Final = "ledger/snapshot.csv"
RECORDS_PATH: Final = "records.json"
SCENARIOS_PATH: Final = "scenarios.json"
INVALID_INDEX_PATH: Final = "invalid/index.json"
MANIFEST_PATH: Final = "manifest.json"


@dataclasses.dataclass(frozen=True, slots=True)
class GeneratedCorpus:
    """One generation run: the typed artifacts and the exact bytes they serialise to."""

    corpus: Corpus
    scenarios: ScenarioCatalogue
    manifest: Manifest
    files: dict[str, bytes]


@dataclasses.dataclass(frozen=True, slots=True)
class _PlacedLine:
    scenario_id: str
    instance: int
    position: int
    line: BuiltLine


def _instance_counts(profile: Profile, instances: int) -> tuple[int, ...]:
    """How many times each catalogue entry is built. **This is the allocation contract.**

    ``canonical`` builds exactly one of each, so a test can address a condition by scenario id
    and know there is precisely one.

    ``bulk`` apportions the declared weights over ``instances``. A share of a discrete corpus
    is rarely an integer, so the rule is stated rather than approximated:

    1. **Ideal share.** ``ideal_i = instances * weight_i / TOTAL_WEIGHT``, exact rational.
    2. **Base.** ``floor(ideal_i)``.
    3. **Remainder.** The ``instances - Σ floor`` unallocated units go one each to the largest
       fractional remainders, ties broken by catalogue position — the **Hare quota with
       largest remainder** (Hamilton's method). Deterministic: no sort instability and no
       dependence on dictionary order can reach it.
    4. **Coverage floor.** Any scenario still at zero is raised to one, and one unit is taken
       from the largest bucket (ties by earliest catalogue position). Applied in catalogue
       order.
    5. **Total.** ``Σ = instances``, exactly, always. Step 4 moves units, never creates them.

    What this guarantees, by size:

    * **Any ``instances ≥ len(CATALOGUE)``** — the total is exactly ``instances``; every
      scenario appears at least once; counts are weakly decreasing in catalogue order (the
      catalogue is ordered by descending weight).
    * **``instances ≥ TOTAL_WEIGHT``** — the floor cannot bind, because the smallest weight is
      1 and ``instances * 1 / TOTAL_WEIGHT ≥ 1``. Pure Hamilton therefore applies and every
      count is ``floor(ideal_i)`` or ``ceil(ideal_i)``: deviation from the ideal share is
      **strictly less than one instance**.
    * **``instances`` a multiple of ``TOTAL_WEIGHT``** — every ideal is an integer, so there is
      no remainder to allocate and the corpus matches the declared percentages **exactly**.
    * **``len(CATALOGUE) ≤ instances < TOTAL_WEIGHT``** — the rarest classes have an ideal
      below one and would be allocated zero. Step 4 raises each to one and takes the units from
      the dominant bucket, so the deviation is concentrated there by construction: every other
      bucket stays within one of its ideal, and the donor absorbs the whole adjustment. That is
      the floor doing its job — a corpus missing a declared condition is worse than one whose
      dominant class is under-represented — and it is a documented property, not drift.

    Step 4 cannot strand a bucket at zero. If any bucket is zero then, since the total is
    ``instances ≥ len(CATALOGUE)`` and there are ``len(CATALOGUE)`` buckets, the largest must
    hold at least two; donating leaves it at one or more. A bucket already passed can therefore
    only fall from ≥2 to ≥1, never to zero.
    """
    if profile is Profile.CANONICAL:
        return tuple(1 for _ in CATALOGUE)

    if instances < len(CATALOGUE):
        raise ValueError(
            f"a bulk corpus needs at least {len(CATALOGUE)} instances, one per scenario"
        )

    base = [entry.weight * instances // TOTAL_WEIGHT for entry in CATALOGUE]
    # Ties broken by catalogue position, so the allocation is a pure function of the inputs
    # rather than of sort stability.
    remainders = sorted(
        (
            (entry.weight * instances % TOTAL_WEIGHT, -index)
            for index, entry in enumerate(CATALOGUE)
        ),
        reverse=True,
    )
    for _, negative_index in remainders[: instances - sum(base)]:
        base[-negative_index] += 1

    # Every scenario appears at least once, or the corpus quietly loses coverage of a condition
    # the catalogue claims it has — proportional allocation zeroes the rare ones first.
    #
    # The unit comes *out of the largest bucket* rather than being added on top. Adding would
    # make the corpus larger than the size that was asked for, which turns --instances into a
    # suggestion; the caller asked for a number and gets exactly that number. Guaranteed to be
    # possible because `instances >= len(CATALOGUE)` is enforced above, so the largest bucket
    # always has more than one to give.
    for index, count in enumerate(base):
        if count == 0:
            donor = max(range(len(base)), key=lambda position: (base[position], -position))
            base[donor] -= 1
            base[index] = 1
    return tuple(base)


def _build_scenarios(seed: int, profile: Profile, instances: int) -> tuple[BuiltScenario, ...]:
    built: list[BuiltScenario] = []
    for entry, count in zip(CATALOGUE, _instance_counts(profile, instances), strict=True):
        for index in range(count):
            suffix = "" if profile is Profile.CANONICAL else f"-{index:04d}"
            built.append(
                entry.build(Draw(seed, f"{profile.value}/{entry.scenario_id}/{index}"), suffix)
            )
    return tuple(built)


def _period(value: dt.date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _reject_duplicates(label: str, values: list[str]) -> None:
    """Fail at generation time rather than halfway through a load.

    A corpus repeating an ``external_ref`` violates a unique constraint. Without this it would
    fail during loading, leaving a partial database and an error naming the constraint rather
    than the generator that produced the bad data.
    """
    repeated = sorted({v for v, count in collections.Counter(values).items() if count > 1})
    if repeated:
        raise ValueError(f"generated corpus repeats {label}: {repeated}")


def _received_at(latest_value_date: dt.date) -> dt.datetime:
    """A settlement file arrives the morning after its last value date. Fixed, not observed."""
    return dt.datetime.combine(
        latest_value_date + dt.timedelta(days=1), dt.time(6, 0), tzinfo=dt.UTC
    )


def generate(
    seed: int,
    profile: Profile = Profile.CANONICAL,
    instances: int = BULK_DEFAULT_INSTANCES,
) -> GeneratedCorpus:
    """Build a complete corpus and render every artifact."""
    scenarios = _build_scenarios(seed, profile, instances)

    placed: list[_PlacedLine] = []
    entries: list[BuiltEntry] = []
    seen: collections.Counter[str] = collections.Counter()

    for scenario in scenarios:
        instance = seen[scenario.scenario_id]
        seen[scenario.scenario_id] += 1
        placed.extend(
            _PlacedLine(scenario.scenario_id, instance, position, line)
            for position, line in enumerate(scenario.lines)
        )
        entries.extend(scenario.entries)

    _reject_duplicates("ledger external_ref", [entry.external_ref for entry in entries])

    # Batches form by value-date period, which is what a settlement feed actually does — one
    # file per settlement period. It also means the cross-period refund scenario produces a
    # second batch by construction rather than through a special case.
    by_period: dict[str, list[_PlacedLine]] = collections.defaultdict(list)
    for item in placed:
        by_period[_period(item.line.value_date)].append(item)

    files: dict[str, bytes] = {}
    batch_records: list[SettlementBatchRecord] = []

    for period in sorted(by_period):
        # A total, explicit ordering. Without it the line numbers — and so the file bytes —
        # would depend on the order scenarios happened to be appended in.
        ordered = sorted(
            by_period[period], key=lambda item: (item.scenario_id, item.instance, item.position)
        )
        path = f"settlement/psp-settlement-{period}.csv"
        payload = render_settlement_csv(tuple(item.line for item in ordered))
        files[path] = payload

        batch_records.append(
            SettlementBatchRecord(
                id=fixture_uuid("settlement_batch", profile.value, str(seed), period),
                content_hash=hashlib.sha256(payload).hexdigest(),
                source=SOURCE_LABEL,
                received_at=_received_at(max(item.line.value_date for item in ordered)),
                status="parsed",
                raw_payload_path=path,
                lines=tuple(
                    SettlementLineRecord(
                        id=fixture_uuid(
                            "settlement_line", profile.value, str(seed), period, str(number)
                        ),
                        scenario_id=item.scenario_id,
                        line_number=number,
                        psp_reference=item.line.psp_reference,
                        merchant_reference=item.line.merchant_reference,
                        amount=item.line.amount,
                        currency=item.line.currency.code,
                        value_date=item.line.value_date,
                    )
                    for number, item in enumerate(ordered, start=1)
                ),
            )
        )

    sorted_entries = tuple(sorted(entries, key=lambda item: item.external_ref))
    files[LEDGER_SNAPSHOT_PATH] = render_ledger_csv(sorted_entries)

    entry_scenario = {
        entry.external_ref: scenario.scenario_id
        for scenario in scenarios
        for entry in scenario.entries
    }
    corpus = Corpus(
        fixture_schema_version=FIXTURE_SCHEMA_VERSION,
        generator_version=GENERATOR_VERSION,
        profile=profile,
        seed=seed,
        batches=tuple(batch_records),
        ledger_entries=tuple(
            LedgerEntryRecord(
                id=fixture_uuid("ledger_entry", profile.value, str(seed), entry.external_ref),
                scenario_id=entry_scenario[entry.external_ref],
                external_ref=entry.external_ref,
                account_code=entry.account_code,
                amount=entry.amount,
                currency=entry.currency.code,
                booked_at=entry.booked_at,
                description=entry.description,
            )
            for entry in sorted_entries
        ),
    )

    # One Scenario entry per catalogue id, not per instance: the metadata describes the
    # *condition*, and a bulk corpus repeating a condition 147 times does not make it 147
    # different things. Instances are addressable through the records' scenario_id.
    catalogue_entry_by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    scenario_catalogue = ScenarioCatalogue(
        fixture_schema_version=FIXTURE_SCHEMA_VERSION,
        generator_version=GENERATOR_VERSION,
        profile=profile,
        seed=seed,
        scenarios=tuple(
            Scenario(
                scenario_id=entry.scenario_id,
                kind=built.kind,
                intent=built.intent,
                intended_classification=built.intended_classification,
                awkwardness=built.awkwardness,
                why_it_exists=built.why_it_exists,
                distinguishing_fields=built.distinguishing_fields,
                settlement_references=tuple(line.psp_reference for line in built.lines),
                ledger_references=tuple(item.external_ref for item in built.entries),
            )
            for entry in CATALOGUE
            for built in (catalogue_entry_by_id[entry.scenario_id],)
        ),
    )

    invalid_catalogue = InvalidCatalogue(
        fixture_schema_version=FIXTURE_SCHEMA_VERSION,
        generator_version=GENERATOR_VERSION,
        profile=profile,
        seed=seed,
        fixtures=tuple(fixture.record for fixture in INVALID_FIXTURES),
    )
    for fixture in INVALID_FIXTURES:
        files[fixture.record.path] = fixture.payload

    files[RECORDS_PATH] = render_json(corpus)
    files[SCENARIOS_PATH] = render_json(scenario_catalogue)
    files[INVALID_INDEX_PATH] = render_json(invalid_catalogue)

    manifest = Manifest(
        fixture_schema_version=FIXTURE_SCHEMA_VERSION,
        generator_version=GENERATOR_VERSION,
        profile=profile,
        seed=seed,
        scenario_count=len(scenarios),
        batch_count=len(corpus.batches),
        settlement_line_count=corpus.line_count,
        ledger_entry_count=len(corpus.ledger_entries),
        residual_mix=residual_mix(scenarios),
        content_sha256=content_digest(files),
    )
    files[MANIFEST_PATH] = render_json(manifest)

    return GeneratedCorpus(
        corpus=corpus, scenarios=scenario_catalogue, manifest=manifest, files=files
    )


def residual_mix(scenarios: tuple[BuiltScenario, ...]) -> dict[str, int]:
    """Instance counts by intended outcome — the declared distribution, made checkable.

    Keyed by intended classification for residual scenarios and by the intent itself for the
    two non-residual intents, so the mix reads as one closed breakdown rather than two tables.
    """
    counter: collections.Counter[str] = collections.Counter()
    for scenario in scenarios:
        if scenario.intent is MatchIntent.RESIDUAL:
            if scenario.intended_classification is None:
                raise ValueError(f"{scenario.scenario_id} is residual with no classification")
            counter[scenario.intended_classification.value] += 1
        else:
            counter[scenario.intent.value] += 1
    return dict(sorted(counter.items()))
