"""Loading residuals, running the classifier, and persisting the exceptions it decided.

The decision itself is pure and lives in :mod:`.engine`. This module does the three things that
need a database: read the residual work and the movements that might explain it, write one
exception per residual, and lose safely when another worker got there first.

**Eligibility is narrow and stated in SQL.** A line becomes an exception only when it belongs to a
batch that actually parsed, M2.2 left it unmatched, no ``match_result`` claims it, and no exception
already does. Quarantine needs no clause of its own: a quarantined batch has no persisted lines at
all — ADR-040 condemns the whole file rather than trusting a subset — so there is nothing there to
classify. The batch-status filter is written anyway, because "there happen to be no rows" is a
property of another increment's implementation and this one should not silently depend on it.

**One residual, one exception, enforced by the database.** ``exception`` is unique on
``settlement_line_id``, inserts go in with ``ON CONFLICT DO NOTHING``, and a second run over an
unchanged database writes nothing. No in-process lock is involved and none would help: the
competing worker is another process.

**The matched-line invariant is closed on both sides.** ``exception`` carries a composite foreign
key onto ``(settlement_line.id, 'unmatched')`` (ADR-044), so the database refuses an exception for a
reconciled line however it is written. That leaves a real race between this module and M2.2 — a line
could be matched between the read here and the insert — and a foreign key resolves it by *failing*,
which is safe but would abort a whole run over one line. So both writers take a row lock in the same
order and re-check under it: M2.2 drops lines that acquired an exception, this module drops lines
that acquired a match. The foreign key stops being the mechanism and becomes what it should be, the
backstop for anything that bypasses both.

**The lock covers the evidence, not only the subject**, and that is the part a first version got
wrong. A classification is derived from the state of *other* rows, so locking the line being
classified is not enough: three unreconciled rows on one order read as a ``fee_split``, and if the
gross is matched before the write lands, two fee rows are persisted as a split whose capture has
gone. The composite key cannot catch it — the rows it constrains are still unmatched, and the
conclusion is wrong for a reason no constraint can see. So the residuals *and* the movements that
explain them are locked together, re-read under that lock, and only then classified.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.classification.engine import (
    Classification,
    SettlementMovement,
    classify,
)
from ledger_exception_control_plane.classification.taxonomy import (
    CLASSIFIER_VERSION,
    movement_type,
)
from ledger_exception_control_plane.db.control import ExceptionRecord, ExceptionStatus
from ledger_exception_control_plane.db.models import (
    BatchStatus,
    MatchResult,
    MatchState,
    SettlementBatch,
    SettlementLine,
)


@dataclasses.dataclass(frozen=True, slots=True)
class ClassificationRun:
    """What one pass over the residual work did.

    ``residuals`` minus ``created`` is not a failure count: it is whatever another worker resolved
    first, reported separately as ``lost_races`` so that a line skipped because someone else
    classified or matched it is distinguishable from one this run decided about.
    """

    residuals: int
    created: int
    lost_races: int
    by_classification: dict[str, int]


@dataclasses.dataclass(frozen=True, slots=True)
class _Residual:
    """A residual line and the ingestion facts its correlation id is derived from."""

    movement: SettlementMovement
    line_number: int
    content_hash: str


def correlation_id_for(content_hash: str, line_number: int) -> str:
    """The correlation id an exception for this line carries (§11).

    Derived from the ingested artefact rather than taken from an ambient request context, and that
    is deliberate. §11 requires the id to span ingestion through to posting; deriving it from the
    content hash of the file the line arrived in and its position within that file makes the span a
    property of the data instead of a value some caller has to remember to thread through. It also
    makes it *stable*: re-running classification after a crash produces the same id, where a
    request-scoped id would produce a new one for the same economic event.

    Content hash rather than batch id because the hash is the identity of the payload (FR-1), so a
    re-delivery of the same file yields the same correlation id for the same line.
    """
    return f"lecp:{content_hash}:{line_number:06d}"


async def run_classification(
    engine: AsyncEngine,
    *,
    batch_id: uuid.UUID | None = None,
) -> ClassificationRun:
    """Classify the residual settlement lines and create one exception for each.

    Takes no timestamp, unlike :func:`..matching.run_matching`. There is no business time here to
    supply: ``exception.created_at`` records when the durable control record came into existence,
    which is a row-lifecycle fact the database owns through ``server_default=now()``. A clock
    argument would only invite something to be decided by it.

    Idempotent. A second run over an unchanged database creates no exceptions and reports
    ``residuals=0``, because a line carrying an exception is no longer eligible.

    **One transaction, unlike matching, and the difference is not stylistic.** Matching computes its
    decision on a read it has already released, because the decision concerns one line and one entry
    and the unique constraints arbitrate it at write time — a stale proposal simply loses. A
    classification is not like that: it is derived from the *state of other rows*, and no constraint
    can arbitrate that. Deciding ``fee_split`` from a group of three unreconciled rows and then
    writing it after one of them has been matched persists a conclusion whose evidence no longer
    exists, and the composite key cannot see it because the row it constrains — the subject — is
    still perfectly unmatched. So every line the decision reads is locked, re-read under that lock
    and only then classified.
    """
    async with AsyncSession(engine) as session, session.begin():
        snapshot = await _load_residuals(session, batch_id)
        if not snapshot:
            return ClassificationRun(0, 0, 0, {})

        # Lock everything the decision will read — the residuals *and* the movements that
        # explain them — in id order. Matching's persistence takes the same lock in the same
        # order, so the two serialise instead of deadlocking, and whichever arrives second sees
        # the other's committed decision.
        pinned = await _lock(
            session,
            {r.movement.id for r in snapshot}
            | {m.id for m in await _load_context(session, snapshot)},
        )

        # Re-read under the lock. Anything that moved between the two reads is caught here:
        # a residual that acquired a match or an exception drops out of the eligibility
        # predicate, and a context line that acquired a match comes back with a different
        # match state, so the group it belonged to is re-evaluated rather than assumed.
        residuals = [r for r in await _load_residuals(session, batch_id) if r.movement.id in pinned]
        context = [m for m in await _load_context(session, residuals) if m.id in pinned]

        decisions = classify([r.movement for r in residuals], context)
        created = await _persist(session, residuals, decisions)

    by_classification: dict[str, int] = {}
    for decision in decisions:
        if decision.line_id in created:
            key = decision.classification.value
            by_classification[key] = by_classification.get(key, 0) + 1

    return ClassificationRun(
        residuals=len(snapshot),
        created=len(created),
        lost_races=len(snapshot) - len(created),
        by_classification=by_classification,
    )


async def _lock(session: AsyncSession, line_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Take a row lock on every settlement line the decision depends on, in id order.

    Returns the ids actually locked. A line that appeared *after* the snapshot is not in this set
    and is deliberately left for the next run: it is a newer world than the one being classified,
    and classifying half of it would be worse than classifying none.
    """
    if not line_ids:
        return set()
    locked = (
        await session.execute(
            select(SettlementLine.id)
            .where(SettlementLine.id.in_(sorted(line_ids)))
            .order_by(SettlementLine.id)
            .with_for_update()
        )
    ).scalars()
    return set(locked.all())


