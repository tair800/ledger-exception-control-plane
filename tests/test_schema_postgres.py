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

import asyncio
import decimal
import hashlib
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
from pydantic import SecretStr

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

M1_1_TABLES = {"settlement_batch", "settlement_line", "ledger_entry", "match_result"}
M1_2_TABLES = {
    "exception",
    "evidence",
    "treatment_proposal",
    "treatment_proposal_evidence",
    "approval",
    "adjustment",
    "outbox",
    "posting_attempt",
    "dlq",
    "recovery_queue",
    "audit_event",
}
#: The one table added since M1.2, and the only one. §13.5 requires resolving an ``UNKNOWN`` to
#: ``REJECTED`` to rest on "N consecutive queries", and the only faithful way to hold that count is
#: append-only rows rather than a column somebody can set — see the metadata suite for the full
#: reasoning. A counter would have fitted the existing schema and been the wrong answer.
M4_4_TABLES = {"reconciliation_query"}

EXPECTED_TABLES = M1_1_TABLES | M1_2_TABLES | M4_4_TABLES

#: Indicative names from later increments. See the metadata suite for why this is a smoke
#: alarm rather than a contract.
LATER_TABLES = {"cassette", "golden_case", "eval_run", "approval_token", "adapter_capability"}

#: Least-privilege application role, provisioned by the script rather than by a migration.
APP_ROLE = "lecp_app"
PROVISION_SQL = REPO_ROOT / "scripts" / "sql" / "provision_app_role.sql"


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
    """Bring the test database to head from zero before the module runs.

    Disposability is checked first. This module drops every table, and it takes its DSN from
    the environment; the same hazard was found in the M1.3 fixture suite by review, and it
    applies here identically. Predates M1.3 — corrected here because the fix is two lines and
    the failure mode is somebody's database.
    """
    assert_target_is_disposable(Settings(postgres_dsn=SecretStr(DSN)))

    downgraded = _alembic("downgrade", "base")
    assert downgraded.returncode == 0, downgraded.stderr
    upgraded = _alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    asyncio.run(_provision_app_role())
    yield


async def _provision_app_role() -> None:
    """Apply the role-provisioning script, exactly as a release would.

    Deliberately after the migration: ``GRANT ... ON ALL TABLES`` applies to the tables that
    exist when it runs, so a release that granted before migrating would leave every new table
    ungranted. Running it here also means the script itself is exercised rather than assumed.
    """
    connection = await _connect()
    try:
        await connection.execute(PROVISION_SQL.read_text(encoding="utf-8"))
    finally:
        await connection.close()


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(DSN)


# --------------------------------------------------------------------------------------
# Migration mechanics
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_creates_exactly_the_m1_1_and_m1_2_tables() -> None:
    connection = await _connect()
    try:
        rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await connection.close()

    tables = {row["tablename"] for row in rows}
    assert tables >= EXPECTED_TABLES, f"missing: {sorted(EXPECTED_TABLES - tables)}"
    assert "alembic_version" in tables
    assert tables - EXPECTED_TABLES == {"alembic_version"}, "unexpected tables were created"


@pytest.mark.asyncio
async def test_no_later_increment_tables_exist_in_the_migrated_database() -> None:
    connection = await _connect()
    try:
        rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await connection.close()

    leaked = LATER_TABLES & {row["tablename"] for row in rows}
    assert not leaked, f"post-M1.2 tables present: {sorted(leaked)}"


def test_downgrade_then_upgrade_is_clean() -> None:
    """Reversibility is project policy (NFR-4), so it is exercised, not assumed."""
    down = _alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    # Dropping and recreating the tables discards every grant on them, so any later test in
    # this module would run against an unprovisioned database. That is not a test-ordering
    # nuisance to work around — it is the real deployment rule (ADR-026) that provisioning
    # follows migration, and a release that skipped it would leave the application unable to
    # write. Re-applying here mirrors what a release does.
    asyncio.run(_provision_app_role())


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


# --------------------------------------------------------------------------------------
# No silent rounding at the persistence boundary
#
# The initial schema used NUMERIC(20, 4). Measured against it, Decimal("1.23456") was
# stored as 1.2346 with no error and no warning — the application could not tell the value
# had changed. These tests pin the corrected behaviour: reject, never round.
# --------------------------------------------------------------------------------------


