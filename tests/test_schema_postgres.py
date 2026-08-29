"""Schema behaviour that only a real PostgreSQL can prove.

Marked ``integration`` and excluded from the default suite, so ordinary testing stays
Docker-free. These verify what metadata assertions cannot: that Alembic actually applies
from zero to head, that the resulting schema matches the models with no drift, that
downgrade genuinely reverses, and that the check and unique constraints *reject* bad rows
rather than merely being declared.

Run against the project's Compose PostgreSQL::

    docker compose up -d --wait postgres
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest -m integration
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

EXPECTED_TABLES = {"settlement_batch", "settlement_line", "ledger_entry", "match_result"}
M1_2_TABLES = {
    "exception",
    "evidence",
    "treatment_proposal",
    "approval",
    "adjustment",
    "outbox",
    "posting_attempt",
    "dlq",
    "recovery_queue",
    "audit_event",
}


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run Alembic in-process configuration, against the test database."""
    env = {**os.environ, "LECP_POSTGRES_DSN": DSN}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """Bring the test database to head from zero before the module runs."""
    downgraded = _alembic("downgrade", "base")
    assert downgraded.returncode == 0, downgraded.stderr
    upgraded = _alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    yield


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(DSN)


# --------------------------------------------------------------------------------------
# Migration mechanics
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_creates_exactly_the_m1_1_tables() -> None:
    connection = await _connect()
    try:
        rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await connection.close()

    tables = {row["tablename"] for row in rows}
    assert tables >= EXPECTED_TABLES
    assert "alembic_version" in tables
    assert tables - EXPECTED_TABLES == {"alembic_version"}, "unexpected tables were created"


@pytest.mark.asyncio
async def test_no_m1_2_tables_exist_in_the_migrated_database() -> None:
    connection = await _connect()
    try:
        rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await connection.close()

    leaked = M1_2_TABLES & {row["tablename"] for row in rows}
    assert not leaked, f"M1.2 tables present after M1.1 migration: {sorted(leaked)}"


def test_downgrade_then_upgrade_is_clean() -> None:
    """Reversibility is project policy (NFR-4), so it is exercised, not assumed."""
    down = _alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr


def test_no_model_migration_drift() -> None:
    """Autogenerate against the migrated database must find nothing to do.

    This is the check that catches a model changed without a migration — the failure mode
    where every migration still succeeds while the schema quietly diverges from the code.
    """
    result = _alembic("check")
    assert result.returncode == 0, (
        f"model and migration have drifted:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --------------------------------------------------------------------------------------
# Constraints must actually reject bad rows
# --------------------------------------------------------------------------------------


async def _insert_batch(connection: asyncpg.Connection, content_hash: str) -> uuid.UUID:
    batch_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO settlement_batch
            (id, content_hash, source, raw_payload, received_at, status)
        VALUES ($1, $2, 'test', $3, now(), 'received')
        """,
        batch_id,
        content_hash,
        b"raw",
    )
    return batch_id


@pytest.mark.asyncio
async def test_duplicate_content_hash_is_rejected() -> None:
    """FR-1: re-delivery of an identical batch must not create duplicate work."""
    digest = "a" * 64
    connection = await _connect()
    try:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await _insert_batch(connection, digest)

        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_batch(connection, digest)
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.close()


@pytest.mark.asyncio
async def test_content_hash_must_be_sha256_hex() -> None:
    connection = await _connect()
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_batch(connection, "not-a-sha256")
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_quarantined_batch_requires_a_reason() -> None:
    connection = await _connect()
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO settlement_batch
                    (id, content_hash, source, raw_payload, received_at, status)
                VALUES ($1, $2, 'test', $3, now(), 'quarantined')
                """,
                uuid.uuid4(),
                "b" * 64,
                b"raw",
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_currency_must_be_three_upper_case_letters() -> None:
    connection = await _connect()
    digest = "c" * 64
    try:
        batch_id = await _insert_batch(connection, digest)
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO settlement_line
                    (id, settlement_batch_id, line_number, psp_reference,
                     amount, currency, value_date, match_state)
                VALUES ($1, $2, 1, 'psp-1', 10.0000, 'eur', current_date, 'unmatched')
                """,
                uuid.uuid4(),
                batch_id,
            )
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.close()


@pytest.mark.asyncio
async def test_monetary_value_round_trips_without_precision_loss() -> None:
    """The point of NUMERIC over floating point, demonstrated end to end."""
    import decimal

    connection = await _connect()
    digest = "d" * 64
    awkward = decimal.Decimal("0.1234")
    try:
        batch_id = await _insert_batch(connection, digest)
        line_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO settlement_line
                (id, settlement_batch_id, line_number, psp_reference,
                 amount, currency, value_date, match_state)
            VALUES ($1, $2, 1, 'psp-1', $3, 'EUR', current_date, 'unmatched')
            """,
            line_id,
            batch_id,
            awkward,
        )
        stored = await connection.fetchval(
            "SELECT amount FROM settlement_line WHERE id = $1", line_id
        )
        assert isinstance(stored, decimal.Decimal)
        assert stored == awkward
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.close()


