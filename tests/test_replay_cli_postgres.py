"""M4.3 — the replay command line itself, against real PostgreSQL.

Acceptance criterion 8 is phrased in terms of the CLI, not the function it calls:

    **The DLQ replay CLI** produces exactly one applied posting for the operation — verified by the
    simulated ledger's applied-count, not by our own records — and replay of an already-`CONFIRMED`
    operation applies nothing further.

The first version of this increment tested ``replay_dead_letter`` thoroughly and the command that
acceptance criterion names not at all — five reviewers said so independently, and they were right:
argument handling, the adapter registry, batch behaviour after a bad entry, the exit code and what
reaches the terminal were all unexercised, and every one of those is a place an operator's mistake
becomes a financial one.

``main()`` is driven with an argument vector, exactly as a shell would. The database it talks to is
the disposable test database, named by ``LECP_POSTGRES_DSN`` like every other integration module.

Marked ``integration``; needs PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import decimal
import os
import pathlib
import random
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ledger import (
    LedgerAdapterCapabilities,
    PostingInstruction,
    PostingOutcome,
    Rejected,
    SimulatedLedger,
)
from ledger_exception_control_plane.ledger.transport import LedgerTransportError, RetryableCause
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from ledger_exception_control_plane.operations import enqueue_posting
from ledger_exception_control_plane.operations.__main__ import ADAPTERS, main
from ledger_exception_control_plane.operations.retry import (
    RetryPolicy,
    attempt_one,
)

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)
EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
REBOOK_ACCOUNT = "4100"

ONE_SHOT = RetryPolicy(
    base_delay=dt.timedelta(seconds=1),
    multiplier=2.0,
    cap=dt.timedelta(seconds=8),
    max_attempts=1,
    time_budget=dt.timedelta(hours=1),
)


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


@pytest.fixture(autouse=True)
def dsn_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI builds its own ``Settings``, exactly as a shell invocation would."""
    monkeypatch.setenv("LECP_POSTGRES_DSN", DSN)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
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
    connection = await asyncpg.connect(DSN)
    try:
        for table in (
            "dlq",
            "posting_attempt",
            "outbox",
            "adjustment",
            "approval",
            "treatment_proposal",
            "exception",
            "match_result",
            "settlement_line",
            "settlement_batch",
        ):
            await connection.execute(f"DELETE FROM {table}")
    finally:
        await connection.close()


async def _seed(marker: str) -> uuid.UUID:
    batch_id, line_id, exception_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO settlement_batch"
            " (id, content_hash, source, raw_payload, received_at, status)"
            " VALUES ($1, $2, 'test', $3, $4, 'parsed')",
            batch_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            b"raw",
            EPOCH,
        )
        await connection.execute(
            "INSERT INTO settlement_line (id, settlement_batch_id, line_number, psp_reference,"
            " merchant_reference, transaction_type, amount, currency, value_date, match_state)"
            " VALUES ($1, $2, 1, $3, $4, 'capture', 2799.97, 'EUR', $5, 'unmatched')",
            line_id,
            batch_id,
            f"psp_{marker}",
            f"ORD-{marker}",
            EPOCH.date(),
        )
        await connection.execute(
            "INSERT INTO exception (id, settlement_line_id, line_match_state, classification,"
            " status, rule_id, classifier_version, correlation_id, created_at)"
            " VALUES ($1, $2, 'unmatched', 'fee_split', 'open', 'fees_deducted_from_a_capture',"
            " 'residual-r2', $3, $4)",
            exception_id,
            line_id,
            f"lecp:{marker}",
            EPOCH,
        )
        approval_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, decided_at)"
            " VALUES ($1, $2, 1, 'approved', 'rebook', 'controller-a', $3)",
            approval_id,
            exception_id,
            EPOCH,
        )
    finally:
        await connection.close()
    return approval_id