async def _insert_amount(
    connection: asyncpg.Connection, batch_id: uuid.UUID, line_number: int, amount: object
) -> uuid.UUID:
    line_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO settlement_line
            (id, settlement_batch_id, line_number, psp_reference,
             amount, currency, value_date, match_state)
        VALUES ($1, $2, $3, 'precision-probe', $4, 'EUR', current_date, 'unmatched')
        """,
        line_id,
        batch_id,
        line_number,
        amount,
    )
    return line_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("four decimal places", "1.2345"),
        ("negative four places", "-1.2345"),
        ("integer", "100"),
        ("zero", "0"),
        ("trailing zeros", "1.2300"),
        ("more trailing zeros", "1.230000"),
        ("maximum magnitude", "9999999999999999.9999"),
        ("maximum negative magnitude", "-9999999999999999.9999"),
    ],
)
async def test_valid_monetary_values_persist_exactly(label: str, value: str) -> None:
    """Accepted values must round-trip exactly, not merely approximately.

    ``1.230000`` is included deliberately: it is numerically identical to ``1.2300`` and
    loses nothing when stored, so a ``scale()``-based rule that rejected it would be wrong.
    """
    expected = decimal.Decimal(value)
    connection = await _connect()
    digest = hashlib.sha256(f"ok:{label}".encode()).hexdigest()
    try:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        batch_id = await _insert_batch(connection, digest)
        line_id = await _insert_amount(connection, batch_id, 1, expected)

        stored = await connection.fetchval(
            "SELECT amount FROM settlement_line WHERE id = $1", line_id
        )
        assert isinstance(stored, decimal.Decimal)
        assert stored == expected, f"{label}: {expected} came back as {stored}"
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("five decimal places", "1.23456"),
        ("negative five places", "-1.23456"),
        ("tiny sub-scale value", "0.00005"),
        ("one over maximum magnitude", "10000000000000000"),
        ("one under minimum magnitude", "-10000000000000000"),
    ],
)
async def test_excess_precision_or_magnitude_is_rejected_not_rounded(
    label: str, value: str
) -> None:
    """The defect this correction exists for: the write must FAIL, not quietly succeed."""
    connection = await _connect()
    digest = hashlib.sha256(f"reject:{label}".encode()).hexdigest()
    try:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        batch_id = await _insert_batch(connection, digest)

        with pytest.raises(asyncpg.CheckViolationError) as error:
            await _insert_amount(connection, batch_id, 1, decimal.Decimal(value))

        name = error.value.constraint_name or ""
        assert name.endswith(("_scale", "_magnitude")), (
            f"{label} was rejected, but by the wrong constraint: {name}"
        )
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.close()


@pytest.mark.asyncio
async def test_no_monetary_column_carries_a_fixed_scale_typmod() -> None:
    """**Regression guard, at the database.**

    A fixed-scale ``NUMERIC(p, s)`` rounds before any check constraint can see the value.
    This fails if a future migration reintroduces one — which the metadata test alone could
    not catch, because a migration can change the database without changing the models.
    """
    connection = await _connect()
    try:
        rows = await connection.fetch(
            """
            SELECT table_name, column_name, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name IN ('amount', 'tolerance_applied')
            """
        )
    finally:
        await connection.close()

    assert rows, "no monetary columns found — the query is wrong, not the schema"
    offenders = [
        f"{r['table_name']}.{r['column_name']} is "
        f"NUMERIC({r['numeric_precision']},{r['numeric_scale']})"
        for r in rows
        if r["numeric_scale"] is not None
    ]
    assert not offenders, f"fixed-scale monetary columns silently round: {offenders}"


@pytest.mark.asyncio
async def test_nullable_tolerance_still_obeys_pairing_and_precision_rules() -> None:
    """The correction must not weaken the existing nullable-tolerance rules."""
    connection = await _connect()
    digest = hashlib.sha256(b"tolerance-precision").hexdigest()
    external_ref = f"entry-{uuid.uuid4()}"
    try:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        batch_id = await _insert_batch(connection, digest)
        entry_id = uuid.uuid4()
        line_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)
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

        # A NULL tolerance still passes the precision check — a bare CHECK yields NULL, not
        # FALSE — and still satisfies the currency pairing rule.
        match_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO match_result (id, settlement_line_id, ledger_entry_id, rule_id, matched_at)
            VALUES ($1, $2, $3, 'exact', now())
            """,
            match_id,
            line_id,
            entry_id,
        )
        assert (
            await connection.fetchval(
                "SELECT tolerance_applied FROM match_result WHERE id = $1", match_id
            )
            is None
        )

        # An over-precise tolerance is rejected like any other monetary value.
        await connection.execute("DELETE FROM match_result WHERE id = $1", match_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO match_result
                    (id, settlement_line_id, ledger_entry_id, rule_id,
                     tolerance_applied, tolerance_currency, matched_at)
                VALUES ($1, $2, $3, 'tolerance', $4, 'EUR', now())
                """,
                uuid.uuid4(),
                line_id,
                entry_id,
                decimal.Decimal("0.00001"),
            )
    finally:
        await connection.execute("DELETE FROM settlement_batch WHERE content_hash = $1", digest)
        await connection.execute("DELETE FROM ledger_entry WHERE external_ref = $1", external_ref)
        await connection.close()


@pytest.mark.asyncio
async def test_money_reads_back_canonicalised_regardless_of_written_scale() -> None:
    """The same economic value must return one canonical ``Decimal``.

    Dropping the fixed typmod also dropped a guarantee that was easy to overlook:
    ``NUMERIC(20, 4)`` normalised every read to four places, so ``1.23`` and ``1.230000``
    came back identical. Unconstrained ``NUMERIC`` preserves the writer's scale, so without
    the ``Money`` type decorator the same amount would surface as two different ``Decimal``
    objects — differently hashed and differently serialised. ADR-004b derives ``operation_id``
    from a hash covering the amount, so a representation-dependent value would silently
    defeat idempotency.
    """
    import datetime as dt

    import sqlalchemy as sa
    from pydantic import SecretStr

    from ledger_exception_control_plane.config import Settings
    from ledger_exception_control_plane.db.engine import create_engine
    from ledger_exception_control_plane.db.models import SettlementBatch, SettlementLine

    engine = create_engine(Settings(postgres_dsn=SecretStr(DSN)))
    digest = hashlib.sha256(b"canonicalisation").hexdigest()
    written = [decimal.Decimal("1.23"), decimal.Decimal("1.2300"), decimal.Decimal("1.230000")]
    batch_id = uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.delete(SettlementBatch).where(SettlementBatch.content_hash == digest)
            )
            await connection.execute(
                sa.insert(SettlementBatch).values(
                    id=batch_id,
                    content_hash=digest,
                    source="canonical",
                    raw_payload=b"x",
                    received_at=dt.datetime.now(dt.UTC),
                    status="received",
                )
            )
            for number, value in enumerate(written, start=1):
                await connection.execute(
                    sa.insert(SettlementLine).values(
                        id=uuid.uuid4(),
                        settlement_batch_id=batch_id,
                        line_number=number,
                        psp_reference="canonical",
                        amount=value,
                        currency="EUR",
                        value_date=dt.date.today(),
                        match_state="unmatched",
                    )
                )

        async with engine.connect() as connection:
            result = await connection.execute(
                sa.select(SettlementLine.amount)
                .where(SettlementLine.settlement_batch_id == batch_id)
                .order_by(SettlementLine.line_number)
            )
            returned = [row[0] for row in result.all()]

        assert all(isinstance(v, decimal.Decimal) for v in returned)
        # Identical exponents, not merely equal values: str() must agree.
        rendered = {str(v) for v in returned}
        assert rendered == {"1.2300"}, (
            f"the same value came back in different representations: {rendered}. "
            f"Written as {[str(w) for w in written]}."
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.delete(SettlementBatch).where(SettlementBatch.content_hash == digest)
            )
        await engine.dispose()


# ======================================================================================
# M1.2 — exception, resolution and reliability schema
#
# Every test here builds a real chain (batch -> line -> exception -> approval -> adjustment)
# inside a transaction that is always rolled back, so the tests leave no rows behind and do
# not depend on each other's ordering. A rejection is asserted inside a nested transaction
# (a savepoint), because a constraint violation aborts the enclosing transaction and would
# otherwise make the rest of the test unusable.
# ======================================================================================


def _hex(seed: str) -> str:
    """A syntactically valid sha256 hex digest, derived from a label so failures are legible."""
    return hashlib.sha256(seed.encode()).hexdigest()


class _Chain:
    """Ids of one fully-formed decision chain."""

    def __init__(self, **ids: uuid.UUID | str) -> None:
        self.__dict__.update(ids)

    line: uuid.UUID
    exception: uuid.UUID
    proposal: uuid.UUID
    approval: uuid.UUID
    adjustment: uuid.UUID
    operation_id: str
    principal: str


async def _build_chain(connection: asyncpg.Connection, seed: str = "chain") -> _Chain:
    """Insert a valid batch, line, exception, proposal, approval and adjustment."""
    batch_id, line_id = uuid.uuid4(), uuid.uuid4()
    exception_id, proposal_id = uuid.uuid4(), uuid.uuid4()
    approval_id, adjustment_id = uuid.uuid4(), uuid.uuid4()

    await connection.execute(
        """
        INSERT INTO settlement_batch (id, content_hash, source, raw_payload, received_at, status)
        VALUES ($1, $2, 'test', $3, now(), 'received')
        """,
        batch_id,
        _hex(seed + "batch"),
        b"raw",
    )
    await connection.execute(
        """
        INSERT INTO settlement_line
            (id, settlement_batch_id, line_number, psp_reference, amount, currency,
             value_date, match_state)
        VALUES ($1, $2, 1, 'psp-1', 10.0000, 'EUR', current_date, 'unmatched')
        """,
        line_id,
        batch_id,
    )
    await connection.execute(
        """
        INSERT INTO exception
            (id, settlement_line_id, line_match_state, classification, status,
             rule_id, classifier_version, correlation_id)
        VALUES ($1, $2, 'unmatched', 'fee_split', 'open',
                'fees_deducted_from_a_capture', 'residual-r2', 'corr-1')
        """,
        exception_id,
        line_id,
    )
    await connection.execute(
        """
        INSERT INTO treatment_proposal
            (id, exception_id, treatment, confidence, rationale, abstained,
             model_id, model_version, prompt_hash, region_jurisdiction, proposed_at)
        VALUES ($1, $2, 'rebook', 'high', 'because', false,
                'model-x', 'v1', $3, 'eu-west', now())
        """,
        proposal_id,
        exception_id,
        _hex(seed + "prompt"),
    )
    await connection.execute(
        """
        INSERT INTO approval
            (id, exception_id, resolution_version, treatment_proposal_id, decision,
             approved_treatment, principal, approval_token, decided_at)
        VALUES ($1, $2, 1, $3, 'approved', 'rebook', 'controller-a', $4, now())
        """,
        approval_id,
        exception_id,
        proposal_id,
        # M5.1 made `approval_token` NOT NULL and unique. The row's own id is unique by
        # construction and recognisably not a token anybody issued.
        str(approval_id),
    )
    operation_id = _hex(seed + "op")
    await connection.execute(
        """
        INSERT INTO adjustment
            (id, approval_id, approved_treatment, approving_principal, amount, currency,
             account_code, period, operation_id, instruction_payload_hash)
        VALUES ($1, $2, 'rebook', 'controller-a', 12.3400, 'EUR', 'acc-1', '2026-08', $3, $4)
        """,
        adjustment_id,
        approval_id,
        operation_id,
        _hex(seed + "payload"),
    )
    return _Chain(
        line=line_id,
        exception=exception_id,
        proposal=proposal_id,
        approval=approval_id,
        adjustment=adjustment_id,
        operation_id=operation_id,
        principal="controller-a",
    )


# --------------------------------------------------------------------------------------
# Duplicate suppression — the guarantee the project exists to demonstrate
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_operation_id_is_rejected_by_the_database() -> None:
    """12.2. The unique constraint is the guarantee; application logic is not.

    Two adjustments carrying one operation identifier is precisely the double-post this
    project exists to prevent, so it must be impossible to write, not merely unlikely.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        first = await _build_chain(connection, "dup-a")
        second = await _build_chain(connection, "dup-b")
        shared = await connection.fetchval(
            "SELECT operation_id FROM adjustment WHERE id = $1", first.adjustment
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE adjustment SET operation_id = $1 WHERE id = $2",
                    shared,
                    second.adjustment,
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_operation_id_must_be_a_sha256_digest() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "opfmt")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE adjustment SET operation_id = 'not-a-digest' WHERE id = $1",
                    chain.adjustment,
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_one_exception_per_settlement_line() -> None:
    """FR-4: a re-run must not open a second decision path for one line."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "one-exc")
        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO exception
                        (id, settlement_line_id, line_match_state, classification, status,
                         rule_id, classifier_version, correlation_id)
                    VALUES ($1, $2, 'unmatched', 'fx_rounding', 'open',
                            'no_rule_matched', 'residual-r2', 'corr-2')
                    """,
                    uuid.uuid4(),
                    chain.line,
                )
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# UNKNOWN must stay first-class
# --------------------------------------------------------------------------------------