@pytest.mark.asyncio
async def test_a_ledger_entry_cannot_be_matched_twice() -> None:
    """Double-consumption of a ledger entry would make the ledger appear to reconcile twice."""
    connection = await _connect()
    digest = "e" * 64
    external_ref = f"entry-{uuid.uuid4()}"
    try:
        batch_id = await _insert_batch(connection, digest)
        entry_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO ledger_entry
                (id, external_ref, account_code, amount, currency, booked_at)
            VALUES ($1, $2, 'acct', 10.0000, 'EUR', now())
            """,
            entry_id,
            external_ref,
        )

        line_ids = [uuid.uuid4(), uuid.uuid4()]
        for number, line_id in enumerate(line_ids, start=1):
            await connection.execute(
                """
                INSERT INTO settlement_line
                    (id, settlement_batch_id, line_number, psp_reference,
                     amount, currency, value_date, match_state)
                VALUES ($1, $2, $3, 'psp', 10.0000, 'EUR', current_date, 'unmatched')
                """,
                line_id,
                batch_id,
                number,
            )

        await connection.execute(
            """
            INSERT INTO match_result
                (id, settlement_line_id, ledger_entry_id, rule_id, matched_at)
            VALUES ($1, $2, $3, 'exact', now())
            """,
            uuid.uuid4(),
            line_ids[0],
            entry_id,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                """
                INSERT INTO match_result
                    (id, settlement_line_id, ledger_entry_id, rule_id, matched_at)
                VALUES ($1, $2, $3, 'exact', now())
                """,
                uuid.uuid4(),
                line_ids[1],
                entry_id,
            )
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.execute("DELETE FROM ledger_entry WHERE external_ref = $1", external_ref)
        await connection.close()


@pytest.mark.asyncio
async def test_tolerance_amount_and_currency_must_agree() -> None:
    """An amount without its currency, or a currency without its amount, is rejected."""
    connection = await _connect()
    digest = "f" * 64
    external_ref = f"entry-{uuid.uuid4()}"
    try:
        batch_id = await _insert_batch(connection, digest)
        entry_id = uuid.uuid4()
        line_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO ledger_entry
                (id, external_ref, account_code, amount, currency, booked_at)
            VALUES ($1, $2, 'acct', 10.0000, 'EUR', now())
            """,
            entry_id,
            external_ref,
        )
        await connection.execute(
            """
            INSERT INTO settlement_line
                (id, settlement_batch_id, line_number, psp_reference,
                 amount, currency, value_date, match_state)
            VALUES ($1, $2, 1, 'psp', 10.0000, 'EUR', current_date, 'unmatched')
            """,
            line_id,
            batch_id,
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO match_result
                    (id, settlement_line_id, ledger_entry_id, rule_id,
                     tolerance_applied, matched_at)
                VALUES ($1, $2, $3, 'tolerance', 0.0100, now())
                """,
                uuid.uuid4(),
                line_id,
                entry_id,
            )
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.execute("DELETE FROM ledger_entry WHERE external_ref = $1", external_ref)
        await connection.close()


@pytest.mark.asyncio
async def test_created_at_is_populated_by_the_database() -> None:
    """Inserting without created_at must still yield a timezone-aware value."""
    connection = await _connect()
    digest = "0" * 64
    try:
        batch_id = await _insert_batch(connection, digest)
        created_at = await connection.fetchval(
            "SELECT created_at FROM settlement_batch WHERE id = $1", batch_id
        )
        assert created_at is not None
        assert created_at.tzinfo is not None, "created_at must be timezone-aware"
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.close()
