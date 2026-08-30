"""Match *precision*, not clearance — did the matcher pair the right rows?

A clearance rate says how much work the matcher removed. It says nothing about whether the pairs
it chose were the right ones, and the two can move in opposite directions: a matcher that paired
lines with whatever ledger entry happened to share an amount would report an excellent clearance
rate while consuming the wrong entries. Because ``match_result`` is unique on the ledger entry
(ADR-024), a wrong pairing is not merely a mislabel — it permanently denies the entry to the line
that genuinely owned it.

The M1.3 corpus makes precision measurable. Every settlement line and every ledger entry carries
the ``scenario_id`` it was *constructed* for, so a pair is correct exactly when both sides come
from the same constructed scenario, and any cross-scenario pair is a false match produced by an
amount-and-date coincidence.

**This file is the only place that reads construction metadata**, and it reads it to *judge*
production output, never to produce it. The matching package cannot reach any of it — asserted
separately by the guards in ``test_matching.py`` and again at the bottom of this file.

The invariant that matters: **no false positive financial match.** It is asserted as zero, not as
a rate.
"""

from __future__ import annotations

import collections
import dataclasses
import uuid

import pytest

from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import MatchIntent, Profile
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    MatchRule,
    match,
)

SEED = 20260829


@dataclasses.dataclass(frozen=True, slots=True)
class Precision:
    """One measurement of the matcher against the corpus it was run over."""

    eligible: int
    matched: int
    correct: int
    false_matches: tuple[tuple[str, str], ...]
    ambiguous: int
    unmatched: int
    exact: int
    tolerance: int
    matched_by_scenario: dict[str, int]
    lines_by_scenario: dict[str, int]
    intent_by_scenario: dict[str, MatchIntent]


def measure(profile: Profile, instances: int = 200) -> Precision:
    """Run the real matcher over a generated corpus and grade every pair it produced."""
    corpus = generate(SEED, profile, instances)

    scenario_of_line: dict[uuid.UUID, str] = {}
    lines: list[CandidateLine] = []
    for batch in corpus.corpus.batches:
        for row in batch.lines:
            scenario_of_line[row.id] = row.scenario_id
            lines.append(
                CandidateLine(row.id, row.line_number, row.amount, row.currency, row.value_date)
            )

    scenario_of_entry = {row.id: row.scenario_id for row in corpus.corpus.ledger_entries}
    entries = [
        CandidateEntry(row.id, row.external_ref, row.amount, row.currency, row.booked_at.date())
        for row in corpus.corpus.ledger_entries
    ]

    outcome = match(lines, entries, DEFAULT_POLICY)

    false_matches = tuple(
        (scenario_of_line[m.line_id], scenario_of_entry[m.entry_id])
        for m in outcome.matches
        if scenario_of_line[m.line_id] != scenario_of_entry[m.entry_id]
    )
    matched_by_scenario: collections.Counter[str] = collections.Counter(
        scenario_of_line[m.line_id] for m in outcome.matches
    )
    lines_by_scenario: collections.Counter[str] = collections.Counter(scenario_of_line.values())

    return Precision(
        eligible=len(lines),
        matched=len(outcome.matches),
        correct=len(outcome.matches) - len(false_matches),
        false_matches=false_matches,
        ambiguous=len(outcome.ambiguous_line_ids),
        unmatched=len(outcome.unmatched_line_ids),
        exact=sum(1 for m in outcome.matches if m.rule is MatchRule.EXACT_AMOUNT),
        tolerance=sum(1 for m in outcome.matches if m.rule is MatchRule.AMOUNT_WITHIN_TOLERANCE),
        matched_by_scenario=dict(matched_by_scenario),
        lines_by_scenario=dict(lines_by_scenario),
        intent_by_scenario={s.scenario_id: s.intent for s in corpus.scenarios.scenarios},
    )


# --------------------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "instances"),
    [
        (Profile.CANONICAL, 200),
        (Profile.BULK, 200),
        (Profile.BULK, 1000),
        (Profile.BULK, 4000),
    ],
)
def test_no_settlement_line_is_ever_paired_with_a_foreign_ledger_entry(
    profile: Profile, instances: int
) -> None:
    """**Zero false matches.** Asserted as a count, not as a rate.

    Run at four sizes because coincidence is a function of volume: at 4,300 lines there are many
    more opportunities for two unrelated movements to share an amount and a date. The matcher's
    answer to those is to refuse them — ambiguity rises with scale while false matches stay at
    zero — which is the behaviour the mutual-uniqueness rule exists to produce.
    """
    result = measure(profile, instances)
    assert result.false_matches == (), (
        f"{len(result.false_matches)} false match(es), e.g. {result.false_matches[:3]}"
    )
    assert result.correct == result.matched


def test_precision_is_reported_in_full_for_the_canonical_corpus() -> None:
    """Every figure the increment claims, pinned. A change to any of them must be deliberate."""
    result = measure(Profile.CANONICAL)

    assert result.eligible == 17
    assert result.matched == 4
    assert result.correct == 4
    assert result.false_matches == ()
    assert result.ambiguous == 0
    assert result.unmatched == 13
    assert result.exact == 4
    assert result.tolerance == 0


