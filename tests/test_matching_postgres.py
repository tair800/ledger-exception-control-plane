"""Matching against real PostgreSQL — persistence, races and the integrity guards.

The decision is proven in ``test_matching.py``. What needs a database is everything about *state*:
that a match and the line status it vouches for land together, that a consumed entry is really
unavailable, that a second run does nothing, and — the part no single-threaded test can establish —
that two workers cannot double-consume.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_matching_postgres.py -m integration
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
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn, create_engine
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ingest import ingest
from ledger_exception_control_plane.matching import DEFAULT_POLICY, MatchRule, run_matching
from ledger_exception_control_plane.matching.policy import TolerancePolicy

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "fixtures" / "canonical"

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

MATCHED_AT = dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC)
RECEIVED_AT = dt.datetime(2026, 6, 15, 8, 30, tzinfo=dt.UTC)
DAY = dt.date(2026, 6, 3)


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
    """A per-test engine that holds no idle connections.

    ``NullPool`` rather than the production pool, deliberately. This fixture is function-scoped, so
    a pooled engine would leave up to five idle sockets behind per test; across four integration
    modules in one pytest session that accumulated until asyncpg's 60-second connection timeout
    started firing — a different test each run, always a timeout, never an assertion. The suites
    each passed alone and CI runs them as separate processes, so nothing was ever wrong with the
    product; the harness was simply spending connections it did not need.
    """
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_slate() -> AsyncIterator[None]:
    """Every test starts from an empty reconciliation world.

    Matching reads *all* eligible rows, so a row another test left behind is not clutter — it is a
    candidate, and it could turn an unambiguous fixture into an ambiguous one. Deleted by table
    rather than truncated, in dependency order.
    """
    await _wipe()
    yield


async def _wipe() -> None:
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute("DELETE FROM match_result")
        await connection.execute("DELETE FROM settlement_line")
        await connection.execute("DELETE FROM settlement_batch")
        await connection.execute("DELETE FROM ledger_entry")
    finally:
        await connection.close()


async def _seed(
    *,
    lines: list[tuple[str, str, dt.date]],
    entries: list[tuple[str, str, str, dt.date]],
    marker: str = "",
) -> tuple[uuid.UUID, list[uuid.UUID], list[uuid.UUID]]:
    """Insert a batch of settlement lines and a set of ledger entries directly.

    Direct SQL rather than the ingestion path: these tests are about matching, and building each
    case through a CSV would make the input harder to read than the assertion.
    """
    batch_id = uuid.uuid4()
    line_ids: list[uuid.UUID] = []
    entry_ids: list[uuid.UUID] = []
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES ($1, $2, 'test', $3, $4, 'parsed')",
            batch_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            b"raw",
            RECEIVED_AT,
        )
        for number, (amount, currency, day) in enumerate(lines, start=1):
            line_id = uuid.uuid4()
            await connection.execute(
                "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
                " amount, currency, value_date, match_state)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, 'unmatched')",
                line_id,
                batch_id,
                number,
                f"psp_{marker}_{number}",
                decimal.Decimal(amount),
                currency,
                day,
            )
            line_ids.append(line_id)
        for ref, amount, currency, day in entries:
            entry_id = uuid.uuid4()
            await connection.execute(
                "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency,"
                " booked_at) VALUES ($1, $2, '4100', $3, $4, $5)",
                entry_id,
                f"GL-{marker}-{ref}",
                decimal.Decimal(amount),
                currency,
                dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.UTC),
            )
            entry_ids.append(entry_id)
    finally:
        await connection.close()
    return batch_id, line_ids, entry_ids


async def _match_rows() -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        return list(
            await connection.fetch(
                "SELECT settlement_line_id, ledger_entry_id, rule_id, tolerance_applied,"
                " tolerance_currency, matched_at FROM match_result ORDER BY rule_id"
            )
        )
    finally:
        await connection.close()


async def _states() -> dict[uuid.UUID, str]:
    connection = await asyncpg.connect(DSN)
    try:
        rows = await connection.fetch("SELECT id, match_state FROM settlement_line")
        return {row["id"]: row["match_state"] for row in rows}
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exact_match_persists_the_result_and_the_line_state(engine: AsyncEngine) -> None:
    _, line_ids, entry_ids = await _seed(
        lines=[("120.45", "EUR", DAY)], entries=[("a", "120.45", "EUR", DAY)], marker="exact"
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert (run.considered, run.matched, run.ambiguous, run.unmatched) == (1, 1, 0, 0)
    rows = await _match_rows()
    assert len(rows) == 1
    assert rows[0]["settlement_line_id"] == line_ids[0]
    assert rows[0]["ledger_entry_id"] == entry_ids[0]
    assert rows[0]["rule_id"] == MatchRule.EXACT_AMOUNT.value
    assert rows[0]["tolerance_applied"] is None
    assert rows[0]["tolerance_currency"] is None
    assert rows[0]["matched_at"] == MATCHED_AT
    assert (await _states())[line_ids[0]] == "matched"


@pytest.mark.asyncio
async def test_a_tolerance_match_records_what_it_absorbed(engine: AsyncEngine) -> None:
    _, line_ids, _ = await _seed(
        lines=[("1828.49", "GBP", DAY)], entries=[("a", "1828.48", "GBP", DAY)], marker="tol"
    )
    await run_matching(engine, matched_at=MATCHED_AT)

    rows = await _match_rows()
    assert rows[0]["rule_id"] == MatchRule.AMOUNT_WITHIN_TOLERANCE.value
    assert rows[0]["tolerance_applied"] == decimal.Decimal("0.01")
    assert rows[0]["tolerance_currency"] == "GBP"
    assert (await _states())[line_ids[0]] == "matched"


@pytest.mark.asyncio
async def test_an_unmatched_line_leaves_no_row_and_no_state_change(engine: AsyncEngine) -> None:
    _, line_ids, _ = await _seed(
        lines=[("120.45", "EUR", DAY)], entries=[("a", "999.99", "EUR", DAY)], marker="none"
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert (run.matched, run.unmatched) == (0, 1)
    assert await _match_rows() == []
    assert (await _states())[line_ids[0]] == "unmatched"


@pytest.mark.parametrize(
    ("ledger_amount", "expected"),
    [("120.44", True), ("120.46", True), ("120.43", False), ("120.47", False)],
)
@pytest.mark.asyncio
async def test_the_tolerance_boundary_holds_against_the_database(
    engine: AsyncEngine, ledger_amount: str, expected: bool
) -> None:
    """Below, on and beyond the band — through the real read, decide and write path."""
    await _seed(
        lines=[("120.45", "EUR", DAY)],
        entries=[("a", ledger_amount, "EUR", DAY)],
        marker=f"band{ledger_amount.replace('.', '')}",
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)
    assert (run.matched == 1) is expected


# --------------------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_consumed_ledger_entry_is_not_offered_again(engine: AsyncEngine) -> None:
    """Two identical lines, one entry — but the entry is taken in the first run, so the second
    line finds nothing rather than sharing it."""
    await _seed(
        lines=[("50.00", "EUR", DAY)], entries=[("a", "50.00", "EUR", DAY)], marker="consumed"
    )
    await run_matching(engine, matched_at=MATCHED_AT)

    _, later_lines, _ = await _seed(lines=[("50.00", "EUR", DAY)], entries=[], marker="consumed2")
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert run.considered == 1
    assert run.matched == 0
    assert (await _states())[later_lines[0]] == "unmatched"
    assert len(await _match_rows()) == 1


@pytest.mark.asyncio
async def test_an_already_matched_line_is_not_reconsidered(engine: AsyncEngine) -> None:
    await _seed(lines=[("50.00", "EUR", DAY)], entries=[("a", "50.00", "EUR", DAY)], marker="once")
    first = await run_matching(engine, matched_at=MATCHED_AT)
    assert first.matched == 1

    second = await run_matching(engine, matched_at=MATCHED_AT + dt.timedelta(days=1))
    assert (second.considered, second.matched) == (0, 0), "a second pass must find no work"
    rows = await _match_rows()
    assert len(rows) == 1
    assert rows[0]["matched_at"] == MATCHED_AT, "the original decision is not rewritten"


@pytest.mark.asyncio
async def test_matching_is_idempotent_across_repeated_runs(engine: AsyncEngine) -> None:
    await _seed(
        lines=[("10.00", "EUR", DAY), ("20.00", "EUR", DAY)],
        entries=[("a", "10.00", "EUR", DAY), ("b", "20.00", "EUR", DAY)],
        marker="idem",
    )
    await run_matching(engine, matched_at=MATCHED_AT)
    before = {(r["settlement_line_id"], r["ledger_entry_id"]) for r in await _match_rows()}

    for _ in range(3):
        run = await run_matching(engine, matched_at=MATCHED_AT)
        assert run.matched == 0
    after = {(r["settlement_line_id"], r["ledger_entry_id"]) for r in await _match_rows()}
    assert after == before


@pytest.mark.asyncio
async def test_the_batch_filter_restricts_the_lines_considered(engine: AsyncEngine) -> None:
    first_batch, _, _ = await _seed(
        lines=[("10.00", "EUR", DAY)], entries=[("a", "10.00", "EUR", DAY)], marker="b1"
    )
    await _seed(lines=[("20.00", "EUR", DAY)], entries=[("b", "20.00", "EUR", DAY)], marker="b2")

    run = await run_matching(engine, matched_at=MATCHED_AT, batch_id=first_batch)
    assert (run.considered, run.matched) == (1, 1)
    assert len(await _match_rows()) == 1


# --------------------------------------------------------------------------------------
# Ambiguity, through the database
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_identical_candidates_leave_the_line_unmatched(engine: AsyncEngine) -> None:
    _, line_ids, _ = await _seed(
        lines=[("75.00", "EUR", DAY)],
        entries=[("a", "75.00", "EUR", DAY), ("b", "75.00", "EUR", DAY)],
        marker="amb",
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert (run.matched, run.ambiguous) == (0, 1)
    assert await _match_rows() == []
    assert (await _states())[line_ids[0]] == "unmatched"


@pytest.mark.asyncio
async def test_two_lines_competing_for_one_entry_leave_both_unmatched(
    engine: AsyncEngine,
) -> None:
    _, line_ids, _ = await _seed(
        lines=[("75.00", "EUR", DAY), ("75.00", "EUR", DAY)],
        entries=[("a", "75.00", "EUR", DAY)],
        marker="compete",
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert (run.matched, run.ambiguous) == (0, 2)
    assert await _match_rows() == []
    states = await _states()
    assert all(states[line_id] == "unmatched" for line_id in line_ids)


@pytest.mark.asyncio
async def test_the_result_does_not_depend_on_row_insertion_order(engine: AsyncEngine) -> None:
    """The same world built in two orders must reconcile identically.

    A greedy matcher would let the physical insertion order decide which line takes a shared
    candidate; here the pairing is a property of the data.
    """

    async def reconcile(reverse: bool) -> set[tuple[str, str]]:
        await _wipe()
        amounts = [("10.00", "EUR", DAY), ("10.01", "EUR", DAY), ("20.00", "EUR", DAY)]
        entries = [("a", "10.00", "EUR", DAY), ("b", "20.00", "EUR", DAY)]
        await _seed(
            lines=list(reversed(amounts)) if reverse else amounts,
            entries=list(reversed(entries)) if reverse else entries,
            marker="order",
        )
        await run_matching(engine, matched_at=MATCHED_AT)
        connection = await asyncpg.connect(DSN)
        try:
            rows = await connection.fetch(
                "SELECT l.amount::text AS line_amount, e.amount::text AS entry_amount"
                " FROM match_result m"
                " JOIN settlement_line l ON l.id = m.settlement_line_id"
                " JOIN ledger_entry e ON e.id = m.ledger_entry_id"
            )
            return {(row["line_amount"], row["entry_amount"]) for row in rows}
        finally:
            await connection.close()

    forwards = await reconcile(reverse=False)
    backwards = await reconcile(reverse=True)
    assert forwards == backwards
    assert forwards, "the fixture must match something for this to mean anything"


# --------------------------------------------------------------------------------------
# Concurrency — the guarantee no single-threaded test can give
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workers_cannot_double_match_one_line(engine: AsyncEngine) -> None:
    """Both read the same world and propose the same pair. Only one row may exist."""
    await _seed(lines=[("42.00", "EUR", DAY)], entries=[("a", "42.00", "EUR", DAY)], marker="race1")
    first, second = create_engine(_settings()), create_engine(_settings())
    try:
        outcomes = await asyncio.gather(
            run_matching(first, matched_at=MATCHED_AT),
            run_matching(second, matched_at=MATCHED_AT),
            return_exceptions=True,
        )
    finally:
        await first.dispose()
        await second.dispose()

    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), f"concurrent matching raised: {outcome!r}"

    rows = await _match_rows()
    assert len(rows) == 1, "the unique constraint on the line must make the race impossible"
    runs = [o for o in outcomes if not isinstance(o, BaseException)]
    assert sum(r.matched for r in runs) == 1
    assert sum(r.lost_races for r in runs) == 1, "the loser must know it lost, not think it won"


@pytest.mark.asyncio
async def test_two_workers_cannot_double_consume_one_ledger_entry(engine: AsyncEngine) -> None:
    """Two different lines in two different batches, each uniquely matching the same entry.

    Neither worker can see the other's proposal, so both will try. The unique constraint on
    ``ledger_entry_id`` is what stops the entry being reconciled twice.
    """
    await _seed(lines=[("64.00", "EUR", DAY)], entries=[("a", "64.00", "EUR", DAY)], marker="r2a")
    _, second_line, _ = await _seed(lines=[("64.00", "EUR", DAY)], entries=[], marker="r2b")

    # Both lines now match the single entry, so a single-threaded run would call it ambiguous.
    # Restricting each worker to its own batch makes each see an unambiguous world — which is
    # exactly the situation where only the database can arbitrate.
    connection = await asyncpg.connect(DSN)
    try:
        batches = [
            row["id"]
            for row in await connection.fetch("SELECT id FROM settlement_batch ORDER BY created_at")
        ]
    finally:
        await connection.close()

    first, second = create_engine(_settings()), create_engine(_settings())
    try:
        outcomes = await asyncio.gather(
            run_matching(first, matched_at=MATCHED_AT, batch_id=batches[0]),
            run_matching(second, matched_at=MATCHED_AT, batch_id=batches[1]),
            return_exceptions=True,
        )
    finally:
        await first.dispose()
        await second.dispose()

    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), f"concurrent matching raised: {outcome!r}"

    rows = await _match_rows()
    assert len(rows) == 1, "one ledger entry may be consumed once"

    runs = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert sum(run.matched for run in runs) == 1, "exactly one worker may claim the entry"

    states = await _states()
    matched_lines = {row["settlement_line_id"] for row in rows}
    for line_id, state in states.items():
        expected = "matched" if line_id in matched_lines else "unmatched"
        assert state == expected, "a line's state must agree with whether a result exists"
    assert second_line[0] in states

    # Deliberately *not* asserted: that a race actually occurred. Two coroutines gathered on
    # separate engines may genuinely serialise, in which case the second worker reads the entry as
    # already consumed and proposes nothing — a correct outcome with `lost_races == 0`. Requiring
    # the interleaving would make this test fail on timing rather than on behaviour. What it does
    # assert holds either way, and the deterministic loser-side behaviour is proven separately by
    # test_a_lost_race_leaves_the_line_cleanly_retryable, which sequences the two runs explicitly.


@pytest.mark.asyncio
async def test_a_lost_race_leaves_the_line_cleanly_retryable(engine: AsyncEngine) -> None:
    """The loser writes nothing: no result, no state change, and the next run re-reads the world."""
    await _seed(lines=[("88.00", "EUR", DAY)], entries=[("a", "88.00", "EUR", DAY)], marker="r3a")
    _, loser_line, _ = await _seed(lines=[("88.00", "EUR", DAY)], entries=[], marker="r3b")

    connection = await asyncpg.connect(DSN)
    try:
        batches = [
            row["id"]
            for row in await connection.fetch("SELECT id FROM settlement_batch ORDER BY created_at")
        ]
    finally:
        await connection.close()

    await run_matching(engine, matched_at=MATCHED_AT, batch_id=batches[0])
    losing = await run_matching(engine, matched_at=MATCHED_AT, batch_id=batches[1])

    assert losing.considered == 1
    assert losing.matched == 0
    assert (await _states())[loser_line[0]] == "unmatched"
    assert len(await _match_rows()) == 1


# --------------------------------------------------------------------------------------
# The database is still the final guard
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_date_filter_does_not_depend_on_the_database_timezone() -> None:
    """The reduction of ``booked_at`` to a calendar day is pinned to UTC, not to a session setting.

    ``booked_at`` is TIMESTAMPTZ, and a plain ``::date`` cast is resolved in PostgreSQL's session
    ``TimeZone``. Nothing in this project pins that, so the same rows would reconcile differently
    on a server configured for a different zone — and because a consumed ledger entry can never be
    released (ADR-024), the divergence would be permanent.

    The instant below is 23:30 UTC on the second, which is 00:30 on the *third* in Berlin. Under a
    plain cast the entry falls outside the one-day window and the line goes residual; under the
    UTC-pinned cast it matches, whatever the database is set to.
    """
    value_date = dt.date(2026, 3, 1)
    booked = dt.datetime(2026, 3, 2, 23, 30, tzinfo=dt.UTC)

    await _seed(lines=[("500.00", "EUR", value_date)], entries=[], marker="tz")
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES (gen_random_uuid(), 'GL-tz-a', '4100', 500.0000, 'EUR', $1)",
            booked,
        )
    finally:
        await connection.close()

    # The zone is set on this engine's own connections, not on the database. An earlier version
    # used ALTER DATABASE, which is persistent, affects every other connection, and left the whole
    # suite waiting on a lock — a test that reconfigures the server to make its point is a worse
    # problem than the one it is testing.
    shifted = create_async_engine(
        async_dsn(_settings()),
        connect_args={"server_settings": {"timezone": "Europe/Berlin"}},
    )
    try:
        async with shifted.connect() as verify:
            zone = (await verify.execute(sa_text("SHOW TimeZone"))).scalar_one()
            assert zone == "Europe/Berlin", "the shifted zone must actually be in effect"
        run = await run_matching(shifted, matched_at=MATCHED_AT)
    finally:
        await shifted.dispose()

    assert run.matched == 1, (
        "the candidate is one day away in UTC and must match regardless of the server's TimeZone"
    )


@pytest.mark.asyncio
async def test_direct_sql_cannot_match_one_line_twice(engine: AsyncEngine) -> None:
    _, line_ids, _ = await _seed(
        lines=[("30.00", "EUR", DAY)],
        entries=[("a", "30.00", "EUR", DAY), ("b", "31.00", "EUR", DAY)],
        marker="guard1",
    )
    await run_matching(engine, matched_at=MATCHED_AT)

    connection = await asyncpg.connect(DSN)
    try:
        other = await connection.fetchval(
            "SELECT id FROM ledger_entry WHERE id NOT IN (SELECT ledger_entry_id FROM match_result)"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO match_result (id, settlement_line_id, ledger_entry_id, rule_id,"
                " matched_at) VALUES (gen_random_uuid(), $1, $2, 'exact_amount', now())",
                line_ids[0],
                other,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_direct_sql_cannot_consume_one_ledger_entry_twice(engine: AsyncEngine) -> None:
    _, line_ids, entry_ids = await _seed(
        lines=[("30.00", "EUR", DAY), ("77.00", "EUR", DAY)],
        entries=[("a", "30.00", "EUR", DAY)],
        marker="guard2",
    )
    await run_matching(engine, matched_at=MATCHED_AT)

    connection = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO match_result (id, settlement_line_id, ledger_entry_id, rule_id,"
                " matched_at) VALUES (gen_random_uuid(), $1, $2, 'exact_amount', now())",
                line_ids[1],
                entry_ids[0],
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_matching_writes_to_no_later_increment_table(engine: AsyncEngine) -> None:
    """M2.2 matches. It does not classify, raise an exception, or assemble evidence."""
    await _seed(
        lines=[("30.00", "EUR", DAY), ("99.00", "EUR", DAY)],
        entries=[("a", "30.00", "EUR", DAY)],
        marker="scope",
    )
    await run_matching(engine, matched_at=MATCHED_AT)

    connection = await asyncpg.connect(DSN)
    try:
        for table in ("exception", "evidence", "treatment_proposal", "approval", "adjustment"):
            count = await connection.fetchval(f"SELECT count(*) FROM {table}")
            assert count == 0, f"matching wrote to {table}, which belongs to a later increment"
    finally:
        await connection.close()


# --------------------------------------------------------------------------------------
# End to end, from the committed corpus
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_committed_corpus_ingests_and_reconciles_end_to_end(
    engine: AsyncEngine,
) -> None:
    """Ingestion and matching together, over the artifacts M1.3 committed.

    The canonical corpus holds one instance of every condition, so its clearance rate reports the
    shape of the catalogue rather than the matcher's reach — the bulk measurement lives in the unit
    suite. What this proves is that the two increments compose against the real schema.
    """
    connection = await asyncpg.connect(DSN)
    try:
        for row in json.loads((CORPUS / "records.json").read_text(encoding="utf-8"))[
            "ledger_entries"
        ]:
            await connection.execute(
                "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency,"
                " booked_at, description) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.UUID(row["id"]),
                row["external_ref"],
                row["account_code"],
                decimal.Decimal(row["amount"]),
                row["currency"],
                dt.datetime.fromisoformat(row["booked_at"]),
                row["description"],
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

    run = await run_matching(engine, matched_at=MATCHED_AT)
    assert run.considered == 17
    assert run.matched == 4
    assert run.ambiguous == 0

    rows = await _match_rows()
    assert {row["rule_id"] for row in rows} == {MatchRule.EXACT_AMOUNT.value}
    states = await _states()
    assert sum(1 for state in states.values() if state == "matched") == 4


@pytest.mark.asyncio
async def test_a_wider_policy_clears_the_corpus_near_misses(engine: AsyncEngine) -> None:
    """The band is configurable, and the corpus proves it changes the outcome.

    Two of the corpus's residual lines differ from their ledger entry by two minor units. Under the
    default one-unit band they stay residual; under a two-unit band they clear. Run here so the
    policy's effect is demonstrated rather than asserted — and so the default is visibly a choice.
    """
    connection = await asyncpg.connect(DSN)
    try:
        for row in json.loads((CORPUS / "records.json").read_text(encoding="utf-8"))[
            "ledger_entries"
        ]:
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
        await ingest(
            engine,
            (CORPUS / "settlement" / name).read_bytes(),
            source="file-drop",
            received_at=RECEIVED_AT,
        )

    wider = TolerancePolicy(
        amount={code: band * 2 for code, band in DEFAULT_POLICY.amount.items()},
        value_date_window_days=DEFAULT_POLICY.value_date_window_days,
    )
    run = await run_matching(engine, matched_at=MATCHED_AT, policy=wider)

    assert run.matched > 4, "doubling the band must clear the two-minor-unit near misses"
    rows = await _match_rows()
    absorbed = [row for row in rows if row["tolerance_applied"] is not None]
    assert absorbed, "and they must be recorded as tolerance matches, not exact ones"
    assert all(row["tolerance_applied"] == decimal.Decimal("0.02") for row in absorbed)


# --------------------------------------------------------------------------------------
# Precedence, through the database
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_ambiguity_is_not_resolved_by_a_tolerance_candidate(
    engine: AsyncEngine,
) -> None:
    """Two exact candidates plus one near one: nothing is written, and nothing is consumed.

    A matcher that dropped the unresolved exact contest before moving on would find the near
    candidate unique within its own tier and write it — a weaker rule settling an ambiguity that a
    stronger rule had refused, and irreversibly, because ``match_result`` is unique on the ledger
    entry.
    """
    _, line_ids, entry_ids = await _seed(
        lines=[("100.00", "EUR", DAY)],
        entries=[
            ("a", "100.00", "EUR", DAY),
            ("b", "100.00", "EUR", DAY),
            ("c", "100.01", "EUR", DAY),
        ],
        marker="tierblock",
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert (run.matched, run.ambiguous) == (0, 1)
    assert await _match_rows() == []
    assert (await _states())[line_ids[0]] == "unmatched"

    connection = await asyncpg.connect(DSN)
    try:
        consumed = await connection.fetchval(
            "SELECT count(*) FROM match_result WHERE ledger_entry_id = ANY($1::uuid[])", entry_ids
        )
        assert consumed == 0, "no candidate may be consumed while the contest is unresolved"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_a_contested_entry_is_not_taken_by_a_lower_tier(engine: AsyncEngine) -> None:
    """Two lines contest one entry exactly; a third would take it under tolerance. It must not."""
    _, line_ids, _ = await _seed(
        lines=[("100.00", "EUR", DAY), ("100.00", "EUR", DAY), ("100.01", "EUR", DAY)],
        entries=[("a", "100.00", "EUR", DAY)],
        marker="steal",
    )
    run = await run_matching(engine, matched_at=MATCHED_AT)

    assert run.matched == 0
    assert await _match_rows() == []
    states = await _states()
    assert all(states[line_id] == "unmatched" for line_id in line_ids)


@pytest.mark.asyncio
async def test_exact_ambiguity_holds_under_reversed_insertion_order(engine: AsyncEngine) -> None:
    """The contest must be refused whichever order the rows physically arrived in."""

    async def reconcile(reverse: bool) -> int:
        await _wipe()
        entries = [
            ("a", "100.00", "EUR", DAY),
            ("b", "100.00", "EUR", DAY),
            ("c", "100.01", "EUR", DAY),
        ]
        await _seed(
            lines=[("100.00", "EUR", DAY)],
            entries=list(reversed(entries)) if reverse else entries,
            marker="tierorder",
        )
        return (await run_matching(engine, matched_at=MATCHED_AT)).matched

    assert await reconcile(reverse=False) == 0
    assert await reconcile(reverse=True) == 0


# --------------------------------------------------------------------------------------
# Persisted precision against fixture construction intent
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_persisted_match_pairs_rows_from_the_same_constructed_scenario(
    engine: AsyncEngine,
) -> None:
    """The invariant that matters, asserted against what is actually in the database.

    The unit measurement grades the pure matcher; this grades ``match_result`` after ingestion,
    matching and persistence have all run. A pair whose two sides come from different constructed
    scenarios is a false financial match — and because the ledger entry can never be released, it
    is permanent.

    Construction metadata is read here, by the test, to judge production output. The matcher was
    handed none of it.
    """
    records = json.loads((CORPUS / "records.json").read_text(encoding="utf-8"))
    scenario_of_entry = {
        uuid.UUID(row["id"]): row["scenario_id"] for row in records["ledger_entries"]
    }
    # Joined on ``psp_reference``, not on the corpus's row id. These lines reach the database
    # through *ingestion*, which mints its own identifiers — the corpus ids belong to the fixture
    # loader and never appear here. The PSP reference is what survives the CSV, which is the whole
    # point of it. SC-012 repeats one reference across two of its own lines, so the mapping is
    # still a function; asserted rather than assumed.
    scenario_by_reference: dict[str, str] = {}
    for batch in records["batches"]:
        for row in batch["lines"]:
            existing = scenario_by_reference.setdefault(row["psp_reference"], row["scenario_id"])
            assert existing == row["scenario_id"], (
                f"{row['psp_reference']} spans two scenarios; it cannot identify one"
            )

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

    run = await run_matching(engine, matched_at=MATCHED_AT)
    rows = await _match_rows()

    assert run.matched == len(rows) == 4

    connection = await asyncpg.connect(DSN)
    try:
        reference_of = {
            record["id"]: record["psp_reference"]
            for record in await connection.fetch("SELECT id, psp_reference FROM settlement_line")
        }
    finally:
        await connection.close()

    def scenario_of(match_row: asyncpg.Record) -> str:
        return scenario_by_reference[reference_of[match_row["settlement_line_id"]]]

    false_matches = [
        (scenario_of(row), scenario_of_entry[row["ledger_entry_id"]])
        for row in rows
        if scenario_of(row) != scenario_of_entry[row["ledger_entry_id"]]
    ]
    assert false_matches == [], f"persisted false match(es): {false_matches}"

    matched_scenarios = sorted(scenario_of(row) for row in rows)
    assert matched_scenarios == [
        "SC-001-exact-match",
        "SC-002-reference-mismatch",
        "SC-006-chargeback-reversal",
        "SC-008-cross-period-refund",
    ]
    # SC-006 and SC-008 each keep a residual line: only part of each corresponds to the ledger.
    assert all(row["rule_id"] == MatchRule.EXACT_AMOUNT.value for row in rows)