async def _insert_outbox(
    connection: asyncpg.Connection,
    adjustment_id: uuid.UUID,
    state: str,
    last_outcome: str | None,
) -> uuid.UUID:
    outbox_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO outbox (id, adjustment_id, state, last_outcome, attempt_count)
        VALUES ($1, $2, $3, $4, 1)
        """,
        outbox_id,
        adjustment_id,
        state,
        last_outcome,
    )
    return outbox_id


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_storable_while_still_pending() -> None:
    """13.5: ambiguity must be recordable. A schema that cannot hold it forces a lie."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "unknown-ok")
        outbox_id = await _insert_outbox(connection, chain.adjustment, "pending", "unknown")
        stored = await connection.fetchval(
            "SELECT last_outcome FROM outbox WHERE id = $1", outbox_id
        )
        assert stored == "unknown"
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "throttled", "partially_applied", None])
async def test_a_non_terminal_outcome_cannot_be_marked_settled(outcome: str | None) -> None:
    """The constraint that stops an ambiguous result being quietly filed as done."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, f"settle-{outcome}")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await _insert_outbox(connection, chain.adjustment, "settled", outcome)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "rejected"])
async def test_a_terminal_outcome_may_be_marked_settled(outcome: str) -> None:
    """The complement: the constraint must not block the legitimate case."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, f"settle-ok-{outcome}")
        await _insert_outbox(connection, chain.adjustment, "settled", outcome)
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# The write-ahead attempt record
# --------------------------------------------------------------------------------------


