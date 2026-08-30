"""Ingestion orchestration: receipt, then interpretation. In that order, and it matters.

FR-1 requires the raw payload to be persisted **before parsing**, and this module takes that
literally: the receipt is written and committed in its own transaction, and only then is the
payload read. The ordering is the difference between a system that can show you the file it
rejected and one that can only tell you a file arrived once.

Two transactions, deliberately:

* **T1 — the receipt.** ``settlement_batch`` with the original bytes, the hash of those bytes and
  status ``received``. Committed before anything looks at the contents.
* **T2 — the outcome.** Either the normalised lines plus status ``parsed``, or status
  ``quarantined`` with a reason. One transaction, so a batch can never hold a partially trusted
  subset of a file: the lines and the status that vouches for them commit together or not at all.

A crash between the two leaves the batch at ``received`` with no lines — a state that is both
recoverable and honest, and re-delivering the same payload completes it rather than starting
again. That is not duplicate work; it is the work that was interrupted.

**T2 claims the batch with ``SELECT ... FOR UPDATE`` before deciding anything.** Two concurrent
deliveries of one payload both reach it: the first inserts the receipt, the second finds the receipt
already there and still at ``received``, and without the lock both would conclude they should do the
interpretation. Found by running two ingests concurrently -- the unique index on
``(settlement_batch_id, line_number)`` caught the second write, which is the guard working, but
the loser got an integrity error where a re-delivery is supposed to be a no-op. The lock makes the
second caller wait for the first to commit and then observe a finished batch. See ADR-041.

**Batch-level quarantine.** FR-2 says invalid *batches* are quarantined, and one bad row condemns
the whole file. The alternative — accepting the rows that happened to parse — would manufacture a
trusted partial settlement file, and reconciliation over a partial file does not produce fewer
results, it produces *wrong* ones: every movement missing from the accepted subset looks like an
unexplained residual. See ADR-040.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger_exception_control_plane.db.models import BatchStatus, SettlementBatch, SettlementLine
from ledger_exception_control_plane.ingest.errors import Defect, render_reason
from ledger_exception_control_plane.ingest.normalise import NormalisedLine, normalise
from ledger_exception_control_plane.ingest.parser import parse


@dataclasses.dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What ingestion did. Closed, and never a bare boolean.

    ``duplicate`` distinguishes "this payload had already been received" from "this payload is
    new", which the caller needs in order to tell a re-delivery from first contact — FR-1 requires
    re-delivery to create no duplicate work, not to be indistinguishable from work.
    """

    batch_id: uuid.UUID
    status: BatchStatus
    line_count: int
    quarantine_reason: str | None
    duplicate: bool

    @property
    def accepted(self) -> bool:
        return self.status is BatchStatus.PARSED


def content_hash(payload: bytes) -> str:
    """SHA-256 of the bytes exactly as received.

    Hashed before decoding, before any BOM is stripped, before anything is normalised. The hash
    identifies the artifact that arrived, not a cleaned-up version of it — otherwise two different
    files could share a hash, and the re-delivery guard would suppress a genuinely new batch.
    """
    return hashlib.sha256(payload).hexdigest()


async def ingest(
    engine: AsyncEngine,
    payload: bytes,
    *,
    source: str,
    received_at: dt.datetime,
) -> IngestOutcome:
    """Ingest one settlement payload.

    ``received_at`` is supplied by the caller rather than read from the clock here. It is an
    operational fact — when the file actually arrived — and on replay it is not now. Keeping it a
    parameter also keeps every business value in this path deterministic, which is what lets a
    test assert normalisation rather than tolerate it.
    """
    digest = content_hash(payload)

    async with AsyncSession(engine) as session:
        batch_id, was_new = await _record_receipt(session, payload, digest, source, received_at)

        return await _settle(session, batch_id, payload, duplicate=not was_new)