def test_precision_is_reported_in_full_for_the_bulk_corpus() -> None:
    result = measure(Profile.BULK, 200)

    assert result.eligible == 215
    assert result.matched == 176
    assert result.correct == 176
    assert result.false_matches == ()
    assert result.ambiguous == 0
    assert result.unmatched == 39
    assert result.exact == 169
    assert result.tolerance == 7
    assert result.matched / result.eligible > 0.75, "the matcher must still clear the bulk"


def test_ambiguity_grows_with_volume_while_false_matches_do_not() -> None:
    """The trade the design makes, measured.

    More rows mean more coincidences. A matcher that resolved them would show a rising false-match
    count; this one shows a rising *ambiguity* count and a false-match count that stays at zero.
    """
    small, large = measure(Profile.BULK, 200), measure(Profile.BULK, 4000)
    assert large.ambiguous > small.ambiguous
    assert small.false_matches == () and large.false_matches == ()


# --------------------------------------------------------------------------------------
# Construction intent, honoured rather than assumed
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "instances"), [(Profile.CANONICAL, 200), (Profile.BULK, 200)])
def test_every_matched_intent_scenario_is_fully_cleared(profile: Profile, instances: int) -> None:
    """The scenarios built to be matchable must all match. Otherwise the matcher is too tight."""
    result = measure(profile, instances)
    for scenario, intent in result.intent_by_scenario.items():
        if intent is MatchIntent.MATCHED:
            assert (
                result.matched_by_scenario.get(scenario, 0) == result.lines_by_scenario[scenario]
            ), f"{scenario} was constructed to match and did not"


def test_a_residual_scenario_only_ever_matches_within_itself() -> None:
    """The subtle case, and the one a naive reading gets wrong.

    Several ``residual`` scenarios contain a line that legitimately corresponds to their own ledger
    entry — SC-006 builds a chargeback and its later reversal against a single ledger debit, and the
    chargeback line *is* that debit. The scenario is residual because its **other** line is. So
    "a residual scenario matched something" is not evidence of a false positive; pairing across
    scenarios would be, and that is what is asserted here.

    Asserting instead that no residual-intent line ever matches would be asserting something false,
    and would have pushed the matcher into agreeing with it.
    """
    result = measure(Profile.CANONICAL)
    residual_with_matches = {
        scenario
        for scenario, count in result.matched_by_scenario.items()
        if result.intent_by_scenario[scenario] is MatchIntent.RESIDUAL and count
    }
    assert residual_with_matches == {"SC-006-chargeback-reversal", "SC-008-cross-period-refund"}
    for scenario in residual_with_matches:
        assert result.matched_by_scenario[scenario] < result.lines_by_scenario[scenario], (
            f"{scenario} must retain a residual line; only part of it corresponds to the ledger"
        )
    assert result.false_matches == ()


def test_the_tolerance_policy_dependent_scenario_is_decided_by_the_policy_not_by_accident() -> None:
    """SC-003 was labelled ``tolerance_policy_dependent`` because M1.3 declined to predict it.

    Now that OPEN-2 is resolved the answer is determined, and it is determined *by the band*: the
    instances that clear are exactly those whose drawn drift is one minor unit or less. Derived
    from the policy here rather than hardcoded, because the corpus draws one to three units and a
    different seed would legitimately give different counts.
    """
    corpus = generate(SEED, Profile.BULK, 200)
    band = DEFAULT_POLICY.band("GBP")
    assert band is not None

    entries_by_scenario: dict[str, list[CandidateEntry]] = collections.defaultdict(list)
    for row in corpus.corpus.ledger_entries:
        entries_by_scenario[row.scenario_id].append(
            CandidateEntry(row.id, row.external_ref, row.amount, row.currency, row.booked_at.date())
        )

    expected = 0
    for batch in corpus.corpus.batches:
        for settlement in batch.lines:
            if not settlement.scenario_id.startswith("SC-003"):
                continue
            gap = min(
                abs(settlement.amount - candidate.amount)
                for candidate in entries_by_scenario[settlement.scenario_id]
            )
            if gap <= band:
                expected += 1

    result = measure(Profile.BULK, 200)
    assert result.matched_by_scenario.get("SC-003-near-amount-difference", 0) == expected
    assert 0 < expected < result.lines_by_scenario["SC-003-near-amount-difference"], (
        "the corpus must contain both cleared and residual near misses for this to be meaningful"
    )


# --------------------------------------------------------------------------------------
# The firewall
# --------------------------------------------------------------------------------------


def test_the_measurement_needs_construction_metadata_that_the_matcher_never_receives() -> None:
    """The firewall, stated where it is most tempting to breach.

    This file grades the matcher using ``scenario_id``. The matcher itself is handed
    :class:`CandidateLine` and :class:`CandidateEntry`, and neither type has a field for it — so the
    comparison this file performs is one production code physically cannot make.
    """
    for candidate in (CandidateLine, CandidateEntry):
        fields = set(candidate.__dataclass_fields__)
        assert "scenario_id" not in fields
        assert not fields & {"intended_classification", "intent", "awkwardness", "memo"}

    corpus = generate(SEED, Profile.CANONICAL)
    assert corpus.corpus.batches[0].lines[0].scenario_id, (
        "the corpus must carry the metadata this file grades against"
    )