async def _insert_attempt(
    connection: asyncpg.Connection,
    chain: _Chain,
    state: str,
    outcome: str | None,
    resolved_offset: str | None = "0 seconds",
    attempt_no: int = 1,
) -> None:
    resolved = f"now() + interval '{resolved_offset}'" if resolved_offset else "NULL"
    await connection.execute(
        f"""
        INSERT INTO posting_attempt
            (id, adjustment_id, operation_id, attempt_no, sent_at, state, outcome, resolved_at)
        VALUES ($1, $2, $3, $4, now(), $5, $6, {resolved})
        """,
        uuid.uuid4(),
        chain.adjustment,
        chain.operation_id,
        attempt_no,
        state,
        outcome,
    )


@pytest.mark.asyncio
async def test_an_in_flight_attempt_records_a_send_with_no_outcome() -> None:
    """12.1.1: this row is the only evidence a send happened at all."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "wal-ok")
        await _insert_attempt(connection, chain, "in_flight", None, resolved_offset=None)
        row = await connection.fetchrow(
            "SELECT state, outcome, resolved_at, sent_at FROM posting_attempt WHERE"
            " adjustment_id = $1",
            chain.adjustment,
        )
        assert row is not None
        assert row["state"] == "in_flight"
        assert row["outcome"] is None
        assert row["resolved_at"] is None
        assert row["sent_at"] is not None, "a send time is what makes the record evidence"
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "state", "outcome", "resolved_offset"),
    [
        ("in_flight carrying an outcome", "in_flight", "confirmed", "0 seconds"),
        ("resolved with no outcome", "resolved", None, "0 seconds"),
        ("resolved with no resolution time", "resolved", "confirmed", None),
    ],
)
async def test_attempt_state_and_outcome_cannot_drift_apart(
    label: str, state: str, outcome: str | None, resolved_offset: str | None
) -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, f"wal-{state}-{outcome}-{resolved_offset}")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await _insert_attempt(connection, chain, state, outcome, resolved_offset)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_in_flight_attempt_cannot_carry_a_posting_reference() -> None:
    """A reference for a response that has not arrived would be fabricated evidence.

    The sibling of the settled-outbox rule: with the outcome still NULL, a check written as
    ``posting_ref IS NULL OR outcome IN (...)`` evaluates to NULL and therefore passes.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "wal-ref")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO posting_attempt
                        (id, adjustment_id, operation_id, attempt_no, sent_at, state,
                         outcome, resolved_at, posting_ref)
                    VALUES ($1, $2, $3, 1, now(), 'in_flight', NULL, NULL, 'LEDGER-123')
                    """,
                    uuid.uuid4(),
                    chain.adjustment,
                    chain.operation_id,
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_attempt_cannot_resolve_before_it_was_sent() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "wal-time")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await _insert_attempt(connection, chain, "resolved", "confirmed", "-1 hour")
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_two_records_for_one_attempt_number_are_rejected() -> None:
    """Duplicate attempt records would corrupt the evidence recovery reasons over."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "wal-dup")
        await _insert_attempt(connection, chain, "in_flight", None, resolved_offset=None)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await _insert_attempt(connection, chain, "in_flight", None, resolved_offset=None)
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# Manual recovery
# --------------------------------------------------------------------------------------