async def _record_receipt(
    session: AsyncSession,
    payload: bytes,
    digest: str,
    source: str,
    received_at: dt.datetime,
) -> tuple[uuid.UUID, bool]:
    """T1. Returns the batch id and whether this call created it.

    ``ON CONFLICT DO NOTHING`` on the unique ``content_hash`` index, not a SELECT followed by an
    INSERT. Check-then-insert has a window: two deliveries of the same payload can both find
    nothing and both insert, and one of them then fails on the constraint anyway — so the
    constraint was doing the work all along while the lookup provided false reassurance. Letting
    the index arbitrate makes the race impossible rather than unlikely, and it is the same
    reasoning §12.2 applies to ``operation_id``: the database is the guarantee.
    """
    async with session.begin():
        statement = (
            pg_insert(SettlementBatch)
            .values(
                id=uuid.uuid4(),
                content_hash=digest,
                source=source,
                raw_payload=payload,
                received_at=received_at,
                status=BatchStatus.RECEIVED,
            )
            .on_conflict_do_nothing(index_elements=["content_hash"])
            .returning(SettlementBatch.id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return inserted, True

        existing_id = (
            await session.execute(
                select(SettlementBatch.id).where(SettlementBatch.content_hash == digest)
            )
        ).scalar_one()
        return existing_id, False


async def _settle(
    session: AsyncSession, batch_id: uuid.UUID, payload: bytes, *, duplicate: bool
) -> IngestOutcome:
    """T2. Claim the batch, then commit exactly one outcome for it.

    Interpretation happens *before* the transaction opens, so the row lock is held only for the
    writes. Under a concurrent re-delivery that work is wasted — but wasted parsing is cheap and a
    lock held across it is not, and correctness does not depend on which order those two happen in:
    the status is re-read under the lock either way.
    """
    lines, defects = interpret(payload)

    async with session.begin():
        # Claim it. Whoever gets here second waits, then sees a batch that is already finished and
        # reports that rather than interpreting it a second time.
        row = (
            await session.execute(
                select(SettlementBatch.status, SettlementBatch.quarantine_reason)
                .where(SettlementBatch.id == batch_id)
                .with_for_update()
            )
        ).one()
        status, existing_reason = row

        if status != BatchStatus.RECEIVED:
            # Already parsed or already quarantined. Nothing is rewritten: the payload is immutable
            # and re-running normalisation over it could only agree or reveal that the code has
            # changed — neither of which justifies mutating a financial record.
            line_count = (
                await session.execute(
                    select(func.count())
                    .select_from(SettlementLine)
                    .where(SettlementLine.settlement_batch_id == batch_id)
                )
            ).scalar_one()
            return IngestOutcome(
                batch_id, BatchStatus(status), line_count, existing_reason, duplicate=True
            )

        if defects:
            reason = render_reason(list(defects))
            await session.execute(
                update(SettlementBatch)
                .where(SettlementBatch.id == batch_id)
                .values(status=BatchStatus.QUARANTINED, quarantine_reason=reason)
            )
            return IngestOutcome(batch_id, BatchStatus.QUARANTINED, 0, reason, duplicate)

        for line in lines:
            session.add(
                SettlementLine(
                    id=uuid.uuid4(),
                    settlement_batch_id=batch_id,
                    line_number=line.line_number,
                    psp_reference=line.psp_reference,
                    merchant_reference=line.merchant_reference,
                    amount=line.amount,
                    currency=line.currency,
                    value_date=line.value_date,
                    # match_state is left at its default. Matching is M2.2; a line that arrived
                    # this second has not been compared with anything.
                )
            )
        await session.execute(
            update(SettlementBatch)
            .where(SettlementBatch.id == batch_id)
            .values(status=BatchStatus.PARSED, quarantine_reason=None)
        )

    return IngestOutcome(batch_id, BatchStatus.PARSED, len(lines), None, duplicate)


def interpret(payload: bytes) -> tuple[tuple[NormalisedLine, ...], tuple[Defect, ...]]:
    """Parse then normalise. Pure — no database, no clock, no I/O.

    Public because it is the whole interpretation of a settlement file and is worth testing on its
    own: everything this increment decides about a payload is decided here, and none of it needs a
    database to observe.
    """
    parsed = parse(payload)
    if not parsed.ok:
        return (), parsed.defects

    lines: list[NormalisedLine] = []
    defects: list[Defect] = []
    for row in parsed.rows:
        line, row_defects = normalise(row)
        if line is None:
            defects.extend(row_defects)
        else:
            lines.append(line)

    # One bad row condemns the batch. The valid lines are discarded rather than persisted —
    # see the module docstring and ADR-040.
    if defects:
        return (), tuple(defects)
    return tuple(lines), ()