async def _load_residuals(session: AsyncSession, batch_id: uuid.UUID | None) -> list[_Residual]:
    """Read the lines eligible to become exceptions, with their ingestion provenance.

    Eligibility is expressed in SQL rather than filtered in Python, so the database does not hand
    back rows this module would only discard — and so the definition of "residual" is one readable
    predicate rather than a condition spread across a loop.
    """
    query = (
        select(
            SettlementLine.id,
            SettlementLine.line_number,
            SettlementLine.merchant_reference,
            SettlementLine.transaction_type,
            SettlementLine.amount,
            SettlementLine.currency,
            SettlementLine.value_date,
            SettlementBatch.content_hash,
        )
        .join(SettlementBatch, SettlementLine.settlement_batch_id == SettlementBatch.id)
        .where(
            SettlementBatch.status == BatchStatus.PARSED,
            SettlementLine.match_state == MatchState.UNMATCHED,
            ~select(MatchResult.id)
            .where(MatchResult.settlement_line_id == SettlementLine.id)
            .exists(),
            ~select(ExceptionRecord.id)
            .where(ExceptionRecord.settlement_line_id == SettlementLine.id)
            .exists(),
        )
        .order_by(SettlementLine.id)
    )
    if batch_id is not None:
        query = query.where(SettlementLine.settlement_batch_id == batch_id)

    return [
        _Residual(
            movement=SettlementMovement(
                id=row.id,
                merchant_reference=row.merchant_reference,
                # Reduced to the closed vocabulary here, at the boundary, so nothing downstream
                # ever sees the raw string. An unrecognised type becomes None and no rule can fire
                # on it — the classifier fails closed on a movement it does not understand.
                movement=movement_type(row.transaction_type),
                amount=row.amount,
                currency=row.currency,
                value_date=row.value_date,
                matched=False,
            ),
            line_number=row.line_number,
            content_hash=row.content_hash,
        )
        for row in (await session.execute(query)).all()
    ]