async def _insert_recovery(
    connection: asyncpg.Connection,
    adjustment_id: uuid.UUID,
    *,
    state: str = "open",
    resolution: str | None = None,
    resolved_by: str | None = None,
    approving_principal: str = "controller-a",
    sla: str = "4 hours",
) -> uuid.UUID:
    recovery_id = uuid.uuid4()
    # ``resolved`` is passed separately rather than reusing $3 in the CASE: PostgreSQL deduces
    # varchar from the column and text from the comparison, and asyncpg rejects the parameter
    # as ambiguous rather than picking one.
    await connection.execute(
        f"""
        INSERT INTO recovery_queue
            (id, adjustment_id, state, reason, evidence_procedure, opened_at, sla_due_at,
             approving_principal, resolution, resolved_by, resolved_at)
        VALUES ($1, $2, $3, 'ambiguous timeout', 'inspect the ledger export for the period',
                now(), now() + interval '{sla}', $4, $5, $6,
                CASE WHEN $7 THEN now() ELSE NULL END)
        """,
        recovery_id,
        adjustment_id,
        state,
        approving_principal,
        resolution,
        resolved_by,
        state == "resolved",
    )
    return recovery_id


@pytest.mark.asyncio
async def test_the_approver_cannot_resolve_their_own_unknown() -> None:
    """13.5: segregation of duties, enforced by the database rather than by discipline."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "sod")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await _insert_recovery(
                    connection,
                    chain.adjustment,
                    state="resolved",
                    resolution="confirmed_by_evidence",
                    resolved_by="controller-a",
                    approving_principal="controller-a",
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_a_different_principal_may_resolve_an_unknown() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "sod-ok")
        await _insert_recovery(
            connection,
            chain.adjustment,
            state="resolved",
            resolution="resolved_unverified",
            resolved_by="operator-b",
            approving_principal="controller-a",
        )
        stored = await connection.fetchval(
            "SELECT resolution FROM recovery_queue WHERE adjustment_id = $1", chain.adjustment
        )
        assert stored == "resolved_unverified", (
            "an unverified judgement must be recordable and visibly distinct from a verified one"
        )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_only_one_open_recovery_item_per_adjustment() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "rec-open")
        await _insert_recovery(connection, chain.adjustment)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await _insert_recovery(connection, chain.adjustment)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_adjustment_may_return_to_recovery_after_an_item_is_closed() -> None:
    """The complement: the index is partial precisely so this stays possible."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "rec-reopen")
        await _insert_recovery(
            connection,
            chain.adjustment,
            state="resolved",
            resolution="confirmed_by_evidence",
            resolved_by="operator-b",
        )
        await _insert_recovery(connection, chain.adjustment)
        open_items = await connection.fetchval(
            "SELECT count(*) FROM recovery_queue WHERE adjustment_id = $1 AND state = 'open'",
            chain.adjustment,
        )
        assert open_items == 1
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_a_recovery_item_must_carry_an_sla_in_the_future() -> None:
    """An item with no deadline cannot go stale, so it can sit unworked indefinitely."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "rec-sla")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await _insert_recovery(connection, chain.adjustment, sla="-1 hour")
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# Dead letters
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["amount", "total", "fee", "rate"])
async def test_a_dead_letter_envelope_cannot_smuggle_a_monetary_value(key: str) -> None:
    """JSONB would bypass every money constraint, so the keys are refused outright."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, f"dlq-{key}")
        outbox_id = await _insert_outbox(connection, chain.adjustment, "dead_lettered", "rejected")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO dlq (id, outbox_id, envelope, reason, attempts, replay_state)
                    VALUES ($1, $2, $3::jsonb, 'exhausted', 3, 'pending')
                    """,
                    uuid.uuid4(),
                    outbox_id,
                    f'{{"operation_id": "abc", "{key}": "12.34"}}',
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_a_dead_letter_envelope_accepts_dispatch_metadata() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "dlq-ok")
        outbox_id = await _insert_outbox(connection, chain.adjustment, "dead_lettered", "rejected")
        await connection.execute(
            """
            INSERT INTO dlq (id, outbox_id, envelope, reason, attempts, replay_state)
            VALUES ($1, $2, $3::jsonb, 'exhausted', 3, 'pending')
            """,
            uuid.uuid4(),
            outbox_id,
            '{"operation_id": "abc", "endpoint": "/postings", "adapter": "sim", "attempt": 3}',
        )
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# Model containment, enforced at the row level
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_abstaining_proposal_must_escalate() -> None:
    """A model that declined to decide must not appear to have recommended an action."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "abstain")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE treatment_proposal SET abstained = true WHERE id = $1",
                    chain.proposal,
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_approval_must_authorise_exactly_when_it_approves() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "appr")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE approval SET decision = 'rejected' WHERE id = $1", chain.approval
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1.23456", "0.00005", "-1.23456"])
async def test_adjustment_amount_is_rejected_not_rounded(value: str) -> None:
    """The money rules established in a72a14e6f51f extend to the posted amount."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, f"money-{value}")
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE adjustment SET amount = $1 WHERE id = $2",
                    decimal.Decimal(value),
                    chain.adjustment,
                )
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# Delete semantics
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_settlement_line_with_an_exception_cannot_be_deleted() -> None:
    """RESTRICT: tidying up upstream must not erase the record of a decision."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "restrict")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await connection.execute("DELETE FROM settlement_line WHERE id = $1", chain.line)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_evidence_is_removed_with_its_exception() -> None:
    """CASCADE, because evidence has no meaning apart from the exception it belongs to."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "cascade")
        await connection.execute(
            "INSERT INTO evidence (id, exception_id, kind, content) VALUES ($1, $2, $3, $4)",
            uuid.uuid4(),
            chain.exception,
            "merchant_memo",
            "partial refund issued",
        )
        # The proposal and approval hold RESTRICT references, so remove them first; the point
        # under test is the evidence cascade, not the order of an unrelated cleanup.
        await connection.execute("DELETE FROM adjustment WHERE id = $1", chain.adjustment)
        await connection.execute("DELETE FROM approval WHERE id = $1", chain.approval)
        await connection.execute("DELETE FROM treatment_proposal WHERE id = $1", chain.proposal)
        await connection.execute("DELETE FROM exception WHERE id = $1", chain.exception)

        remaining = await connection.fetchval(
            "SELECT count(*) FROM evidence WHERE exception_id = $1", chain.exception
        )
        assert remaining == 0
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_a_proposal_cannot_cite_evidence_that_does_not_exist() -> None:
    """The reason ``evidence_refs`` is a relation rather than a UUID array."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "cite")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO treatment_proposal_evidence"
                    " (treatment_proposal_id, evidence_id) VALUES ($1, $2)",
                    chain.proposal,
                    uuid.uuid4(),
                )
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# audit_event is append-only
#
# Two independent controls, tested separately because they fail for different reasons and
# protect against different things.
# --------------------------------------------------------------------------------------


