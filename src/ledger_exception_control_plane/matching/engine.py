"""The matching decision, as a pure function. No database, no clock, no randomness.

Given the eligible lines, the eligible ledger entries and a policy, this returns the same matches
every time — and, importantly, the same matches regardless of the order the inputs arrive in.

**Why order-independence needed designing for rather than asserting.** The obvious implementation
walks the lines and lets each take the first candidate it likes. That is greedy, and greedy is
order-dependent: two settlement lines of 10.00 with one ledger entry of 10.00 produce a different
winner depending on which line is considered first. A financial match decided by the order rows came
back from a query is a defect even when both answers look reasonable, because nothing about the
business says the first row wins.

So the rule is **mutual uniqueness**: a pair is accepted only when the line has exactly one eligible
candidate *and* that candidate is claimed by exactly one line. Anything else is ambiguous and stays
unmatched. Two lines competing for one entry match nothing — which is the honest answer, because the
system genuinely cannot tell which movement the entry represents, and consuming the wrong one would
put a real discrepancy beyond reach.

**Ambiguity is an outcome, not an error.** It is reported, not raised, and it leaves the line
exactly where an unmatched line belongs. See ADR-043.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import decimal
import uuid
from collections.abc import Iterable, Sequence

from ledger_exception_control_plane.matching.policy import MatchRule, TolerancePolicy

#: Rule precedence. Exact before tolerance, and the order is explicit rather than implied by the
#: order two ``if`` branches happen to be written in.
RULE_PRECEDENCE: tuple[MatchRule, ...] = (MatchRule.EXACT_AMOUNT, MatchRule.AMOUNT_WITHIN_TOLERANCE)


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateLine:
    """A settlement line as the matcher sees it. Deliberately not the ORM row.

    Five fields, and no reference, no memo, no transaction type, no scenario label. What the matcher
    cannot see, it cannot accidentally decide on.
    """

    id: uuid.UUID
    line_number: int
    amount: decimal.Decimal
    currency: str
    value_date: dt.date


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateEntry:
    """A ledger entry as the matcher sees it.

    ``booked_on`` is a date, not the stored timestamp: the comparison is against a settlement
    *value date*, and carrying a time here would invite a comparison the source cannot support.
    """

    id: uuid.UUID
    external_ref: str
    amount: decimal.Decimal
    currency: str
    booked_on: dt.date


@dataclasses.dataclass(frozen=True, slots=True)
class ProposedMatch:
    """One accepted pair, with the evidence ``match_result`` will record."""

    line_id: uuid.UUID
    entry_id: uuid.UUID
    rule: MatchRule
    tolerance_applied: decimal.Decimal | None
    tolerance_currency: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class MatchOutcome:
    """What the matcher decided, including what it declined to decide.

    ``ambiguous_line_ids`` is reported because "no unique candidate" and "no candidate at all" are
    different facts about a line, and a later increment reading this will want to tell them apart.
    Neither produces a row here: both leave the line unmatched.
    """

    matches: tuple[ProposedMatch, ...]
    ambiguous_line_ids: frozenset[uuid.UUID]
    unmatched_line_ids: frozenset[uuid.UUID]


def _eligible(
    line: CandidateLine, entry: CandidateEntry, rule: MatchRule, policy: TolerancePolicy
) -> decimal.Decimal | None:
    """The absorbed difference if this pair satisfies ``rule``, otherwise ``None``.

    Returns ``Decimal("0")`` for an exact match, which is not the same as ``None`` — the caller
    distinguishes "no match" from "match absorbing nothing".
    """
    # Hard filters, applied to every rule. Currency equality is absolute: this system performs no
    # conversion (§3 lists a conversion policy engine as a non-goal), so two amounts in different
    # currencies are not near each other, they are incomparable. The presentment and FX columns the
    # file carries are not consulted here and must not be — using them would be implementing FX
    # matching, which no rule in this increment declares.
    if line.currency != entry.currency:
        return None
    if not policy.within_window(line.value_date, entry.booked_on):
        return None

    difference = abs(line.amount - entry.amount)

    if rule is MatchRule.EXACT_AMOUNT:
        # Decimal equality, not float. `1828.49 - 1828.47` is exactly `0.02` here; in binary
        # floating point it is not.
        return decimal.Decimal(0) if difference == 0 else None

    band = policy.band(line.currency)
    if band is None:
        # No declared band for this currency means no tolerance at all — see TolerancePolicy.band.
        return None
    # Inclusive. The policy states the largest difference that may be absorbed, so that value is
    # admissible; an exclusive reading would make the documented number the first one refused.
    return difference if difference <= band else None


def _accept_mutually_unique(
    lines: Sequence[CandidateLine],
    entries: Sequence[CandidateEntry],
    rule: MatchRule,
    policy: TolerancePolicy,
) -> tuple[list[ProposedMatch], set[uuid.UUID], set[uuid.UUID]]:
    """Accept every pair that is the unique choice from both sides.

    Returns the accepted matches, the lines left ambiguous, and the **entries those ambiguous
    lines were contesting** — the third value is what lets :func:`match` keep an unresolved
    higher-tier contest out of the tiers below it.

    Set-based rather than iterative: the candidate graph is built in full before anything is
    accepted, so no line's outcome depends on whether another line was considered first.
    """
    candidates: dict[uuid.UUID, list[tuple[CandidateEntry, decimal.Decimal]]] = {}
    claimants: collections.defaultdict[uuid.UUID, list[uuid.UUID]] = collections.defaultdict(list)

    for line in lines:
        for entry in entries:
            absorbed = _eligible(line, entry, rule, policy)
            if absorbed is not None:
                candidates.setdefault(line.id, []).append((entry, absorbed))
                claimants[entry.id].append(line.id)

    matches: list[ProposedMatch] = []
    ambiguous: set[uuid.UUID] = set()
    contested: set[uuid.UUID] = set()

    for line in lines:
        options = candidates.get(line.id, [])
        if len(options) != 1:
            if options:
                ambiguous.add(line.id)
                contested.update(entry.id for entry, _ in options)
            continue
        entry, absorbed = options[0]
        if len(claimants[entry.id]) != 1:
            # The line has one candidate, but that candidate is wanted by another line too. Neither
            # can be accepted without guessing which movement the entry represents.
            ambiguous.add(line.id)
            contested.add(entry.id)
            continue
        matches.append(
            ProposedMatch(
                line_id=line.id,
                entry_id=entry.id,
                rule=rule,
                tolerance_applied=None if rule is MatchRule.EXACT_AMOUNT else absorbed,
                tolerance_currency=None if rule is MatchRule.EXACT_AMOUNT else line.currency,
            )
        )

    return matches, ambiguous, contested


def match(
    lines: Iterable[CandidateLine],
    entries: Iterable[CandidateEntry],
    policy: TolerancePolicy,
) -> MatchOutcome:
    """Decide which lines match which ledger entries.

    Rules are applied in :data:`RULE_PRECEDENCE`, and a rule's accepted pairs are removed before the
    next rule runs. Exact therefore beats tolerance: a line with an exact candidate is settled by it
    and never reaches the tolerance rule, and an entry consumed exactly is no longer available to
    absorb a near miss.

    **An unresolved contest is withdrawn from every tier below it.** A line left ambiguous by a rule
    is removed from the pool, and so is every entry it was contesting. Without the second half the
    first would be actively harmful: blocking only the line would release the entries it was
    claiming, and a *tolerance* match could then take an entry that an *exact* claim was still
    contesting — precedence inverted by the very step meant to protect it.

    Today this changes nothing. Exact candidates are a subset of tolerance candidates, so an
    exact-ambiguous line would still see two candidates at the tolerance tier and remain ambiguous
    on its own; the tests below pass identically with and without this block. It is made explicit
    because that safety is an *accident of these two rules* — it holds only while every lower tier
    is a superset of every higher one, and nothing in the code said so. A future rule that selected
    a different candidate set would silently start resolving higher-tier ambiguity at a lower tier,
    which is exactly the failure this increment must not allow.
    """
    # A total ordering on the inputs. The acceptance rule is set-based and so does not depend on
    # this, but the *output* order does, and a deterministic output makes the persisted sequence and
    # the test assertions stable.
    remaining_lines = sorted(lines, key=lambda line: (line.line_number, line.id.bytes))
    remaining_entries = sorted(entries, key=lambda entry: (entry.external_ref, entry.id.bytes))
    all_line_ids = frozenset(line.id for line in remaining_lines)

    matches: list[ProposedMatch] = []
    ambiguous: set[uuid.UUID] = set()

    for rule in RULE_PRECEDENCE:
        accepted, rule_ambiguous, contested = _accept_mutually_unique(
            remaining_lines, remaining_entries, rule, policy
        )
        matches.extend(accepted)
        ambiguous |= rule_ambiguous

        settled_lines = {match.line_id for match in accepted} | rule_ambiguous
        settled_entries = {match.entry_id for match in accepted} | contested
        remaining_lines = [line for line in remaining_lines if line.id not in settled_lines]
        remaining_entries = [
            entry for entry in remaining_entries if entry.id not in settled_entries
        ]

    settled = {match.line_id for match in matches}
    # Ambiguous lines are withdrawn from the pool above, so no later rule can match one. The
    # subtraction is a belt-and-braces assertion of that, kept because the two sets are maintained
    # separately and a future edit could let them disagree.
    ambiguous -= settled
    return MatchOutcome(
        matches=tuple(matches),
        ambiguous_line_ids=frozenset(ambiguous),
        unmatched_line_ids=frozenset(all_line_ids - settled - ambiguous),
    )
