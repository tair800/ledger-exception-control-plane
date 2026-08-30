"""Classification against real PostgreSQL — persistence, integrity and deliberate races.

The decision is proven in ``test_classification.py`` and graded in
``test_classification_precision.py``. What needs a database is everything about *state*: that only
a residual can become an exception, that the invariant holds against direct SQL as well as against
this code, that a second run creates nothing, and — the part no single-threaded test can establish
— that two workers cannot raise two exceptions for one residual, and that a worker racing the
matcher cannot leave the two contradicting each other.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_classification_postgres.py -m integration
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import json
import os
import pathlib
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.classification import (
    CLASSIFIER_VERSION,
    correlation_id_for,
    run_classification,
)
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn, create_engine
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ingest import ingest
from ledger_exception_control_plane.matching import run_matching

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "fixtures" / "canonical"

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

MATCHED_AT = dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC)
RECEIVED_AT = dt.datetime(2026, 6, 15, 8, 30, tzinfo=dt.UTC)
JUNE = dt.date(2026, 6, 10)
JULY = dt.date(2026, 7, 4)
ORDER = "ORD-2026-000042"


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
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
    """A per-test engine holding no idle connections.

    ``NullPool`` for the reason recorded in ``test_matching_postgres``: this fixture is
    function-scoped, and a pooled engine leaves idle sockets behind per test until asyncpg's
    connection timeout starts firing somewhere unrelated.
    """
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_slate() -> AsyncIterator[None]:
    await _wipe()
    yield


async def _wipe() -> None:
    """Delete in dependency order. ``exception`` first: it holds the settlement line under
    RESTRICT, so a leftover control record blocks the line's own deletion — which is the foreign
    key doing exactly what it exists to do."""
    connection = await asyncpg.connect(DSN)
    try:
        for table in ("exception", "match_result", "settlement_line", "settlement_batch"):
            await connection.execute(f"DELETE FROM {table}")
        await connection.execute("DELETE FROM ledger_entry")
    finally:
        await connection.close()


async def _seed_batch(
    lines: list[tuple[str, str, dt.date, str | None]],
    *,
    marker: str,
    status: str = "parsed",
) -> tuple[uuid.UUID, list[uuid.UUID], str]:
    """Insert one settlement batch and its lines directly.

    Direct SQL rather than the ingestion path: these tests are about the exception boundary, and
    building each case as a CSV would make the input harder to read than the assertion. The corpus
    end-to-end test below does go through ingestion.
    """
    batch_id = uuid.uuid4()
    content_hash = uuid.uuid4().hex + uuid.uuid4().hex
    line_ids: list[uuid.UUID] = []
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status, quarantine_reason)"
            " VALUES ($1, $2, 'test', $3, $4, $5, $6)",
            batch_id,
            content_hash,
            b"raw",
            RECEIVED_AT,
            status,
            "line 1: amount invalid" if status == "quarantined" else None,
        )
        for number, (amount, currency, day, reference) in enumerate(lines, start=1):
            line_id = uuid.uuid4()
            await connection.execute(
                "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
                " merchant_reference, amount, currency, value_date, match_state)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'unmatched')",
                line_id,
                batch_id,
                number,
                f"psp_{marker}_{number}",
                reference,
                decimal.Decimal(amount),
                currency,
                day,
            )
            line_ids.append(line_id)
    finally:
        await connection.close()
    return batch_id, line_ids, content_hash


async def _mark_matched(line_id: uuid.UUID, *, amount: str, day: dt.date = JUNE) -> None:
    """Give a line a real ledger entry and a real ``match_result``, as M2.2 would."""
    connection = await asyncpg.connect(DSN)
    try:
        entry_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES ($1, $2, '4100', $3, 'EUR', $4)",
            entry_id,
            f"GL-{entry_id.hex[:8]}",
            decimal.Decimal(amount),
            dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.UTC),
        )
        await connection.execute(
            "INSERT INTO match_result (id, settlement_line_id, ledger_entry_id, rule_id,"
            " matched_at) VALUES ($1, $2, $3, 'exact_amount', $4)",
            uuid.uuid4(),
            line_id,
            entry_id,
            MATCHED_AT,
        )
        await connection.execute(
            "UPDATE settlement_line SET match_state = 'matched' WHERE id = $1", line_id
        )
    finally:
        await connection.close()


async def _exceptions() -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        return list(
            await connection.fetch(
                "SELECT id, settlement_line_id, line_match_state, classification, status, rule_id,"
                " classifier_version, correlation_id, created_at FROM exception"
                " ORDER BY classification, correlation_id"
            )
        )
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# Eligibility — what may and may not become an exception
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_residual_line_becomes_exactly_one_exception(engine: AsyncEngine) -> None:
    _, line_ids, content_hash = await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="basic")

    run = await run_classification(engine)

    assert run.residuals == 1
    assert run.created == 1
    assert run.lost_races == 0
    rows = await _exceptions()
    assert len(rows) == 1
    assert rows[0]["settlement_line_id"] == line_ids[0]
    assert rows[0]["correlation_id"] == correlation_id_for(content_hash, 1)


@pytest.mark.asyncio
async def test_a_matched_line_never_becomes_an_exception(engine: AsyncEngine) -> None:
    """The central eligibility rule. A reconciled line has no residual to explain."""
    _, line_ids, _ = await _seed_batch(
        [("500.00", "EUR", JUNE, ORDER), ("77.00", "EUR", JUNE, None)], marker="matched"
    )
    await _mark_matched(line_ids[0], amount="500.00")

    run = await run_classification(engine)

    assert run.residuals == 1
    rows = await _exceptions()
    assert [row["settlement_line_id"] for row in rows] == [line_ids[1]]


@pytest.mark.asyncio
async def test_a_quarantined_batch_generates_no_exception(engine: AsyncEngine) -> None:
    """Quarantine condemns the whole file (ADR-040), so a quarantined batch has no lines to
    classify. The batch-status filter is asserted anyway rather than relying on that: the day
    someone persists a partial parse, this test fails instead of a reconciliation exception being
    raised against a row nobody vouched for."""
    _, line_ids, _ = await _seed_batch(
        [("120.00", "EUR", JUNE, ORDER)], marker="quarantined", status="quarantined"
    )

    run = await run_classification(engine)

    assert run.residuals == 0
    assert await _exceptions() == []
    assert line_ids  # the rows exist; they are simply not eligible


@pytest.mark.asyncio
async def test_a_batch_still_at_received_generates_no_exception(engine: AsyncEngine) -> None:
    """A crash between ingestion's two transactions leaves a batch at ``received``. Nothing has
    vouched for its contents, so nothing there is residual — it is unfinished."""
    await _seed_batch([("120.00", "EUR", JUNE, ORDER)], marker="received", status="received")

    assert (await run_classification(engine)).residuals == 0
    assert await _exceptions() == []


@pytest.mark.asyncio
async def test_a_line_already_carrying_an_exception_is_not_reclassified(
    engine: AsyncEngine,
) -> None:
    """Idempotence at the eligibility level: the second run has no work, not duplicate work."""
    await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="idem")

    first = await run_classification(engine)
    second = await run_classification(engine)

    assert (first.residuals, first.created) == (1, 1)
    assert (second.residuals, second.created) == (0, 0)
    assert len(await _exceptions()) == 1


@pytest.mark.asyncio
async def test_repeated_runs_are_stable_in_class_rule_and_correlation_id(
    engine: AsyncEngine,
) -> None:
    """Nothing about the persisted row moves between runs — not the class, not the rule, not the
    correlation id. A clock or a counter anywhere in the path would show up here."""
    await _seed_batch(
        [("1244.71", "EUR", JUNE, ORDER), ("-7.94", "EUR", JUNE, ORDER)], marker="stable"
    )

    await run_classification(engine)
    before = [dict(row) for row in await _exceptions()]
    for _ in range(3):
        await run_classification(engine)
    assert [dict(row) for row in await _exceptions()] == before


# --------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_exception_records_the_rule_the_ruleset_and_its_lifecycle_state(
    engine: AsyncEngine,
) -> None:
    """A persisted exception must answer which line, which class, by which rule, under which
    ruleset, when, and in what state — without joining to anything that could have changed since."""
    _, _, content_hash = await _seed_batch(
        [
            ("1244.71", "EUR", JUNE, ORDER),
            ("-2.13", "EUR", JUNE, ORDER),
            ("50.21", "EUR", JUNE, None),
        ],
        marker="prov",
    )

    await run_classification(engine)
    rows = await _exceptions()

    assert len(rows) == 3
    for row in rows:
        assert row["classifier_version"] == CLASSIFIER_VERSION
        assert row["status"] == "open"
        assert row["line_match_state"] == "unmatched"
        assert row["created_at"] is not None
        assert row["correlation_id"].startswith(f"lecp:{content_hash}:")

    by_class = {row["classification"]: row["rule_id"] for row in rows}
    assert by_class == {
        "fee_split": "deductions_split_across_rows",
        "unclassified": "no_rule_matched",
    }


# --------------------------------------------------------------------------------------
# The database refuses contradictory state, whoever writes it
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_sql_cannot_raise_an_exception_for_a_matched_line() -> None:
    """The invariant against a writer that is not this application.

    A check constraint cannot reference another table, so this is a composite foreign key onto
    ``(settlement_line.id, 'unmatched')``. Bypassing the service does not bypass it.
    """
    _, line_ids, _ = await _seed_batch([("500.00", "EUR", JUNE, ORDER)], marker="direct1")
    await _mark_matched(line_ids[0], amount="500.00")

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
                " status, rule_id, classifier_version, correlation_id)"
                " VALUES ($1, $2, 'unmatched', 'unclassified', 'open', 'no_rule_matched',"
                " 'residual-r1', 'lecp:x:000001')",
                uuid.uuid4(),
                line_ids[0],
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_direct_sql_cannot_match_a_line_that_already_has_an_exception(
    engine: AsyncEngine,
) -> None:
    """The same invariant read from the other end, and the half that is easy to forget.

    Blocking only the insert would leave the contradiction reachable by marking the line matched
    afterwards. The foreign key refuses that too, because the row it references would cease to
    exist.
    """
    _, line_ids, _ = await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="direct2")
    await run_classification(engine)
    assert len(await _exceptions()) == 1

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                "UPDATE settlement_line SET match_state = 'matched' WHERE id = $1", line_ids[0]
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_direct_sql_cannot_write_two_exceptions_for_one_residual() -> None:
    """FR-4's one-per-residual rule is a unique constraint, not an application convention."""
    _, line_ids, _ = await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="direct3")

    connection = await asyncpg.connect(DSN)
    try:
        for attempt in range(2):
            statement = connection.execute(
                "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
                " status, rule_id, classifier_version, correlation_id)"
                " VALUES ($1, $2, 'unmatched', 'unclassified', 'open', 'no_rule_matched',"
                " 'residual-r1', $3)",
                uuid.uuid4(),
                line_ids[0],
                f"lecp:x:00000{attempt}",
            )
            if attempt == 0:
                await statement
            else:
                with pytest.raises(asyncpg.UniqueViolationError):
                    await statement
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value", "error"),
    [
        ("classification", "partial_refund", asyncpg.CheckViolationError),
        ("classification", "", asyncpg.CheckViolationError),
        ("status", "in_progress", asyncpg.CheckViolationError),
        ("rule_id", "No Rule Matched", asyncpg.CheckViolationError),
        ("rule_id", "", asyncpg.CheckViolationError),
        ("classifier_version", "Residual R1", asyncpg.CheckViolationError),
        ("line_match_state", "matched", asyncpg.CheckViolationError),
    ],
)
async def test_direct_sql_cannot_write_a_value_outside_the_declared_vocabulary(
    column: str, value: str, error: type[Exception]
) -> None:
    """Every closed field is closed in the database.

    ``classification`` and ``status`` are the taxonomy itself; ``rule_id`` and
    ``classifier_version`` are constrained by *shape* rather than by value, because the rule set
    evolves and enumerating rule ids here would demand a migration for every new rule. Free-form
    prose still cannot get in. ``line_match_state`` is pinned to one value, which is what turns the
    composite foreign key into the invariant it enforces.
    """
    _, line_ids, _ = await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker=f"vocab{column}")
    values = {
        "settlement_line_id": line_ids[0],
        "line_match_state": "unmatched",
        "classification": "unclassified",
        "status": "open",
        "rule_id": "no_rule_matched",
        "classifier_version": "residual-r1",
        "correlation_id": "lecp:x:000001",
    }
    values[column] = value

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(error):
            await connection.execute(
                "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
                " status, rule_id, classifier_version, correlation_id)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                uuid.uuid4(),
                values["settlement_line_id"],
                values["line_match_state"],
                values["classification"],
                values["status"],
                values["rule_id"],
                values["classifier_version"],
                values["correlation_id"],
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_an_exception_without_provenance_is_refused() -> None:
    """``rule_id`` and ``classifier_version`` are NOT NULL. An exception that cannot say how it was
    reached is not a control record."""
    _, line_ids, _ = await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="noprov")

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.NotNullViolationError):
            await connection.execute(
                "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
                " status, correlation_id)"
                " VALUES ($1, $2, 'unmatched', 'unclassified', 'open', 'lecp:x:000001')",
                uuid.uuid4(),
                line_ids[0],
            )
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# Concurrency — deliberately interleaved, not merely gathered
# --------------------------------------------------------------------------------------