async def _load_context(
    session: AsyncSession, residuals: list[_Residual]
) -> list[SettlementMovement]:
    """Read every settlement line that could explain one of these residuals.

    Related lines are the ones sharing a residual's merchant reference, **whatever their match
    state** — the reversal rules turn on a counterpart the ledger already booked, so restricting
    this to unmatched lines would remove exactly the evidence they need.

    Not restricted by batch either. A refund settling in the month after its capture is in a
    different file, which is the whole point of the cross-period case, and a same-batch restriction
    would silently unclassify it.
    """
    references = {
        residual.movement.merchant_reference
        for residual in residuals
        if residual.movement.merchant_reference is not None
    }
    if not references:
        return []

    query = (
        select(
            SettlementLine.id,
            SettlementLine.merchant_reference,
            SettlementLine.transaction_type,
            SettlementLine.amount,
            SettlementLine.currency,
            SettlementLine.value_date,
            SettlementLine.match_state,
        )
        .join(SettlementBatch, SettlementLine.settlement_batch_id == SettlementBatch.id)
        .where(
            SettlementBatch.status == BatchStatus.PARSED,
            SettlementLine.merchant_reference.in_(references),
        )
        .order_by(SettlementLine.id)
    )
    return [
        SettlementMovement(
            id=row.id,
            merchant_reference=row.merchant_reference,
            movement=movement_type(row.transaction_type),
            amount=row.amount,
            currency=row.currency,
            value_date=row.value_date,
            matched=row.match_state == MatchState.MATCHED,
        )
        for row in (await session.execute(query)).all()
    ]


async def _persist(
    session: AsyncSession,
    residuals: list[_Residual],
    decisions: tuple[Classification, ...],
) -> set[uuid.UUID]:
    """Write one exception per residual. Returns the line ids this transaction actually claimed.

    Called with the caller's transaction already open and every line it depends on already locked
    and re-read, so there is nothing left to re-check here — a line M2.2 matched in the meantime
    dropped out of the eligibility predicate before the decision was taken, and the composite
    foreign key never has to reject anything.

    ``ON CONFLICT DO NOTHING`` covers the one remaining race: a second classifier that queued behind
    this transaction's lock and, on acquiring it, still sees an eligible line. The loser writes
    nothing rather than failing.
    """
    if not decisions:
        return set()

    provenance = {residual.movement.id: residual for residual in residuals}
    created: set[uuid.UUID] = set()

    for decision in decisions:
        residual = provenance[decision.line_id]
        statement = (
            pg_insert(ExceptionRecord)
            .values(
                id=uuid.uuid4(),
                settlement_line_id=decision.line_id,
                line_match_state=MatchState.UNMATCHED,
                classification=decision.classification,
                status=ExceptionStatus.OPEN,
                rule_id=decision.rule_id.value,
                classifier_version=CLASSIFIER_VERSION,
                correlation_id=correlation_id_for(residual.content_hash, residual.line_number),
            )
            .on_conflict_do_nothing()
            .returning(ExceptionRecord.settlement_line_id)
        )
        claimed = (await session.execute(statement)).scalar_one_or_none()
        if claimed is not None:
            created.add(claimed)

    return created
