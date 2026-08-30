"""Ingestion against real PostgreSQL — the properties a unit test cannot establish.

Everything here is about *persisted state*: that the receipt survives a parse failure, that a
rejected batch leaves no trusted rows behind, that re-delivery is a no-op the database arbitrates,
and that an interrupted attempt can be finished rather than restarted.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_ingest_postgres.py -m integration
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import hashlib
import os
import pathlib
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import create_engine
from ledger_exception_control_plane.db.models import BatchStatus
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ingest import QuarantineCode, content_hash, ingest
from ledger_exception_control_plane.ingest.parser import SETTLEMENT_COLUMNS

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "fixtures" / "canonical"

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

#: A fixed receipt time. Real ingestion passes the actual arrival instant; the tests pass a
#: constant so nothing in the assertions depends on when they ran.
RECEIVED_AT = dt.datetime(2026, 6, 15, 8, 30, tzinfo=dt.UTC)

HEADER = ",".join(SETTLEMENT_COLUMNS)
VALID_ROW = "psp_ingest_0001,ORD-2026-500001,capture,120.45,EUR,2026-06-03,,,,capture"


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


def payload(*rows: str, header: str = HEADER) -> bytes:
    return ("\n".join((header, *rows)) + "\n").encode("utf-8")


def unique_payload(marker: str, *, amount: str = "120.45", currency: str = "EUR") -> bytes:
    """A distinct valid payload per test, so tests never collide on the content hash."""
    return payload(
        f"psp_{marker},ORD-2026-500001,capture,{amount},{currency},2026-06-03,,,,capture"
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """Head schema from zero. Disposability is checked before anything destructive runs."""
    assert_target_is_disposable(_settings())
    env = {**os.environ, "LECP_POSTGRES_DSN": DSN}
    for args in (("downgrade", "base"), ("upgrade", "head")):
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    yield


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_engine(_settings())
    try:
        yield created
    finally:
        await created.dispose()


async def _batch(digest: str) -> asyncpg.Record:
    connection = await asyncpg.connect(DSN)
    try:
        row = await connection.fetchrow(
            "SELECT id, status, quarantine_reason, raw_payload, content_hash, received_at,"
            " source FROM settlement_batch WHERE content_hash = $1",
            digest,
        )
        assert row is not None, "the receipt must exist"
        return row
    finally:
        await connection.close()


async def _line_count(batch_id: uuid.UUID) -> int:
    connection = await asyncpg.connect(DSN)
    try:
        count = await connection.fetchval(
            "SELECT count(*) FROM settlement_line WHERE settlement_batch_id = $1", batch_id
        )
        assert isinstance(count, int)
        return count
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_payload_is_parsed_and_its_lines_persisted(engine: AsyncEngine) -> None:
    raw = unique_payload("happy01")
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    assert outcome.accepted
    assert outcome.status is BatchStatus.PARSED
    assert outcome.line_count == 1
    assert outcome.quarantine_reason is None
    assert outcome.duplicate is False

    row = await _batch(content_hash(raw))
    assert row["status"] == "parsed"
    assert row["quarantine_reason"] is None
    assert row["received_at"] == RECEIVED_AT
    assert row["source"] == "file-drop"
    assert await _line_count(outcome.batch_id) == 1


@pytest.mark.asyncio
async def test_the_stored_raw_payload_is_the_original_bytes(engine: AsyncEngine) -> None:
    """Immutable, unrewritten, and the hash is of exactly these bytes (FR-1)."""
    raw = b"\xef\xbb\xbf" + unique_payload("rawbytes01")
    outcome = await ingest(engine, raw, source="webhook", received_at=RECEIVED_AT)
    assert outcome.accepted, "a BOM must not stop the file being read"

    row = await _batch(content_hash(raw))
    assert bytes(row["raw_payload"]) == raw, "the payload must not be canonicalised on the way in"
    assert row["content_hash"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_persisted_amounts_are_exact_and_carry_their_currency(engine: AsyncEngine) -> None:
    raw = unique_payload("money01", amount="1.2345", currency="BHD")
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)
    assert outcome.accepted

    connection = await asyncpg.connect(DSN)
    try:
        row = await connection.fetchrow(
            "SELECT amount, currency, value_date, match_state FROM settlement_line"
            " WHERE settlement_batch_id = $1",
            outcome.batch_id,
        )
        assert row is not None
        assert row["amount"] == decimal.Decimal("1.2345")
        assert row["currency"] == "BHD"
        assert row["value_date"] == dt.date(2026, 6, 3)
        # Matching is M2.2. Ingestion has compared this line with nothing.
        assert row["match_state"] == "unmatched"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_committed_canonical_settlement_file_ingests_end_to_end(
    engine: AsyncEngine,
) -> None:
    """The corpus M1.3 committed, taken through the real ingestion path into the real schema."""
    raw = (CORPUS / "settlement/psp-settlement-2026-06.csv").read_bytes()
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    assert outcome.accepted, outcome.quarantine_reason
    assert outcome.line_count == 16
    assert await _line_count(outcome.batch_id) == 16


# --------------------------------------------------------------------------------------
# Quarantine
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("invalid/over-precise-amount.csv", QuarantineCode.AMOUNT_PRECISION_EXCEEDED),
        ("invalid/missing-column.csv", QuarantineCode.HEADER_MISMATCH),
        ("invalid/bad-currency.csv", QuarantineCode.INVALID_CURRENCY),
        ("invalid/unparseable-amount.csv", QuarantineCode.INVALID_AMOUNT),
    ],
)
@pytest.mark.asyncio
async def test_the_committed_malformed_fixtures_quarantine_with_the_right_reason(
    engine: AsyncEngine, name: str, expected: QuarantineCode
) -> None:
    """The files M1.3 committed for exactly this path, each landing on its declared defect."""
    raw = (CORPUS / name).read_bytes()
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    assert outcome.status is BatchStatus.QUARANTINED
    assert outcome.line_count == 0
    assert outcome.quarantine_reason is not None
    assert expected.value in outcome.quarantine_reason
    assert await _line_count(outcome.batch_id) == 0


@pytest.mark.asyncio
async def test_the_receipt_survives_a_parse_failure(engine: AsyncEngine) -> None:
    """FR-1's ordering, observed in the database rather than argued from the code.

    A parser that ran before persistence would leave nothing behind for a malformed file, and the
    operator would have a quarantine record referring to bytes nobody kept.
    """
    raw = b"this is not a settlement file at all\n"
    outcome = await ingest(engine, raw, source="webhook", received_at=RECEIVED_AT)

    assert outcome.status is BatchStatus.QUARANTINED
    row = await _batch(content_hash(raw))
    assert bytes(row["raw_payload"]) == raw
    assert row["status"] == "quarantined"
    assert row["quarantine_reason"] is not None


@pytest.mark.asyncio
async def test_a_batch_with_one_bad_row_persists_no_lines_at_all(engine: AsyncEngine) -> None:
    """Batch-level quarantine, proven against storage: no trusted subset survives (ADR-040)."""
    raw = payload(
        "psp_partial_a,ORD-1,capture,10.00,EUR,2026-06-03,,,,ok",
        "psp_partial_b,ORD-2,capture,not-a-number,EUR,2026-06-03,,,,bad",
        "psp_partial_c,ORD-3,capture,30.00,EUR,2026-06-03,,,,ok",
    )
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    assert outcome.status is BatchStatus.QUARANTINED
    assert await _line_count(outcome.batch_id) == 0

    connection = await asyncpg.connect(DSN)
    try:
        leaked = await connection.fetchval(
            "SELECT count(*) FROM settlement_line WHERE psp_reference = ANY($1::text[])",
            ["psp_partial_a", "psp_partial_c"],
        )
        assert leaked == 0, "a valid row from a rejected batch reached the database"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_a_stored_quarantine_reason_is_bounded_and_carries_no_internals(
    engine: AsyncEngine,
) -> None:
    rows = [
        f"psp_reason_{index},ORD-1,capture,bad-{index},EUR,2026-06-03,,,,x" for index in range(40)
    ]
    outcome = await ingest(engine, payload(*rows), source="file-drop", received_at=RECEIVED_AT)

    reason = outcome.quarantine_reason
    assert reason is not None
    assert len(reason) <= 512
    for marker in ("Traceback", "asyncpg", "postgresql://", "bad-0", "psp_reason_0"):
        assert marker not in reason
    assert "more)" in reason, "truncation must be visible, not silent"


# --------------------------------------------------------------------------------------
# Re-delivery
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_re_delivery_creates_no_second_batch_or_line(engine: AsyncEngine) -> None:
    """FR-1: re-delivery of an identical batch must not create duplicate work."""
    raw = unique_payload("redeliver01")
    first = await ingest(engine, raw, source="webhook", received_at=RECEIVED_AT)
    second = await ingest(
        engine, raw, source="webhook", received_at=RECEIVED_AT + dt.timedelta(hours=2)
    )

    assert first.accepted
    assert second.batch_id == first.batch_id
    assert second.duplicate is True
    assert second.status is BatchStatus.PARSED
    assert second.line_count == first.line_count

    connection = await asyncpg.connect(DSN)
    try:
        batches = await connection.fetchval(
            "SELECT count(*) FROM settlement_batch WHERE content_hash = $1", content_hash(raw)
        )
        assert batches == 1
        # The receipt is not rewritten: the first arrival time stands.
        stored = await connection.fetchval(
            "SELECT received_at FROM settlement_batch WHERE content_hash = $1", content_hash(raw)
        )
        assert stored == RECEIVED_AT
    finally:
        await connection.close()
    assert await _line_count(first.batch_id) == 1


@pytest.mark.asyncio
async def test_re_delivery_of_a_quarantined_batch_is_also_a_no_op(engine: AsyncEngine) -> None:
    raw = payload("psp_requar,ORD-1,capture,nope,EUR,2026-06-03,,,,x")
    first = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)
    second = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    assert first.status is BatchStatus.QUARANTINED
    assert second.status is BatchStatus.QUARANTINED
    assert second.duplicate is True
    assert second.quarantine_reason == first.quarantine_reason


@pytest.mark.asyncio
async def test_two_concurrent_deliveries_of_one_payload_produce_exactly_one_batch(
    engine: AsyncEngine,
) -> None:
    """The database is the final guard, not a lookup.

    Check-then-insert has a window in which both callers find nothing and both insert. Running
    them genuinely concurrently, on separate engines, is the only way to show the window is closed
    rather than merely narrow.
    """
    raw = unique_payload("concurrent01")
    first_engine = create_engine(_settings())
    second_engine = create_engine(_settings())
    try:
        outcomes = await asyncio.gather(
            ingest(first_engine, raw, source="webhook", received_at=RECEIVED_AT),
            ingest(second_engine, raw, source="webhook", received_at=RECEIVED_AT),
            return_exceptions=True,
        )
    finally:
        await first_engine.dispose()
        await second_engine.dispose()

    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), f"concurrent ingestion raised: {outcome!r}"

    connection = await asyncpg.connect(DSN)
    try:
        batches = await connection.fetchval(
            "SELECT count(*) FROM settlement_batch WHERE content_hash = $1", content_hash(raw)
        )
        assert batches == 1, "the unique index must make the race impossible"
        lines = await connection.fetchval(
            "SELECT count(*) FROM settlement_line l JOIN settlement_batch b"
            " ON b.id = l.settlement_batch_id WHERE b.content_hash = $1",
            content_hash(raw),
        )
        assert lines == 1, "concurrent delivery must not double the lines"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_unique_index_still_refuses_a_hand_written_duplicate(
    engine: AsyncEngine,
) -> None:
    """The guard is the constraint, and ingestion has not weakened it."""
    raw = unique_payload("guard01")
    await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO settlement_batch"
                " (id, content_hash, source, raw_payload, received_at, status)"
                " VALUES (gen_random_uuid(), $1, 'x', $2, now(), 'received')",
                content_hash(raw),
                raw,
            )
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# Interruption and integrity failure
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interrupted_attempt_is_completed_rather_than_abandoned(
    engine: AsyncEngine,
) -> None:
    """A crash between the two transactions leaves a receipt with no lines. Re-delivering the same
    payload finishes the work — which is not duplicate work, it is the work that was interrupted.
    """
    raw = unique_payload("resume01")
    digest = content_hash(raw)

    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES (gen_random_uuid(), $1, 'file-drop', $2, $3, 'received')",
            digest,
            raw,
            RECEIVED_AT,
        )
    finally:
        await connection.close()

    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)
    assert outcome.accepted
    assert outcome.duplicate is True, "the receipt already existed"
    assert outcome.line_count == 1
    assert await _line_count(outcome.batch_id) == 1


@pytest.mark.asyncio
async def test_an_integrity_failure_during_line_persistence_leaves_no_partial_state(
    engine: AsyncEngine,
) -> None:
    """Line 1 already exists, so the resume violates ``uq_settlement_line_batch_line_number``.

    The whole of T2 rolls back: no lines are added, and the batch stays at ``received`` rather
    than being marked parsed over an incomplete write. Loud, and recoverable.
    """
    raw = payload(
        "psp_integrity_a,ORD-1,capture,10.00,EUR,2026-06-03,,,,one",
        "psp_integrity_b,ORD-2,capture,20.00,EUR,2026-06-03,,,,two",
    )
    digest = content_hash(raw)

    connection = await asyncpg.connect(DSN)
    try:
        batch_id = await connection.fetchval(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES (gen_random_uuid(), $1, 'file-drop', $2, $3, 'received') RETURNING id",
            digest,
            raw,
            RECEIVED_AT,
        )
        await connection.execute(
            "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
            " amount, currency, value_date, match_state)"
            " VALUES (gen_random_uuid(), $1, 1, 'squatter', 1.0000, 'EUR', '2026-06-03',"
            " 'unmatched')",
            batch_id,
        )

        with pytest.raises(Exception, match=r"uq_settlement_line|duplicate key"):
            await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

        status = await connection.fetchval(
            "SELECT status FROM settlement_batch WHERE id = $1", batch_id
        )
        assert status == "received", "the batch must not be marked parsed over a failed write"
        lines = await connection.fetchval(
            "SELECT count(*) FROM settlement_line WHERE settlement_batch_id = $1", batch_id
        )
        assert lines == 1, "only the pre-existing row survives; nothing partial was added"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_ingestion_never_disables_a_database_constraint(engine: AsyncEngine) -> None:
    """No ``session_replication_role``, no deferred checks, no dropped constraint.

    Verified by asking the database to accept something the schema forbids, after ingestion has
    run: if ingestion had loosened anything, this would succeed.
    """
    await ingest(
        engine, unique_payload("constraints01"), source="file-drop", received_at=RECEIVED_AT
    )

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SHOW session_replication_role") == "origin"
        batch_id = await connection.fetchval(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES (gen_random_uuid(), $1, 'x', 'y', now(), 'received') RETURNING id",
            "c" * 64,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
                " amount, currency, value_date, match_state)"
                " VALUES (gen_random_uuid(), $1, 1, 'over-precise', 1.23456, 'EUR', '2026-06-03',"
                " 'unmatched')",
                batch_id,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_ingestion_creates_no_row_in_any_later_increment_s_table(
    engine: AsyncEngine,
) -> None:
    """M2.1 ingests. It does not match, classify, or raise an exception record."""
    await ingest(engine, unique_payload("scope01"), source="file-drop", received_at=RECEIVED_AT)

    connection = await asyncpg.connect(DSN)
    try:
        for table in ("match_result", "exception", "evidence", "treatment_proposal", "adjustment"):
            count = await connection.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"ingestion wrote to {table}, which belongs to a later increment"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_a_nul_in_a_reference_quarantines_instead_of_jamming_the_batch(
    engine: AsyncEngine,
) -> None:
    """The poison pill, proven closed against the database that would have rejected the INSERT.

    Before the fix this normalised cleanly, failed at INSERT with SQLSTATE 22021, and — because the
    receipt was already committed and the payload is immutable — reproduced the same failure on
    every re-delivery. The batch could never reach a terminal state. It now quarantines, and the
    second delivery is an ordinary no-op.
    """
    raw = payload("psp_nul\x00ref,ORD-1,capture,10.00,EUR,2026-06-03,,,,x")
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)

    assert outcome.status is BatchStatus.QUARANTINED
    assert outcome.quarantine_reason is not None
    assert QuarantineCode.UNSTORABLE_CHARACTER.value in outcome.quarantine_reason
    assert await _line_count(outcome.batch_id) == 0

    # And the batch is genuinely terminal, not stuck: re-delivery reports the same outcome.
    again = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)
    assert again.status is BatchStatus.QUARANTINED
    assert again.duplicate is True
    assert again.quarantine_reason == outcome.quarantine_reason


@pytest.mark.asyncio
async def test_an_amount_with_trailing_zeros_beyond_four_places_persists(
    engine: AsyncEngine,
) -> None:
    """ADR-020's value-based rule, end to end.

    ``120.450000`` loses nothing at four decimal places, so ingestion accepts it and the column's
    ``trunc(amount, 4) = amount`` accepts it too. The read path canonicalises to four places, which
    is the M1.1 ``Money`` type doing its job rather than a rounding.
    """
    raw = unique_payload("trailingzeros01", amount="120.450000")
    outcome = await ingest(engine, raw, source="file-drop", received_at=RECEIVED_AT)
    assert outcome.accepted, outcome.quarantine_reason

    connection = await asyncpg.connect(DSN)
    try:
        stored = await connection.fetchval(
            "SELECT amount FROM settlement_line WHERE settlement_batch_id = $1", outcome.batch_id
        )
        assert stored == decimal.Decimal("120.45")
    finally:
        await connection.close()
