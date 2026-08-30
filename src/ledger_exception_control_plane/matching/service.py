"""Loading candidates, running the matcher, and persisting what it decided.

The decision itself is pure and lives in :mod:`.engine`. This module does the three things that
need a database: read a consistent set of candidates, write each accepted pair atomically with the
line state that vouches for it, and lose safely when another worker got there first.

**The two writes never diverge.** ``match_result`` and ``settlement_line.match_state`` are written
in one transaction, and the state is set only for the pairs whose ``match_result`` this transaction
actually inserted. A line marked matched with no result, or a result with the line still unmatched,
is not a state this code can produce.

**Concurrency is resolved by the constraints, not by hope.** ``match_result`` is unique on the line
and unique on the entry, and inserts go in with ``ON CONFLICT DO NOTHING``. A worker that loses a
race simply does not insert that pair, does not mark that line, and leaves it unmatched — which is
the correct retryable state, because the next run reads the world as it now is. No in-process lock
is involved, and none would help: the competing worker is another process.

**A line under exception control is not matchable.** Once M2.3 has raised an exception for a
residual, that line has an open control record and a decision path of its own; matching it later —
after a fresh ledger snapshot, say — would silently revoke a claim the system had already made,
leaving one line with two contradictory resolutions and no record of the reversal. The database
refuses it outright (ADR-044), so this module excludes such lines from eligibility and re-checks
under a row lock before writing. That keeps the foreign key as a backstop rather than as the
mechanism: without the re-check a single line acquiring an exception mid-run would abort the whole
matching transaction.

**This module still classifies nothing.** It reads whether an exception exists and nothing about
what it says — not its class, not its status. Reversing that claim is what M2.3's workflow owns.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.db.control import ExceptionRecord
from ledger_exception_control_plane.db.models import (
    LedgerEntry,
    MatchResult,
    MatchState,
    SettlementLine,
)
from ledger_exception_control_plane.matching.engine import (
    CandidateEntry,
    CandidateLine,
    MatchOutcome,
    match,
)
from ledger_exception_control_plane.matching.policy import DEFAULT_POLICY, TolerancePolicy


@dataclasses.dataclass(frozen=True, slots=True)
class MatchRun:
    """What one pass over the eligible work did.

    ``considered`` minus ``matched`` is not the residual: it is the residual *plus* whatever
    another worker took first. The two are distinguished by ``lost_races``, because a line left
    unmatched by a race is retryable and a line left unmatched by the rules is not.
    """

    considered: int
    matched: int
    ambiguous: int
    unmatched: int
    lost_races: int

    # Counts only, deliberately. An earlier version exposed a `cleared_fraction: float` for
    # reporting; the package's own no-float guard caught it. The guard is right and the property
    # was wrong — a ratio on a business result invites something downstream to branch on it, and
    # the caller that wants a percentage can divide two integers where the intent is visible.


async def run_matching(
    engine: AsyncEngine,
    *,
    matched_at: dt.datetime,
    policy: TolerancePolicy = DEFAULT_POLICY,
    batch_id: uuid.UUID | None = None,
) -> MatchRun:
    """Match the eligible settlement lines against the unconsumed ledger entries.

    ``matched_at`` is supplied by the caller rather than read from the clock, for the same reason
    ingestion takes its receipt time as an argument: it is an operational fact, on a replay it is
    not now, and a business path that reads a clock cannot be asserted deterministically.

    Idempotent in what it *writes*: a second run over an unchanged database produces no new
    ``match_result`` rows, changes no line state, and does not rewrite the ``matched_at`` of an
    earlier decision.

    It is not a no-op, and an earlier version of this docstring wrongly said it was. Residual and
    ambiguous lines keep ``match_state`` unmatched by design — that is what makes them residual —
    so every run reconsiders them and reports them again. On the canonical corpus a second run
    returns ``considered=13, matched=0``, not a run of zeroes. Repeated matching is safe, not free.
    """
    async with AsyncSession(engine) as session:
        # The read gets its own transaction, explicitly. SQLAlchemy's autobegin would otherwise
        # open one on the first SELECT and leave it open, and the write below would then fail with
        # "a transaction is already begun" — which is what happened, and what the integration
        # suite caught before any of this ran anywhere real.
        #
        # Two transactions rather than one spanning the whole run: the decision is computed with
        # no transaction held, so a large batch does not keep one open while it thinks. The read
        # can go stale in between, and that is handled where it has to be — the unique constraints
        # arbitrate, and a pair another worker took is skipped rather than forced.
        async with session.begin():
            lines, entries = await _load_candidates(session, batch_id)

        outcome = match(lines, entries, policy)
        inserted = await _persist(session, outcome, matched_at)

    return MatchRun(
        considered=len(lines),
        matched=len(inserted),
        ambiguous=len(outcome.ambiguous_line_ids),
        unmatched=len(outcome.unmatched_line_ids),
        lost_races=len(outcome.matches) - len(inserted),
    )


async def _load_candidates(
    session: AsyncSession, batch_id: uuid.UUID | None
) -> tuple[list[CandidateLine], list[CandidateEntry]]:
    """Read the eligible work.

    Eligibility is expressed in SQL rather than filtered in Python, so the database does not hand
    back rows the matcher would only discard — a settlement line already matched, or a ledger entry
    already consumed, is not a candidate and there is no reason to carry it.

    The ledger side is narrowed by the currencies actually present in the lines. Beyond that it is
    not narrowed further: a date-range predicate would need the policy window, and at this scale it
    would trade a readable query for an optimisation nothing has measured. The access path is
    recorded in PROJECT_STATUS so a later increment with real volume has somewhere to start.
    """
    line_query = select(
        SettlementLine.id,
        SettlementLine.line_number,
        SettlementLine.amount,
        SettlementLine.currency,
        SettlementLine.value_date,
    ).where(
        SettlementLine.match_state == MatchState.UNMATCHED,
        ~select(MatchResult.id).where(MatchResult.settlement_line_id == SettlementLine.id).exists(),
        # An open control record makes the line M2.3's, not this module's. See the docstring.
        ~select(ExceptionRecord.id)
        .where(ExceptionRecord.settlement_line_id == SettlementLine.id)
        .exists(),
    )
    if batch_id is not None:
        line_query = line_query.where(SettlementLine.settlement_batch_id == batch_id)
    # Explicit ordering. The engine does not depend on it, and an unordered query would still be
    # correct — but a deterministic read makes a failure reproducible, which an unordered one does
    # not.
    line_query = line_query.order_by(SettlementLine.line_number, SettlementLine.id)

    lines = [
        CandidateLine(
            id=row.id,
            line_number=row.line_number,
            amount=row.amount,
            currency=row.currency,
            value_date=row.value_date,
        )
        for row in (await session.execute(line_query)).all()
    ]
    if not lines:
        return [], []

    entry_query = (
        select(
            LedgerEntry.id,
            LedgerEntry.external_ref,
            LedgerEntry.amount,
            LedgerEntry.currency,
            # UTC, explicitly. ``booked_at`` is TIMESTAMPTZ, and PostgreSQL resolves a
            # timestamptz-to-date cast in the *session's* TimeZone setting — which nothing here
            # pins, so a plain cast would make a hard eligibility filter depend on an ambient
            # database configuration. The same rows would then reconcile differently on two
            # servers, and because a consumed ledger entry can never be released (ADR-024) the
            # divergence would be unrecoverable.
            #
            # Reducing an instant to a calendar day requires *choosing* a zone; UTC is chosen
            # because it is what the column stores, what the Python side computes, and the only
            # answer that does not vary with where the database happens to be configured.
            cast(func.timezone("UTC", LedgerEntry.booked_at), Date).label("booked_on"),
        )
        .where(
            LedgerEntry.currency.in_({line.currency for line in lines}),
            ~select(MatchResult.id).where(MatchResult.ledger_entry_id == LedgerEntry.id).exists(),
        )
        .order_by(LedgerEntry.external_ref, LedgerEntry.id)
    )
    entries = [
        CandidateEntry(
            id=row.id,
            external_ref=row.external_ref,
            amount=row.amount,
            currency=row.currency,
            booked_on=row.booked_on,
        )
        for row in (await session.execute(entry_query)).all()
    ]
    return lines, entries


async def _persist(
    session: AsyncSession, outcome: MatchOutcome, matched_at: dt.datetime
) -> list[uuid.UUID]:
    """Write the accepted pairs. Returns the line ids actually matched by this transaction.

    ``ON CONFLICT DO NOTHING`` covers both unique constraints — the line and the entry — so a pair
    lost to a concurrent worker is skipped rather than aborting the run. The line state is then set
    only for the pairs that really landed, which is what keeps the two writes from diverging.

    The lines are locked in id order first, and any that acquired an exception since the read are
    dropped. Both halves matter. Without the lock, M2.3 could raise an exception between the check
    and the update, and the composite foreign key would then abort this whole transaction over one
    line. Without the shared id ordering, two writers holding rows the other wants could deadlock.
    M2.3 takes the same lock in the same order and drops the lines *this* module matched, so
    whichever gets there second observes the other's decision instead of colliding with it.
    """
    if not outcome.matches:
        return []

    inserted: list[uuid.UUID] = []
    async with session.begin():
        proposed_line_ids = sorted({proposed.line_id for proposed in outcome.matches})
        await session.execute(
            select(SettlementLine.id)
            .where(SettlementLine.id.in_(proposed_line_ids))
            .order_by(SettlementLine.id)
            .with_for_update()
        )
        under_exception = set(
            (
                await session.execute(
                    select(ExceptionRecord.settlement_line_id).where(
                        ExceptionRecord.settlement_line_id.in_(proposed_line_ids)
                    )
                )
            )
            .scalars()
            .all()
        )

        for proposed in outcome.matches:
            if proposed.line_id in under_exception:
                continue
            statement = (
                pg_insert(MatchResult)
                .values(
                    id=uuid.uuid4(),
                    settlement_line_id=proposed.line_id,
                    ledger_entry_id=proposed.entry_id,
                    rule_id=proposed.rule.value,
                    tolerance_applied=proposed.tolerance_applied,
                    tolerance_currency=proposed.tolerance_currency,
                    matched_at=matched_at,
                )
                .on_conflict_do_nothing()
                .returning(MatchResult.settlement_line_id)
            )
            claimed = (await session.execute(statement)).scalar_one_or_none()
            if claimed is not None:
                inserted.append(claimed)

        if inserted:
            await session.execute(
                update(SettlementLine)
                .where(SettlementLine.id.in_(inserted))
                .values(match_state=MatchState.MATCHED)
            )

    return inserted