async def _insert_audit_event(connection: asyncpg.Connection) -> uuid.UUID:
    event_id = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO audit_event
            (id, occurred_at, principal, tool, scope_granted, approval_decision,
             outcome, correlation_id)
        VALUES ($1, now(), 'controller-a', 'approve', 'exception:approve', 'approved',
                'success', 'corr-audit')
        """,
        event_id,
    )
    return event_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement", ["UPDATE audit_event SET outcome = 'failure'", "DELETE FROM audit_event"]
)
async def test_audit_event_rejects_mutation_even_from_the_table_owner(statement: str) -> None:
    """The trigger, not the grant.

    This is the control that matters: a migration, a maintenance script or a psql session runs
    as the owner, for whom a grant offers no protection whatsoever.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        event_id = await _insert_audit_event(connection)
        with pytest.raises(asyncpg.PostgresError) as raised:
            async with connection.transaction():
                await connection.execute(f"{statement} WHERE id = $1", event_id)
        assert "append-only" in str(raised.value)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_audit_event_rejects_truncate() -> None:
    """TRUNCATE bypasses row-level triggers, so it needs its own statement-level guard."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        with pytest.raises(asyncpg.PostgresError) as raised:
            async with connection.transaction():
                await connection.execute("TRUNCATE audit_event")
        assert "append-only" in str(raised.value)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement", ["UPDATE audit_event SET outcome = 'failure'", "DELETE FROM audit_event"]
)
async def test_the_application_role_cannot_update_or_delete_audit_event(statement: str) -> None:
    """The insert-only grant required by IMPLEMENTATION_PLAN 1.2, tested as the app role.

    ``SET ROLE`` rather than a second connection, so no credential for the role exists or is
    needed anywhere in the test suite.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        # Without this the test passes for the wrong reason. A database where the role holds
        # no privileges at all also denies UPDATE and DELETE, so the assertion below would
        # succeed while proving nothing about the insert-only grant. It did exactly that on
        # the first run, because an earlier test drops and recreates the tables.
        assert await connection.fetchval(
            "SELECT has_table_privilege($1, 'audit_event', 'INSERT')", APP_ROLE
        ), "the role holds no INSERT grant, so a denial here would prove nothing"

        await connection.execute(f"SET LOCAL ROLE {APP_ROLE}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.transaction():
                await connection.execute(f"{statement} WHERE id = $1", uuid.uuid4())
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_the_application_role_can_still_append_an_audit_event() -> None:
    """The complement: a control that blocked appends would break the audit trail itself."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(f"SET LOCAL ROLE {APP_ROLE}")
        event_id = await _insert_audit_event(connection)
        assert await connection.fetchval("SELECT count(*) FROM audit_event WHERE id = $1", event_id)
    finally:
        await transaction.rollback()
        await connection.close()


# --------------------------------------------------------------------------------------
# Authorisation and segregation of duties, proven against PostgreSQL
#
# Added after an adversarial review. Both controls previously rested on a value the
# application supplied, and both were expressible as composite foreign keys instead.
# --------------------------------------------------------------------------------------


async def _approval_only(
    connection: asyncpg.Connection,
    seed: str,
    *,
    decision: str,
    approved_treatment: str | None,
    principal: str = "controller-a",
) -> uuid.UUID:
    """Build a chain up to an approval with a chosen decision, and stop there."""
    batch_id, line_id = uuid.uuid4(), uuid.uuid4()
    exception_id, approval_id = uuid.uuid4(), uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO settlement_batch (id, content_hash, source, raw_payload, received_at, status)
        VALUES ($1, $2, 'test', $3, now(), 'received')
        """,
        batch_id,
        _hex(seed + "batch"),
        b"raw",
    )
    await connection.execute(
        """
        INSERT INTO settlement_line
            (id, settlement_batch_id, line_number, psp_reference, amount, currency,
             value_date, match_state)
        VALUES ($1, $2, 1, 'psp-1', 10.0000, 'EUR', current_date, 'unmatched')
        """,
        line_id,
        batch_id,
    )
    await connection.execute(
        """
        INSERT INTO exception
            (id, settlement_line_id, line_match_state, classification, status,
             rule_id, classifier_version, correlation_id)
        VALUES ($1, $2, 'unmatched', 'fee_split', 'open',
                'fees_deducted_from_a_capture', 'residual-r2', 'corr-1')
        """,
        exception_id,
        line_id,
    )
    await connection.execute(
        """
        INSERT INTO approval
            (id, exception_id, resolution_version, decision, approved_treatment,
             principal, approval_token, decided_at)
        VALUES ($1, $2, 1, $3, $4, $5, $6, now())
        """,
        approval_id,
        exception_id,
        decision,
        approved_treatment,
        principal,
        str(approval_id),
    )
    return approval_id