async def _wait_until_a_backend_is_blocked_on_a_lock(timeout: float = 30.0) -> None:
    """Block until PostgreSQL reports a session waiting on a lock in this database.

    The handshake the two forced-race tests below are built on, and it replaces a sleep that looked
    like one and was not. Both tests originally started the classifier, slept 250 ms and asserted
    the task was unfinished — which it was, but because it was still opening its connection, not
    because it had reached the lock. The classifier then did its *read* after the other transaction
    committed, saw a world already resolved, and reported no residual work at all: the race under
    test never happened, and the assertion that caught it was about a count rather than about
    timing, which is the only reason the illusion did not survive.

    Asking the database is the real signal. A backend waiting on a ``Lock`` wait event has finished
    everything before it and is genuinely parked behind another transaction, so the interleaving is
    established rather than assumed — and the test no longer depends on how long a connection takes
    to open on the machine it runs on.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    watcher = await asyncpg.connect(DSN)
    try:
        while loop.time() < deadline:
            waiting = await watcher.fetchval(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND state = 'active'"
                " AND wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.05)
    finally:
        await watcher.close()
    raise AssertionError("no backend ever blocked on a lock; the race under test did not happen")


@pytest.mark.asyncio
async def test_two_workers_classifying_one_residual_create_one_exception(
    engine: AsyncEngine,
) -> None:
    """Both read the same residual and reach the same class. Only one row may exist."""
    await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="race1")
    first, second = create_engine(_settings()), create_engine(_settings())
    try:
        outcomes = await asyncio.gather(
            run_classification(first), run_classification(second), return_exceptions=True
        )
    finally:
        await first.dispose()
        await second.dispose()

    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), (
            f"concurrent classification raised: {outcome!r}"
        )

    assert len(await _exceptions()) == 1
    runs = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert sum(run.created for run in runs) == 1


@pytest.mark.asyncio
async def test_the_loser_of_a_forced_classification_race_writes_nothing_and_knows_it(
    engine: AsyncEngine,
) -> None:
    """A real interleaving, not two calls that may well have run one after the other.

    A raw connection inserts the exception and **holds the transaction open**. The classifier is
    then started: its ``SELECT … FOR UPDATE`` blocks behind the foreign-key lock the open insert
    holds on the settlement line, which is how the overlap is guaranteed rather than hoped for.
    Releasing the first transaction lets the classifier proceed, and it must find the work already
    done — one exception, no error, and a run that reports the loss instead of claiming the win.
    """
    _, line_ids, _ = await _seed_batch([("2799.97", "EUR", JUNE, ORDER)], marker="race2")

    holder = await asyncpg.connect(DSN)
    try:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute(
            "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
            " status, rule_id, classifier_version, correlation_id)"
            " VALUES ($1, $2, 'unmatched', 'unclassified', 'open', 'no_rule_matched',"
            " 'residual-r1', 'lecp:held:000001')",
            uuid.uuid4(),
            line_ids[0],
        )

        task = asyncio.create_task(run_classification(engine))
        await _wait_until_a_backend_is_blocked_on_a_lock()
        assert not task.done(), "the classifier must be blocked on the row lock, not racing past it"

        await transaction.commit()
        run = await asyncio.wait_for(task, timeout=30)
    finally:
        await holder.close()

    assert run.residuals == 1, "the classifier must have read the line as residual before blocking"
    assert run.created == 0
    assert run.lost_races == 1, "the loser must know it lost, not think it won"
    rows = await _exceptions()
    assert len(rows) == 1
    assert rows[0]["correlation_id"] == "lecp:held:000001", "the first writer's row survives"


@pytest.mark.asyncio
async def test_a_line_matched_mid_run_is_dropped_rather_than_violating_the_invariant(
    engine: AsyncEngine,
) -> None:
    """The race between this module and M2.2, forced to actually happen.

    The classifier has already read the line as residual. A raw connection then takes the same row
    lock M2.2's persistence takes, marks the line matched and commits while the classifier is
    blocked behind it. When the classifier proceeds it re-reads under the lock, finds the line is
    no longer unmatched, and writes nothing — so the composite foreign key never has to reject
    anything, which is the difference between a graceful loss and an aborted run.
    """
    _, line_ids, _ = await _seed_batch([("500.00", "EUR", JUNE, ORDER)], marker="race3")

    holder = await asyncpg.connect(DSN)
    try:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute(
            "SELECT id FROM settlement_line WHERE id = $1 ORDER BY id FOR UPDATE", line_ids[0]
        )

        task = asyncio.create_task(run_classification(engine))
        await _wait_until_a_backend_is_blocked_on_a_lock()
        assert not task.done(), "the classifier must be blocked on the row lock"

        entry_id = uuid.uuid4()
        await holder.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES ($1, $2, '4100', 500.00, 'EUR', $3)",
            entry_id,
            f"GL-{entry_id.hex[:8]}",
            dt.datetime.combine(JUNE, dt.time(12, 0), tzinfo=dt.UTC),
        )
        await holder.execute(
            "INSERT INTO match_result (id, settlement_line_id, ledger_entry_id, rule_id,"
            " matched_at) VALUES ($1, $2, $3, 'exact_amount', $4)",
            uuid.uuid4(),
            line_ids[0],
            entry_id,
            MATCHED_AT,
        )
        await holder.execute(
            "UPDATE settlement_line SET match_state = 'matched' WHERE id = $1", line_ids[0]
        )
        await transaction.commit()

        run = await asyncio.wait_for(task, timeout=30)
    finally:
        await holder.close()

    assert run.residuals == 1, "the classifier must have read the line as residual before blocking"
    assert run.created == 0
    assert run.lost_races == 1
    assert await _exceptions() == [], "a matched line must not acquire a control record"


@pytest.mark.asyncio
async def test_a_group_whose_evidence_is_matched_mid_run_is_not_persisted_from_the_stale_read(
    engine: AsyncEngine,
) -> None:
    """The second defect adversarial review found, and the reason the lock covers the evidence.

    A fee split is decided from the *other* rows on the order, so locking only the line being
    classified is not enough. Here three unreconciled rows read as a split; the gross capture is
    then matched by a concurrent worker before the write lands. The two fee rows are still unmatched
    and still eligible, so the composite foreign key sees nothing wrong — and the earlier version
    persisted them as ``fee_split``, a split whose capture had gone.

    Locking the evidence and re-reading it under that lock changes the answer rather than the
    eligibility: the fees are still classified, but as ``unclassified``, because a group with no
    unreconciled inflow is not a deduction from anything.
    """
    _, line_ids, _ = await _seed_batch(
        [
            ("1244.71", "EUR", JUNE, ORDER),
            ("-2.13", "EUR", JUNE, ORDER),
            ("-7.94", "EUR", JUNE, ORDER),
        ],
        marker="ctxrace",
    )
    gross = line_ids[0]

    holder = await asyncpg.connect(DSN)
    try:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute("SELECT id FROM settlement_line WHERE id = $1 FOR UPDATE", gross)

        task = asyncio.create_task(run_classification(engine))
        await _wait_until_a_backend_is_blocked_on_a_lock()
        assert not task.done(), "the classifier must be blocked on the evidence lock"

        entry_id = uuid.uuid4()
        await holder.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES ($1, $2, '4100', 1244.71, 'EUR', $3)",
            entry_id,
            f"GL-{entry_id.hex[:8]}",
            dt.datetime.combine(JUNE, dt.time(12, 0), tzinfo=dt.UTC),
        )
        await holder.execute(
            "INSERT INTO match_result (id, settlement_line_id, ledger_entry_id, rule_id,"
            " matched_at) VALUES ($1, $2, $3, 'exact_amount', $4)",
            uuid.uuid4(),
            gross,
            entry_id,
            MATCHED_AT,
        )
        await holder.execute(
            "UPDATE settlement_line SET match_state = 'matched' WHERE id = $1", gross
        )
        await transaction.commit()

        run = await asyncio.wait_for(task, timeout=30)
    finally:
        await holder.close()

    assert run.residuals == 3, "all three were residual when the run began"
    assert run.created == 2, "the matched gross is no longer eligible"
    assert run.lost_races == 1
    assert run.by_classification == {"unclassified": 2}, (
        "a split whose capture has been reconciled is no longer a split"
    )
    rows = await _exceptions()
    assert {row["settlement_line_id"] for row in rows} == {line_ids[1], line_ids[2]}
    assert {row["classification"] for row in rows} == {"unclassified"}


@pytest.mark.asyncio
async def test_matching_leaves_alone_a_line_that_is_already_under_exception_control(
    engine: AsyncEngine,
) -> None:
    """The reverse direction, through the real matcher rather than through SQL.

    A residual that has become an exception has a decision path of its own. Matching it after a
    later ledger snapshot would silently revoke a claim the system had already made — so M2.2
    excludes it, and the run completes normally rather than colliding with the foreign key.
    """
    _, line_ids, _ = await _seed_batch([("500.00", "EUR", JUNE, ORDER)], marker="control")
    await run_classification(engine)
    assert len(await _exceptions()) == 1

    connection = await asyncpg.connect(DSN)
    try:
        entry_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES ($1, $2, '4100', 500.00, 'EUR', $3)",
            entry_id,
            f"GL-{entry_id.hex[:8]}",
            dt.datetime.combine(JUNE, dt.time(12, 0), tzinfo=dt.UTC),
        )
    finally:
        await connection.close()

    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert run.considered == 0, "a line under exception control is not matching's to take"
    assert run.matched == 0
    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM match_result") == 0
        assert (
            await connection.fetchval(
                "SELECT match_state FROM settlement_line WHERE id = $1", line_ids[0]
            )
            == "unmatched"
        )
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# The corpus, end to end
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_canonical_corpus_classifies_correctly_through_the_whole_pipeline(
    engine: AsyncEngine,
) -> None:
    """Ingestion, matching and classification against real PostgreSQL, graded against intent.

    The unit measurement grades the pure classifier; this grades ``exception`` after the whole path
    has run. Joined on ``psp_reference`` rather than on the corpus's row ids, because these lines
    reach the database through ingestion, which mints its own identifiers.

    Construction metadata is read here, by the test, to judge production output. The classifier was
    handed none of it.
    """
    records = json.loads((CORPUS / "records.json").read_text(encoding="utf-8"))
    scenario_by_reference: dict[str, str] = {}
    for batch in records["batches"]:
        for row in batch["lines"]:
            existing = scenario_by_reference.setdefault(row["psp_reference"], row["scenario_id"])
            assert existing == row["scenario_id"]

    connection = await asyncpg.connect(DSN)
    try:
        for row in records["ledger_entries"]:
            await connection.execute(
                "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency,"
                " booked_at) VALUES ($1, $2, $3, $4, $5, $6)",
                uuid.UUID(row["id"]),
                row["external_ref"],
                row["account_code"],
                decimal.Decimal(row["amount"]),
                row["currency"],
                dt.datetime.fromisoformat(row["booked_at"]),
            )
    finally:
        await connection.close()

    for name in ("psp-settlement-2026-06.csv", "psp-settlement-2026-07.csv"):
        outcome = await ingest(
            engine,
            (CORPUS / "settlement" / name).read_bytes(),
            source="file-drop",
            received_at=RECEIVED_AT,
        )
        assert outcome.accepted, outcome.quarantine_reason

    assert (await run_matching(engine, matched_at=MATCHED_AT)).matched == 4
    run = await run_classification(engine)

    assert run.residuals == 13
    assert run.created == 13
    assert run.by_classification == {
        "fee_split": 3,
        "chargeback_reversal": 1,
        "cross_period_refund": 1,
        "unclassified": 8,
    }

    connection = await asyncpg.connect(DSN)
    try:
        rows = await connection.fetch(
            "SELECT l.psp_reference, e.classification, e.rule_id FROM exception e"
            " JOIN settlement_line l ON l.id = e.settlement_line_id"
        )
    finally:
        await connection.close()

    expected = {
        "SC-005-fee-split": "fee_split",
        "SC-006-chargeback-reversal": "chargeback_reversal",
        "SC-008-cross-period-refund": "cross_period_refund",
    }
    for row in rows:
        scenario = scenario_by_reference[row["psp_reference"]]
        prefix = scenario if scenario in expected else None
        if prefix is not None:
            assert row["classification"] == expected[prefix], (
                f"{scenario} was classified {row['classification']}"
            )
        else:
            assert row["classification"] == "unclassified", (
                f"{scenario} was given {row['classification']} on evidence that cannot support it"
            )

    assert {row["classification"] for row in rows} <= {
        "fee_split",
        "chargeback_reversal",
        "cross_period_refund",
        "unclassified",
    }


@pytest.mark.asyncio
async def test_classification_writes_nothing_into_the_money_path(engine: AsyncEngine) -> None:
    """M2.3 creates no adjustment, no approval and no treatment proposal.

    Asserted against the database rather than only against the source, because the scope boundary
    that matters is what ends up persisted: an adjustment row written here would carry an amount
    nobody computed and an approval nobody gave.
    """
    await _seed_batch(
        [("1244.71", "EUR", JUNE, ORDER), ("-7.94", "EUR", JUNE, ORDER)], marker="scope"
    )
    await run_classification(engine)
    assert len(await _exceptions()) == 2

    connection = await asyncpg.connect(DSN)
    try:
        for table in ("adjustment", "approval", "treatment_proposal", "evidence", "outbox"):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 0, table
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_classification_of_one_world_does_not_depend_on_insertion_order(
    engine: AsyncEngine,
) -> None:
    """The same three movements, inserted in two different orders, classify identically.

    Row order in PostgreSQL is not a guarantee, and a classifier that read a group in physical
    order could reach a different answer for the same world. The class must come from the evidence.
    """
    rows: list[tuple[str, str, dt.date, str | None]] = [
        ("1244.71", "EUR", JUNE, ORDER),
        ("-2.13", "EUR", JUNE, ORDER),
        ("-7.94", "EUR", JUNE, ORDER),
    ]

    await _seed_batch(rows, marker="orderA")
    await run_classification(engine)
    forwards = sorted((row["classification"], row["rule_id"]) for row in await _exceptions())

    await _wipe()
    await _seed_batch(list(reversed(rows)), marker="orderB")
    await run_classification(engine)
    backwards = sorted((row["classification"], row["rule_id"]) for row in await _exceptions())

    assert forwards == backwards
    assert forwards == [("fee_split", "deductions_split_across_rows")] * 3
