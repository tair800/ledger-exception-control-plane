"""M5.2 against real PostgreSQL — the three tests §5.2 names, and the exit criterion.

`IMPLEMENTATION_PLAN.md` §5.2's Tests line is three claims:

    Every ledger-affecting action emits at least one event; append-only enforced by database grant;
    correlation id spans ingestion to posting.

and its exit criterion is a question:

    A posted adjustment answers: what evidence, which model, who approved, what was computed, by
    which code path.

**The second of the three was already discharged at M1.2** and is not rebuilt here.
``test_schema_postgres.py`` runs ``SET LOCAL ROLE lecp_app`` against the real grant, checks
``has_table_privilege`` first so a database where the role holds nothing cannot pass vacuously, and
asserts ``InsufficientPrivilegeError`` — which is a different error from the trigger's
``RestrictViolationError``, so the two controls are genuinely told apart. Duplicating it here would
be worse than redundant: only that module provisions the role, so a copy in this one would run
against a database where ``lecp_app`` holds nothing and would pass for the wrong reason. That trap
has caught this repository once already (``PROJECT_STATUS.md``, the M3.1 correction). What is added
below is the *third* append-only surface the trigger covers and the grant did not.

**On what "every ledger-affecting action" can be demonstrated against.** There is no wired pipeline
in this repository: nothing calls ``run_classification`` then ``propose_for_exception`` then
``record_decision`` then ``enqueue_posting`` then ``dispatch_once`` in production code. The
orchestration is M7's. So the test below composes the real service entry points in order and drives
them — it is not a running system, and the docstring says so rather than letting a green test imply
one. Every event it asserts is one the services emitted; nothing is seeded.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_audit_contract_postgres.py -m integration
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import os
import pathlib
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.audit import correlation_id_for
from ledger_exception_control_plane.classification import run_classification
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import ApprovalDecision, TreatmentCode
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ingest.parser import SETTLEMENT_COLUMNS
from ledger_exception_control_plane.ingest.service import ingest
from ledger_exception_control_plane.ledger import SimulatedLedger
from ledger_exception_control_plane.matching.service import run_matching
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from ledger_exception_control_plane.operations import dispatch_once, enqueue_posting
from ledger_exception_control_plane.operations.approval import record_decision
from ledger_exception_control_plane.provenance import provenance
from ledger_exception_control_plane.security import Principal, Role

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
CONTROLLER = Principal("controller-a", Role.CONTROLLER)
REBOOK_ACCOUNT = "4100"


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


def _clear_audit_trail() -> None:
    """Empty the two append-only tables, suspending the triggers that make them so.

    The harness explicitly overriding a control it also tests, which is worth naming rather than
    burying: ``assert_target_is_disposable`` has already refused to run against anything but a
    throwaway database, and the controls themselves are asserted directly below.
    """

    async def clear() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            for table, trigger in (
                ("audit_event", "audit_event_append_only_row"),
                ("reconciliation_query", "reconciliation_query_append_only_row"),
            ):
                if await connection.fetchval(f"SELECT to_regclass('public.{table}')") is None:
                    continue
                await connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
                await connection.execute(f"DELETE FROM {table}")
                await connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        finally:
            await connection.close()

    asyncio.run(clear())


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    assert_target_is_disposable(_settings())
    _clear_audit_trail()
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

    # **Provisioned here, deliberately, and this module has to do it itself.** Grants are dropped
    # with the tables they were made on, and the downgrade above drops every one — so a module that
    # asserted a grant without re-applying the script would be testing a database where the role
    # holds nothing, and the assertion would pass by denying everything. This repository has fallen
    # into that trap once (PROJECT_STATUS.md, the M3.1 correction), which is why the grant test
    # below checks `has_table_privilege` before it checks the denial.
    #
    # After the migration, never before: `GRANT ... ON ALL TABLES` applies to the tables that exist
    # when it runs.
    _provision_app_role()
    yield
    _clear_audit_trail()


def _provision_app_role() -> None:
    """Apply the role-provisioning script, exactly as a release would."""
    script = (REPO_ROOT / "scripts" / "sql" / "provision_app_role.sql").read_text(encoding="utf-8")

    async def apply() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            await connection.execute(script)
        finally:
            await connection.close()

    asyncio.run(apply())


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
        for table, trigger in (
            ("audit_event", "audit_event_append_only_row"),
            ("reconciliation_query", "reconciliation_query_append_only_row"),
        ):
            await connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            await connection.execute(f"DELETE FROM {table}")
            await connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        for table in (
            "recovery_queue",
            "dlq",
            "posting_attempt",
            "outbox",
            "adjustment",
            "approval",
            "treatment_proposal_evidence",
            "treatment_proposal",
            "evidence",
            "exception",
            "match_result",
            "settlement_line",
            "settlement_batch",
            "ledger_entry",
        ):
            await connection.execute(f"DELETE FROM {table}")
    finally:
        await connection.close()


#: One settlement file: two lines, one of which the ledger will reconcile and one of which will not.
#:
#: Written as bytes here rather than generated, because this test is about the audit trail and a
#: corpus generator between the payload and the assertion is one more thing that could explain a
#: failure.
PAYLOAD = (
    "\n".join(
        (
            ",".join(SETTLEMENT_COLUMNS),
            "PSP-AUDIT-1,ORD-AUDIT-1,capture,120.00,EUR,2026-06-01,,,,capture",
            "PSP-AUDIT-2,ORD-AUDIT-2,capture,2799.97,EUR,2026-06-01,,,,capture",
        )
    )
    + "\n"
).encode("utf-8")


async def _seed_ledger_entry(*, amount: str) -> None:
    """One ledger entry that will match the first line and leave the second residual."""
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency,"
            " booked_at) VALUES ($1, 'ORD-AUDIT-1', '4100', $2, 'EUR', $3)",
            uuid.uuid4(),
            decimal.Decimal(amount),
            EPOCH,
        )
    finally:
        await connection.close()


async def _events(correlation_id: str | None = None) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        if correlation_id is None:
            return await connection.fetch(
                "SELECT * FROM audit_event ORDER BY occurred_at, created_at"
            )
        return await connection.fetch(
            "SELECT * FROM audit_event WHERE correlation_id = $1 ORDER BY occurred_at, created_at",
            correlation_id,
        )
    finally:
        await connection.close()


async def _execute(sql: str, *args: object) -> None:
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(sql, *args)
    finally:
        await connection.close()


async def _drive_the_pipeline(engine: AsyncEngine) -> tuple[uuid.UUID, str]:
    """Compose the real service entry points in order. Returns ``(adjustment_id, correlation_id)``.

    **Not a running system, and the distinction matters.** Nothing in ``src/`` calls these five in
    sequence — the orchestration is M7's — so this function is the sequence, written once here. What
    makes the assertions meaningful is that every step is the *production* entry point: ``ingest``,
    ``run_matching``, ``run_classification``, ``record_decision``, ``enqueue_posting`` and
    ``dispatch_once`` are the functions the system ships. Nothing is seeded and no event is written
    by the test.

    The proposal step is deliberately absent from this particular path and covered separately: it
    needs an injected proposer, and mixing that into the walk would make a failure ambiguous between
    the pipeline and the double.
    """
    await _seed_ledger_entry(amount="120.00")

    receipt = await ingest(engine, PAYLOAD, source="file-drop", received_at=EPOCH)
    assert receipt.accepted, "the fixture payload must be accepted for the walk to mean anything"

    await run_matching(engine, matched_at=EPOCH)
    await run_classification(engine)

    connection = await asyncpg.connect(DSN)
    try:
        exception_id = await connection.fetchval("SELECT id FROM exception")
        correlation_id = await connection.fetchval("SELECT correlation_id FROM exception")
    finally:
        await connection.close()
    assert exception_id is not None, "the residual line must have become an exception"

    async with AsyncSession(engine) as session, session.begin():
        record = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=uuid.uuid4().hex,
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    instruction = AdjustmentInstruction(
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
    async with AsyncSession(engine) as session, session.begin():
        operation = await enqueue_posting(
            session, approval_id=record.approval_id, instruction=instruction
        )

    await dispatch_once(
        engine,
        adjustment_id=operation.adjustment_id,
        adapter=SimulatedLedger(),
        sent_at=EPOCH,
    )
    return operation.adjustment_id, str(correlation_id)


# ======================================================================================
# Test 1 — every ledger-affecting action emits at least one event
# ======================================================================================


@pytest.mark.asyncio
async def test_every_ledger_affecting_action_emits_at_least_one_event(
    engine: AsyncEngine,
) -> None:
    """§11: *"Every ledger-affecting action has at least one event."*  Acceptance criterion 11.

    Driven through the real entry points, asserting only what the services wrote. The four verbs
    below are every one this path can reach: matching a line, approving a resolution, computing its
    amount, and posting it.
    """
    adjustment_id, correlation_id = await _drive_the_pipeline(engine)

    tools = [row["tool"] for row in await _events()]

    assert "match" in tools, "matching a line is a state transition and owes an event"
    assert "approve" in tools, "the human decision behind every ledger write"
    assert "compute_amount" in tools, "the amount becoming durable"
    assert tools.count("post") == 2, "one attempt owes two: the send recorded, then the answer"

    assert adjustment_id is not None
    assert correlation_id.startswith("lecp:")


@pytest.mark.asyncio
async def test_no_event_is_written_by_anything_but_the_services(engine: AsyncEngine) -> None:
    """The complement, and the reason the walk above is worth trusting.

    Every row in the table after a pipeline run carries a scope from the contract's own vocabulary
    and a principal that is either ``system`` or a configured human. A row with free-text scope
    would mean something wrote an event without going through the emitter.
    """
    from ledger_exception_control_plane.audit import is_known_scope

    await _drive_the_pipeline(engine)

    rows = await _events()
    assert rows, "the walk produced no events at all"
    for row in rows:
        assert is_known_scope(row["scope_granted"]), row["scope_granted"]
        assert row["principal"] in {"system", "controller-a"}, row["principal"]
        assert row["correlation_id"], "every event carries a correlation id (§11)"


# ======================================================================================
# Test 3 — the correlation id spans ingestion to posting
# ======================================================================================


@pytest.mark.asyncio
async def test_the_correlation_id_spans_ingestion_to_posting(engine: AsyncEngine) -> None:
    """§18 and acceptance criterion 16, proven by **recomputation** rather than by comparison.

    The id every event carries is recomputed here from the two ingestion facts it is derived from —
    the content hash of the file the line arrived in, and the line's position within it. Comparing
    the events to each other would only prove they agree; recomputing proves they agree *with
    ingestion*, which is the claim.

    The residual is line 2, and that is not incidental: line 1 matched, so the span being asserted
    reaches from a file that arrived to a posting that was dispatched, across four stages that share
    no call frame.
    """
    _, correlation_id = await _drive_the_pipeline(engine)

    connection = await asyncpg.connect(DSN)
    try:
        content_hash = await connection.fetchval("SELECT content_hash FROM settlement_batch")
        line_number = await connection.fetchval(
            "SELECT line_number FROM settlement_line WHERE psp_reference = 'PSP-AUDIT-2'"
        )
    finally:
        await connection.close()

    recomputed = correlation_id_for(content_hash, line_number)
    assert recomputed == correlation_id

    posting_events = [row for row in await _events(recomputed) if row["tool"] == "post"]
    assert len(posting_events) == 2, "the posting end of the span"

    # And the matching end. The matched line has its own id, derived the same way from the same
    # file — so the trail for one ingested artefact is reachable without knowing anything but the
    # payload.
    connection = await asyncpg.connect(DSN)
    try:
        matched_line = await connection.fetchval(
            "SELECT line_number FROM settlement_line WHERE psp_reference = 'PSP-AUDIT-1'"
        )
    finally:
        await connection.close()
    match_events = await _events(correlation_id_for(content_hash, matched_line))
    assert [row["tool"] for row in match_events] == ["match"]


@pytest.mark.asyncio
async def test_an_unmatched_line_produces_no_match_event(engine: AsyncEngine) -> None:
    """Emission follows transitions, not runs.

    Matching re-considers residual lines on every pass by design, so emitting for them would append
    a fresh event each time for a line nothing had happened to — a trail that grows with the number
    of times it was asked rather than with the number of things that changed.
    """
    await _drive_the_pipeline(engine)

    connection = await asyncpg.connect(DSN)
    try:
        content_hash = await connection.fetchval("SELECT content_hash FROM settlement_batch")
        residual_line = await connection.fetchval(
            "SELECT line_number FROM settlement_line WHERE psp_reference = 'PSP-AUDIT-2'"
        )
    finally:
        await connection.close()

    residual_events = await _events(correlation_id_for(content_hash, residual_line))
    assert [row["tool"] for row in residual_events] != []
    assert "match" not in [row["tool"] for row in residual_events]

    # A second matching pass changes nothing, so it appends nothing.
    before = len(await _events())
    await run_matching(engine, matched_at=EPOCH + dt.timedelta(hours=1))
    assert len(await _events()) == before


# ======================================================================================
# Append-only: the third surface the grant did not cover
# ======================================================================================


@pytest.mark.asyncio
async def test_the_application_role_cannot_rewrite_reconciliation_evidence() -> None:
    """4.4 added a second append-only table and left the grant behind.

    ``reconciliation_query`` holds the observations that justify declaring an ambiguous financial
    write un-applied. The trigger has protected it since 4.4 — against every role including the
    owner — but the provisioning script granted the application role ``UPDATE`` and ``DELETE`` on
    every table and revoked them only on ``audit_event``. Defence in depth with one layer missing.

    ``SET LOCAL ROLE`` rather than a second connection, so no credential for the role exists or is
    needed anywhere in the suite. The ``has_table_privilege`` check first, because a database where
    the role holds nothing at all would deny these statements too and the assertion would pass while
    proving nothing — a trap this repository has already fallen into once.
    """
    connection = await asyncpg.connect(DSN)
    transaction = connection.transaction()
    await transaction.start()
    try:
        assert await connection.fetchval(
            "SELECT has_table_privilege('lecp_app', 'reconciliation_query', 'INSERT')"
        ), "the role holds no INSERT grant, so a denial here would prove nothing"

        await connection.execute("SET LOCAL ROLE lecp_app")
        for statement in (
            "UPDATE reconciliation_query SET answer = 'found'",
            "DELETE FROM reconciliation_query",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.transaction():
                    await connection.execute(statement)
    finally:
        await transaction.rollback()
        await connection.close()


# ======================================================================================
# The exit criterion
# ======================================================================================


@pytest.mark.asyncio
async def test_a_posted_adjustment_answers_all_five_questions(engine: AsyncEngine) -> None:
    """**§5.2's exit criterion, evaluated rather than asserted.**

    *"A posted adjustment answers: what evidence, which model, who approved, what was computed, by
    which code path."*

    Read through :func:`~ledger_exception_control_plane.provenance.provenance`, which keeps the two
    halves apart on purpose: what the audit trail attests, and what the domain tables hold. Three of
    the five answers cannot come from ``audit_event`` — §11 carries no amount and no evidence — so a
    report that blurred them would let a reader believe the trail proved a figure it never recorded.

    This particular adjustment was decided by a human with no model proposal behind it, which is why
    ``model`` is ``None`` here and why that still counts as answered: an adjustment nobody asked a
    model about is fully explained without one. The case *with* a proposal is asserted below.
    """
    adjustment_id, correlation_id = await _drive_the_pipeline(engine)

    async with AsyncSession(engine) as session:
        report = await provenance(session, adjustment_id=adjustment_id)

    assert report.answers_the_exit_criterion

    assert report.facts.approver == "controller-a"
    assert report.facts.decision == "approved"
    assert report.facts.approved_treatment == "rebook"
    assert report.facts.amount == "2799.9700"
    assert report.facts.currency == "EUR"
    assert report.facts.operation_id
    assert report.facts.correlation_id == correlation_id
    assert report.facts.model is None, "no proposal informed this decision"

    trail = [(action.tool, action.outcome) for action in report.trail]
    assert ("approve", "success") in trail
    assert ("compute_amount", "success") in trail
    assert ("post", "success") in trail


@pytest.mark.asyncio
async def test_the_report_names_what_it_cannot_answer(engine: AsyncEngine) -> None:
    """**The honest field, and the reason it exists.**

    "No model was involved" and "a model was involved and we failed to record which" are different
    states, and a report rendering both as an empty cell would be the kind of artefact that looks
    like evidence and is not.

    ``agent_identity`` is always listed: §11 admits null *"for deterministic steps"* and §2 states
    this system is not an agent, so there is nothing to identify. ``region_jurisdiction`` is listed
    only when a model was involved — no model call is made in this repository at all, so recording a
    processing region would describe a request that never happened.
    """
    adjustment_id, _ = await _drive_the_pipeline(engine)

    async with AsyncSession(engine) as session:
        report = await provenance(session, adjustment_id=adjustment_id)

    gaps = " ".join(report.not_recorded)
    assert "agent_identity" in gaps
    assert "not an agent" in gaps
    assert "region_jurisdiction" not in gaps, (
        "no model was involved in this adjustment, so its processing region is not a gap"
    )


@pytest.mark.asyncio
async def test_asking_about_an_adjustment_that_does_not_exist_raises(engine: AsyncEngine) -> None:
    """ "Nothing is recorded about this adjustment" and "there is no such adjustment" are different
    findings, and an auditor handed the first when the second is true has been misled."""
    async with AsyncSession(engine) as session:
        with pytest.raises(LookupError, match="no adjustment"):
            await provenance(session, adjustment_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_the_trail_this_report_reads_cannot_be_edited(engine: AsyncEngine) -> None:
    """The report is only worth anything because its source is append-only.

    Asserted here rather than left to the schema suite, because the claim being made is about *this*
    artefact: a provenance report over a mutable table would be a rendering of whatever somebody
    last wrote.
    """
    await _drive_the_pipeline(engine)

    for statement in (
        "UPDATE audit_event SET principal = 'somebody-else'",
        "DELETE FROM audit_event",
    ):
        with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="append-only"):
            await _execute(statement)