async def _insert_adjustment(
    connection: asyncpg.Connection,
    approval_id: uuid.UUID,
    seed: str,
    *,
    approved_treatment: str,
    approving_principal: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO adjustment
            (id, approval_id, approved_treatment, approving_principal, amount, currency,
             account_code, period, operation_id, instruction_payload_hash)
        VALUES ($1, $2, $3, $4, 1250.0000, 'USD', '4100', '2026-07', $5, $6)
        """,
        uuid.uuid4(),
        approval_id,
        approved_treatment,
        approving_principal,
        _hex(seed + "op"),
        _hex(seed + "payload"),
    )


@pytest.mark.asyncio
async def test_a_rejection_cannot_authorise_an_adjustment() -> None:
    """FR-7. The defect this closes: a plain FK proves an approval exists, not that it agreed.

    A rejected approval carries ``approved_treatment IS NULL``. The referencing column is
    NOT NULL, so there is no value it could hold that would match — the row is unreachable
    rather than merely discouraged.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        approval_id = await _approval_only(
            connection, "reject", decision="rejected", approved_treatment=None
        )
        for attempted in ("rebook", "accrue", "write_off"):
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await _insert_adjustment(
                        connection,
                        approval_id,
                        f"reject-{attempted}",
                        approved_treatment=attempted,
                        approving_principal="controller-a",
                    )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_adjustment_cannot_claim_a_treatment_the_approval_did_not_authorise() -> None:
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        approval_id = await _approval_only(
            connection, "mismatch", decision="approved", approved_treatment="rebook"
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await _insert_adjustment(
                    connection,
                    approval_id,
                    "mismatch",
                    approved_treatment="write_off",
                    approving_principal="controller-a",
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_adjustment_cannot_name_someone_who_did_not_approve_it() -> None:
    """The value the segregation-of-duties check compares against cannot be invented."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        approval_id = await _approval_only(
            connection,
            "principal",
            decision="approved",
            approved_treatment="rebook",
            principal="controller-a",
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await _insert_adjustment(
                    connection,
                    approval_id,
                    "principal",
                    approved_treatment="rebook",
                    approving_principal="system",
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_escalated_approval_cannot_produce_an_adjustment() -> None:
    """§6.2: escalation is what happens when no amount can be computed deterministically."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        approval_id = await _approval_only(
            connection, "escalate", decision="approved", approved_treatment="escalate"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await _insert_adjustment(
                    connection,
                    approval_id,
                    "escalate",
                    approved_treatment="escalate",
                    approving_principal="controller-a",
                )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_approved_adjustment_is_still_permitted() -> None:
    """The complement. A rule that rejected everything would pass every test above."""
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        approval_id = await _approval_only(
            connection, "allow", decision="approved", approved_treatment="accrue"
        )
        await _insert_adjustment(
            connection,
            approval_id,
            "allow",
            approved_treatment="accrue",
            approving_principal="controller-a",
        )
        stored = await connection.fetchval(
            "SELECT approved_treatment FROM adjustment WHERE approval_id = $1", approval_id
        )
        assert stored == "accrue"
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_a_recovery_item_cannot_invent_its_approving_principal() -> None:
    """§13.5 clause 5. Closes the other half of the segregation-of-duties control.

    Before the composite key, a second code path could write any principal here and the
    check constraint would happily compare the resolver against a value nobody approved.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "rec-principal")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await _insert_recovery(connection, chain.adjustment, approving_principal="system")
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_an_attempt_cannot_record_a_foreign_operation_id() -> None:
    """§12.1.1. Recovery decides whether an irreversible write may be repeated from this row.

    An attempt naming a different operation than its adjustment would be well-formed and
    wrong — the worst combination for evidence.
    """
    connection = await _connect()
    transaction = connection.transaction()
    await transaction.start()
    try:
        chain = await _build_chain(connection, "wal-foreign")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO posting_attempt
                        (id, adjustment_id, operation_id, attempt_no, sent_at, state)
                    VALUES ($1, $2, $3, 1, now(), 'in_flight')
                    """,
                    uuid.uuid4(),
                    chain.adjustment,
                    _hex("some-other-operation"),
                )
    finally:
        await transaction.rollback()
        await connection.close()