def _instruction(exception_id: uuid.UUID, **overrides: Any) -> AdjustmentInstruction:
    base = AdjustmentInstruction(
        exception_id=exception_id,
        treatment=TreatmentCode.REBOOK,
        amount=decimal.Decimal("2799.97"),
        currency="EUR",
        account_code=REBOOK_ACCOUNT,
        period="2026-06",
        quantum=MONEY_QUANTUM,
        rounding=ROUNDING,
        ledger_context_version=DEMO_LEDGER_CONTEXT.version,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


async def run_cli(argv: list[str]) -> int:
    """Drive ``main`` exactly as a shell would, from inside an async test.

    In a worker thread, because ``main`` calls :func:`asyncio.run` and that refuses to start a loop
    inside one that is already running. Threading it is not a workaround for the test's benefit —
    it is what keeps the entry point under test *the real one*, rather than reaching past it to the
    coroutine it wraps and leaving argument parsing, the adapter registry and the exit code
    unexercised.
    """
    return await asyncio.to_thread(main, argv)


async def _rows(table: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        return await connection.fetch(f"SELECT * FROM {table}")
    finally:
        await connection.close()


class _RefusesToConnect:
    name = "refuses-to-connect"

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities()

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        raise LedgerTransportError(RetryableCause.TCP_CONNECT, "nothing was written")


async def _dead_letter_one(engine: AsyncEngine, marker: str) -> tuple[uuid.UUID, str]:
    """Enqueue an operation, fail its only attempt at the transport, and return its dead letter."""
    approval_id = await _seed(marker)
    connection = await asyncpg.connect(DSN)
    try:
        exception_id = await connection.fetchval(
            "SELECT exception_id FROM approval WHERE id = $1", approval_id
        )
    finally:
        await connection.close()

    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    await attempt_one(
        engine,
        adjustment_id=record.adjustment_id,
        adapter=_RefusesToConnect(),
        policy=ONE_SHOT,
        now=EPOCH,
        rng=random.Random(1),
    )
    (entry,) = await _rows("dlq")
    return entry["id"], record.identity.operation_id


# ======================================================================================
# The exit criterion, driven through the command
# ======================================================================================


@pytest.mark.asyncio
async def test_the_cli_replays_a_dead_letter_and_applies_exactly_one_posting(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Acceptance criterion 8, through the command it actually names.**

    The applied-count is read off the very ledger the CLI posted to, which is only possible because
    the adapter registry is substituted for the duration — otherwise the command constructs its own
    ``SimulatedLedger`` and the count would be measured on a different set of books, which is a test
    that proves nothing.
    """
    dlq_id, operation_id = await _dead_letter_one(engine, "cliapply")
    ledger = SimulatedLedger()
    monkeypatch.setitem(ADAPTERS, "simulated", lambda: ledger)

    code = await run_cli(["replay", "--id", str(dlq_id)])

    assert code == 0
    assert ledger.applied_count(operation_id) == 1, "measured at the ledger, not at us"
    assert ledger.posts_received == 1, "exactly one send left the client"

    out = capsys.readouterr().out
    assert str(dlq_id) in out
    assert "applied" in out


@pytest.mark.asyncio
async def test_the_cli_sends_nothing_for_an_already_confirmed_operation(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second half of acceptance criterion 8, also through the command.

    ``posts_received`` is the assertion that matters: the applied-count alone stays at one whether
    the CLI sent nothing or sent a duplicate the ledger suppressed, so on its own it cannot tell the
    safe case from the dangerous one.
    """
    dlq_id, operation_id = await _dead_letter_one(engine, "cliconfirmed")
    ledger = SimulatedLedger()
    monkeypatch.setitem(ADAPTERS, "simulated", lambda: ledger)

    assert await run_cli(["replay", "--id", str(dlq_id)]) == 0
    assert ledger.posts_received == 1

    # A second dead letter for the same, now-confirmed, operation.
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute("UPDATE dlq SET replay_state = 'pending', replayed_at = NULL")
    finally:
        await connection.close()

    assert await run_cli(["replay", "--id", str(dlq_id)]) == 0

    assert ledger.applied_count(operation_id) == 1
    assert ledger.posts_received == 1, "the second invocation sent something"
    assert "already_confirmed" in capsys.readouterr().out


# ======================================================================================
# Batch behaviour, bad input and what reaches the terminal
# ======================================================================================


@pytest.mark.asyncio
async def test_one_bad_entry_does_not_abandon_the_rest_of_the_batch(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A forged id is reported and skipped, and the real entries are still worked.**

    The first version let ``LookupError`` escape as a traceback, so a single bad id abandoned every
    entry after it and exited with a code that said nothing about how far it had got. Two reviewers
    walked through it.
    """
    first, first_operation = await _dead_letter_one(engine, "clibatch1")
    ledger = SimulatedLedger()
    monkeypatch.setitem(ADAPTERS, "simulated", lambda: ledger)

    forged = uuid.uuid4()
    code = await run_cli(["replay", "--id", str(forged), "--id", str(first)])

    captured = capsys.readouterr().out
    assert str(forged) in captured and "skipped" in captured
    assert ledger.applied_count(first_operation) == 1, "the good entry was still replayed"
    assert code == 1, "the run reports that something was left undone"


@pytest.mark.asyncio
async def test_replay_needs_a_target(engine: AsyncEngine) -> None:
    """``replay`` with neither ``--id`` nor ``--all`` is an error, not an empty batch.

    A command that quietly did nothing would look identical to one that found nothing to do, and an
    operator draining a queue needs those to be distinguishable.
    """
    with pytest.raises(SystemExit) as exit_info:
        await run_cli(["replay"])
    assert exit_info.value.code == 2


@pytest.mark.asyncio
async def test_dry_run_sends_nothing(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` reports and sends nothing — asserted at the ledger, not at the output."""
    dlq_id, operation_id = await _dead_letter_one(engine, "clidry")
    ledger = SimulatedLedger()
    monkeypatch.setitem(ADAPTERS, "simulated", lambda: ledger)

    assert await run_cli(["replay", "--all", "--dry-run"]) == 0

    assert ledger.posts_received == 0
    assert ledger.applied_count(operation_id) == 0
    assert str(dlq_id) in capsys.readouterr().out
    assert len(await _rows("posting_attempt")) == 1, "no new attempt was recorded"


@pytest.mark.asyncio
async def test_list_reports_the_pending_queue(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    dlq_id, _ = await _dead_letter_one(engine, "clilist")

    assert await run_cli(["list"]) == 0

    captured = capsys.readouterr()
    assert str(dlq_id) in captured.out
    assert "1 pending" in captured.err


@pytest.mark.asyncio
async def test_an_empty_queue_is_reported_as_empty(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run_cli(["replay", "--all"]) == 0
    assert "no pending dead letters" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_cli_never_prints_the_ledgers_own_rejection_text(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Provider text is persisted, not rendered.**

    A rejection's ``reason`` is provider-controlled and unbounded, and the first version printed it
    straight to the terminal. The dead letter still records it; the command reports the outcome.
    """
    approval_id = await _seed("clireject")
    connection = await asyncpg.connect(DSN)
    try:
        exception_id = await connection.fetchval(
            "SELECT exception_id FROM approval WHERE id = $1", approval_id
        )
    finally:
        await connection.close()

    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    secret = "DECLINED: account 4100 flagged by RULE-9 <internal note>"
    await attempt_one(
        engine,
        adjustment_id=record.adjustment_id,
        adapter=SimulatedLedger(responder=lambda _op, _i: Rejected(reason=secret)),
        policy=ONE_SHOT,
        now=EPOCH,
        rng=random.Random(1),
    )
    (entry,) = await _rows("dlq")

    ledger = SimulatedLedger()
    monkeypatch.setitem(ADAPTERS, "simulated", lambda: ledger)
    await run_cli(["replay", "--id", str(entry["id"])])

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "rejected" in captured.out


@pytest.mark.asyncio
async def test_a_rejected_dead_letter_leaves_the_queue(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A rejection is finished, not blocked**, and its entry must not poison the queue.

    The first version reported it as ``refused`` and left it pending, so one declined posting
    starved ``replay --all`` forever and returned a non-zero exit code on every subsequent run.
    Four reviewers found it.
    """
    approval_id = await _seed("cliqueue")
    connection = await asyncpg.connect(DSN)
    try:
        exception_id = await connection.fetchval(
            "SELECT exception_id FROM approval WHERE id = $1", approval_id
        )
    finally:
        await connection.close()

    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    await attempt_one(
        engine,
        adjustment_id=record.adjustment_id,
        adapter=SimulatedLedger(responder=lambda _op, _i: Rejected(reason="closed")),
        policy=ONE_SHOT,
        now=EPOCH,
        rng=random.Random(1),
    )

    ledger = SimulatedLedger()
    monkeypatch.setitem(ADAPTERS, "simulated", lambda: ledger)

    assert await run_cli(["replay", "--all"]) == 0, "a rejection is resolved, not left undone"
    assert ledger.posts_received == 0, "nothing was sent for an operation the ledger declined"

    (entry,) = await _rows("dlq")
    assert entry["replay_state"] == "replayed"
    assert entry["replayed_at"] is not None

    # And the queue is now genuinely empty, so a second pass has nothing to starve on.
    assert await run_cli(["replay", "--all"]) == 0


@pytest.mark.asyncio
async def test_the_adapter_registry_is_closed(engine: AsyncEngine) -> None:
    """An operator cannot point a replay at an arbitrary import path."""
    with pytest.raises(SystemExit) as exit_info:
        await run_cli(["replay", "--all", "--adapter", "whatever-i-like"])
    assert exit_info.value.code == 2
    assert set(ADAPTERS) == {"simulated"}


@pytest.mark.asyncio
async def test_the_cli_never_prints_the_database_password(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The DSN is a secret and reaches the terminal through no path, including the empty one."""
    await _dead_letter_one(engine, "clisecret")

    await run_cli(["list"])

    captured = capsys.readouterr()
    assert "lecp_local_dev" not in captured.out
    assert "lecp_local_dev" not in captured.err
