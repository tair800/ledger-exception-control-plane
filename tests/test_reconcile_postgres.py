"""M4.4 against real PostgreSQL — §13.5's capability branch, end to end.

Everything here is about an operation whose outcome nobody knows, and almost none of it can be
established without a database: the transitions are rows, the monotonicity is triggers, the count of
consecutive negative answers is derived from appended evidence, and the interlock spans four tables.

**Three capability configurations, because the correct behaviour differs by capability.** §13.6:
*"a suite that tests only the strong adapter proves only the easy case"*. They are:

===========================  ==================================  ===============================
Configuration                Adapter                             §13.5 branch
===========================  ==================================  ===============================
``ENFORCES_KEY`` + query     ``SimulatedLedger`` (default)       reconcile by querying
``ENFORCES_KEY``, no query   ``SimulatedLedger``, query declared bounded re-send, then recovery
                             ``NONE``
``NONE`` / ``NONE``          ``NonIdempotentLedger``             manual recovery, no re-send
===========================  ==================================  ===============================

The third adapter **genuinely double-books**: its applied-count rises with every post. That is what
makes an assertion of "applied exactly once" against it a statement about our restraint rather than
about the double's forgiveness — and it is why the ``NONE``/``NONE`` assertions here are the ones
worth reading.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_reconcile_postgres.py -m integration
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import decimal
import json
import os
import pathlib
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.api import create_app
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.base import MONEY_QUANTUM
from ledger_exception_control_plane.db.control import (
    ApprovalDecision,
    RecoveryItem,
    RecoveryResolution,
    TreatmentCode,
)
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ledger import (
    Eventual,
    IdempotencyMode,
    Indeterminate,
    LedgerAdapterCapabilities,
    Linearizable,
    NonIdempotentLedger,
    NotFound,
    PostingInstruction,
    PostingQueryMode,
    QueryOutcome,
    SimulatedLedger,
    Unknown,
    capabilities_for,
)
from ledger_exception_control_plane.money import DEMO_LEDGER_CONTEXT, AdjustmentInstruction
from ledger_exception_control_plane.money.calculator import ROUNDING
from ledger_exception_control_plane.operations import (
    ReconciliationPolicy,
    RecoveryReason,
    RecoveryRefusal,
    RecoveryRefusedError,
    Resolution,
    dispatch_once,
    enqueue_posting,
    open_items,
    reconcile_once,
    resolve_item,
    stale_items,
)
from ledger_exception_control_plane.operations.approval import (
    ApprovalRefusedError,
    RefusalReason,
    record_decision,
)
from ledger_exception_control_plane.security import Principal, Role, token_fingerprint

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
REBOOK_ACCOUNT = "4100"

#: Long enough that "the windows elapsed" is never accidentally true.
INFLIGHT = dt.timedelta(seconds=30)
AFTER_WINDOWS = EPOCH + dt.timedelta(minutes=5)

CONTROLLER_TOKEN = "controller-token-for-tests-0123"
OPERATOR_TOKEN = "operator-token-for-tests-0123"
ANALYST_TOKEN = "analyst-token-for-tests-0123"

REGISTRY = json.dumps(
    {
        "controller-a": {
            "role": "controller",
            "token_sha256": token_fingerprint(CONTROLLER_TOKEN),
        },
        "operator-a": {"role": "operator", "token_sha256": token_fingerprint(OPERATOR_TOKEN)},
        "analyst-a": {"role": "analyst", "token_sha256": token_fingerprint(ANALYST_TOKEN)},
    }
)

OPERATOR = Principal("operator-a", Role.OPERATOR)
CONTROLLER = Principal("controller-a", Role.CONTROLLER)


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN), principals=REGISTRY)


# ======================================================================================
# The three configurations
# ======================================================================================


def _queryable(**kwargs: Any) -> SimulatedLedger:
    """Configuration 1: enforces the key *and* answers by operation identifier."""
    return SimulatedLedger(
        capabilities=LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.ENFORCES_KEY,
            idempotency_window=dt.timedelta(days=1),
            posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
            query_consistency=Linearizable(),
            max_inflight_window=INFLIGHT,
        ),
        **kwargs,
    )


def _resendable(*, window: dt.timedelta = dt.timedelta(hours=1), **kwargs: Any) -> SimulatedLedger:
    """Configuration 2: enforces the key and cannot be asked anything.

    The query capability is declared ``NONE`` rather than the adapter being made unqueryable,
    because §13.4 requires the branch to be chosen from *declared* data. The method still exists;
    the declaration is what closes that route, and a test below proves the branch respects the
    declaration rather than the method.
    """
    return SimulatedLedger(
        capabilities=LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.ENFORCES_KEY,
            idempotency_window=window,
            posting_identity_query=PostingQueryMode.NONE,
            query_consistency=Linearizable(),
            max_inflight_window=INFLIGHT,
        ),
        **kwargs,
    )


def _weak(**kwargs: Any) -> NonIdempotentLedger:
    """Configuration 3: ``idempotency=NONE, posting_identity_query=NONE`` — and it double-books."""
    return NonIdempotentLedger(**kwargs)


def _lost_response(_operation_id: str, _instruction: PostingInstruction) -> Unknown:
    """The answer never came back. What that means depends on which adapter it is given to.

    :class:`SimulatedLedger` consults its responder **before** applying, so here it models a request
    that did not reach the books — a genuine ambiguity with nothing posted.
    :class:`NonIdempotentLedger` consults its responder **after**, so there it models §19.1 exactly:
    the posting applied and the response was lost. Both are real cases and the assertions differ,
    which is why the adapters differ rather than one flag being passed to a shared double.
    """
    return Unknown(detail="read timeout after request was sent")


def _lost_then_lands() -> Any:
    """The first send is ambiguous; a later call to the same ledger commits normally.

    §14's row *"reconciliation returns NotFound, posting later appears"* — a request that was in
    flight when we gave up waiting and committed afterwards. It cannot be expressed by a constant
    responder against :class:`SimulatedLedger`, because that adapter short-circuits before applying
    and would leave the books empty forever; returning ``None`` on the second call lets the ledger
    behave normally, which is what "it landed after all" means.
    """
    calls = {"n": 0}

    def responder(_operation_id: str, _instruction: PostingInstruction) -> Unknown | None:
        calls["n"] += 1
        return Unknown(detail="read timeout after request was sent") if calls["n"] == 1 else None

    return responder


# ======================================================================================
# Fixtures
# ======================================================================================


def _clear_audit_trail() -> None:
    """Empty ``audit_event``, suspending the trigger that makes it append-only.

    **This module is the only one that writes verbs an earlier schema cannot express.** The 4.4
    migration's own downgrade *refuses* to re-narrow the audit vocabulary while `reconcile` or
    `recover` events exist — deliberately, because the table is append-only and the alternative
    would be a migration deciding on an operator's behalf to destroy history. That refusal is
    correct and it means this module has to leave the database as it found it, or the next
    integration module's ``downgrade base`` would meet a refusal it did not cause.

    Called before the downgrade as well as after the module, so a run killed part-way does not leave
    the next one stuck. ``assert_target_is_disposable`` has already refused to run against anything
    but a throwaway database.
    """

    async def clear() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            exists = await connection.fetchval("SELECT to_regclass('public.audit_event')")
            if exists is None:
                return
            await connection.execute(
                "ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only_row"
            )
            await connection.execute("DELETE FROM audit_event")
            await connection.execute(
                "ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only_row"
            )
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
    yield
    _clear_audit_trail()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings())) as test_client:
        yield test_client


@pytest_asyncio.fixture(autouse=True)
async def clean_slate() -> AsyncIterator[None]:
    await _wipe()
    yield


#: Tables the harness clears by suspending an append-only trigger it also tests.
#:
#: Stated here rather than buried in the wipe, because suspending a control in a test harness is
#: exactly the move that quietly disarms one. Both tables refuse ``DELETE`` and ``TRUNCATE`` to
#: every role including the owner, which is the property ``test_the_query_record_is_append_only``
#: and its audit-trail counterpart assert directly. The harness is the one caller entitled to say
#: "this database is disposable" — ``assert_target_is_disposable`` above has already refused to run
#: against anything else.
_APPEND_ONLY = {
    "audit_event": ("audit_event_append_only_row",),
    "reconciliation_query": ("reconciliation_query_append_only_row",),
}


async def _wipe() -> None:
    connection = await asyncpg.connect(DSN)
    try:
        for table, triggers in _APPEND_ONLY.items():
            for trigger in triggers:
                await connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            await connection.execute(f"DELETE FROM {table}")
            for trigger in triggers:
                await connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")

        for table in (
            "recovery_queue",
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


async def _seed_residual(*, marker: str) -> uuid.UUID:
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
    finally:
        await connection.close()
    return exception_id


async def _seed_approval(
    exception_id: uuid.UUID, *, resolution_version: int = 1, principal: str = "controller-a"
) -> uuid.UUID:
    approval_id = uuid.uuid4()
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, approval_token, decided_at)"
            " VALUES ($1, $2, $3, 'approved', 'rebook', $4, $5, $6)",
            approval_id,
            exception_id,
            resolution_version,
            principal,
            str(approval_id),
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


@dataclasses.dataclass(frozen=True, slots=True)
class Ambiguous:
    """One operation that has been sent and whose outcome nobody knows."""

    exception_id: uuid.UUID
    approval_id: uuid.UUID
    adjustment_id: uuid.UUID
    operation_id: str
    instruction: PostingInstruction


async def _dispatched_into_ambiguity(
    engine: AsyncEngine, adapter: Any, *, marker: str
) -> Ambiguous:
    """Seed, approve, enqueue and send once — with the answer lost.

    Returns the whole handle rather than an id, because every assertion below needs at least two of
    these and reconstructing them per test is where a test starts testing its own helper.
    """
    exception_id = await _seed_residual(marker=marker)
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )

    result = await dispatch_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=EPOCH
    )
    assert isinstance(result.outcome, Unknown), "the fixture must produce a genuine ambiguity"

    return Ambiguous(
        exception_id=exception_id,
        approval_id=approval_id,
        adjustment_id=record.adjustment_id,
        operation_id=record.identity.operation_id,
        instruction=PostingInstruction(
            adjustment_id=record.adjustment_id,
            amount=decimal.Decimal("2799.97"),
            currency="EUR",
            account_code=REBOOK_ACCOUNT,
            period="2026-06",
        ),
    )


async def _rows(table: str, **where: Any) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(DSN)
    try:
        if not where:
            return await connection.fetch(f"SELECT * FROM {table}")
        clause = " AND ".join(f"{col} = ${i + 1}" for i, col in enumerate(where))
        return await connection.fetch(f"SELECT * FROM {table} WHERE {clause}", *where.values())
    finally:
        await connection.close()


async def _one(table: str, **where: Any) -> asyncpg.Record:
    rows = await _rows(table, **where)
    assert len(rows) == 1, f"expected exactly one {table} row, found {len(rows)}"
    return rows[0]


async def _execute(sql: str, *args: Any) -> None:
    connection = await asyncpg.connect(DSN)
    try:
        await connection.execute(sql, *args)
    finally:
        await connection.close()


# ======================================================================================
# The state itself — UNKNOWN is first-class and is never silently coerced
# ======================================================================================


@pytest.mark.asyncio
async def test_an_ambiguous_dispatch_is_recorded_as_unknown_and_never_settles(
    engine: AsyncEngine,
) -> None:
    """**The property everything else rests on.**

    ``settled_requires_terminal_outcome`` admits only ``confirmed`` and ``rejected`` on a settled
    row, so an ``unknown`` outcome cannot be filed as finished even by a caller that tried. The
    dispatch is over, and the operation is not.
    """
    case = await _dispatched_into_ambiguity(
        engine, _queryable(responder=_lost_response), marker="u"
    )

    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    attempt = await _one("posting_attempt", adjustment_id=case.adjustment_id)

    assert intent["last_outcome"] == "unknown"
    assert intent["state"] == "pending", "an unknown outcome is not a finished dispatch"
    assert attempt["outcome"] == "unknown"
    assert attempt["posting_ref"] is None
    assert (await _one("adjustment", id=case.adjustment_id))["posting_ref"] is None


@pytest.mark.asyncio
async def test_the_attempt_records_where_it_was_sent(engine: AsyncEngine) -> None:
    """The evidence §13.5's scope bound is checked against. Recorded at send time, not inferred."""
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="endpoint")

    attempt = await _one("posting_attempt", adjustment_id=case.adjustment_id)
    assert attempt["endpoint"] == adapter.endpoint


@pytest.mark.asyncio
async def test_an_unambiguous_operation_is_left_alone(engine: AsyncEngine) -> None:
    """Reconciliation is not a background tidy-up. A confirmed posting is not its business."""
    exception_id = await _seed_residual(marker="clean")
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )
    adapter = _queryable()
    await dispatch_once(engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=EPOCH)

    report = await reconcile_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    assert report.resolution is Resolution.NOT_AMBIGUOUS
    assert await _rows("reconciliation_query") == []


# ======================================================================================
# Configuration 1 — reconcile by querying (§13.5 clause 4)
# ======================================================================================


@pytest.mark.asyncio
async def test_a_positive_hit_resolves_to_confirmed_immediately(engine: AsyncEngine) -> None:
    """§13.5: *"Resolve to `CONFIRMED` immediately. A positive hit is trustworthy."*

    The set-up is §14's row *"reconciliation returns NotFound, posting later appears"* seen from
    the other side: the request was in flight when we gave up waiting, and it has since committed.
    The reconciliation finds it, and the reference recorded is the ledger's own.
    """
    adapter = _queryable(responder=_lost_then_lands())
    case = await _dispatched_into_ambiguity(engine, adapter, marker="found")
    # The request was still in flight when we gave up waiting, and it commits now.
    await adapter.post(case.operation_id, case.instruction)

    report = await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=EPOCH + dt.timedelta(1)
    )

    assert report.resolution is Resolution.CONFIRMED
    applied = adapter.applied(case.operation_id)
    assert applied is not None
    assert (await _one("adjustment", id=case.adjustment_id))["posting_ref"] == applied.posting_ref
    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    assert (intent["last_outcome"], intent["state"]) == ("confirmed", "settled")
    assert adapter.applied_count(case.operation_id) == 1


@pytest.mark.asyncio
async def test_the_attempt_that_saw_the_ambiguity_keeps_its_outcome_forever(
    engine: AsyncEngine,
) -> None:
    """**§13.5 clause 6, made structural rather than promised.**

    *"`UNKNOWN` is never overwritten in place; resolution is an appended transition."* The outbox is
    a current-state pointer and moves; the attempt row is evidence of what one send observed, and
    that never changes. An auditor reading the attempt history still sees that a send came back
    ambiguous, which is the fact a resolution must not erase.
    """
    adapter = _queryable(responder=_lost_then_lands())
    case = await _dispatched_into_ambiguity(engine, adapter, marker="append")
    await adapter.post(case.operation_id, case.instruction)

    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=EPOCH + dt.timedelta(1)
    )

    attempt = await _one("posting_attempt", adjustment_id=case.adjustment_id)
    assert attempt["outcome"] == "unknown"
    assert attempt["posting_ref"] is None


@pytest.mark.asyncio
async def test_a_notfound_alone_never_resolves_to_rejected(engine: AsyncEngine) -> None:
    """**The single most dangerous inference in the system, refused.**

    §13.5: a `NotFound` means *"not visible to this query yet"*, which is not *"will never be
    applied"*. Here neither window has elapsed, so even the first negative answer changes nothing.
    """
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="nf1")

    report = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(seconds=1),
    )

    assert report.resolution is Resolution.UNRESOLVED
    assert report.consecutive_not_found == 1
    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    assert intent["last_outcome"] == "unknown", "still ambiguous, and still not settled"


@pytest.mark.asyncio
async def test_the_windows_must_elapse_before_enough_negatives_are_enough(
    engine: AsyncEngine,
) -> None:
    """Three negatives inside the in-flight window resolve nothing.

    The count alone is not the condition — §13.5 requires the count **and** both windows, and this
    is the half a "count three failures then give up" implementation would get wrong.
    """
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="early")

    for second in (1, 2, 3):
        report = await reconcile_once(
            engine,
            adjustment_id=case.adjustment_id,
            adapter=adapter,
            now=EPOCH + dt.timedelta(seconds=second),
        )
        assert report.resolution is Resolution.UNRESOLVED

    assert report.consecutive_not_found == 3, "the evidence is there; the clock is not"
    assert (await _one("outbox", adjustment_id=case.adjustment_id))["last_outcome"] == "unknown"


@pytest.mark.asyncio
async def test_n_consecutive_negatives_after_both_windows_resolve_to_rejected(
    engine: AsyncEngine,
) -> None:
    """The permitted negative resolution, with every condition satisfied and none assumed."""
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="rejected")

    reports = [
        await reconcile_once(
            engine,
            adjustment_id=case.adjustment_id,
            adapter=adapter,
            now=AFTER_WINDOWS + dt.timedelta(seconds=index),
        )
        for index in range(3)
    ]

    assert [r.resolution for r in reports] == [
        Resolution.UNRESOLVED,
        Resolution.UNRESOLVED,
        Resolution.REJECTED,
    ]
    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    assert (intent["last_outcome"], intent["state"]) == ("rejected", "settled")
    assert adapter.applied_count(case.operation_id) == 0, "nothing was ever posted"


@pytest.mark.asyncio
async def test_a_posting_appearing_after_a_notfound_still_resolves_correctly(
    engine: AsyncEngine,
) -> None:
    """**§14's named failure: *"reconciliation returns NotFound, posting later appears"*.**

    This is the scenario the whole windows rule exists for. Had the first `NotFound` been treated as
    a negative answer, the operation would have been declared rejected and a correction posted —
    against a ledger that had already applied it.
    """
    adapter = _queryable(responder=_lost_then_lands())
    case = await _dispatched_into_ambiguity(engine, adapter, marker="late")

    first = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(seconds=1),
    )
    assert first.resolution is Resolution.UNRESOLVED

    # The posting becomes visible. It was in flight all along.
    await adapter.post(case.operation_id, case.instruction)

    second = await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    assert second.resolution is Resolution.CONFIRMED
    assert adapter.applied_count(case.operation_id) == 1
    answers = [row["answer"] for row in await _rows("reconciliation_query")]
    assert sorted(answers) == ["found", "not_found"], "both observations are on the record"


@pytest.mark.asyncio
async def test_an_indeterminate_answer_never_counts_toward_the_requirement(
    engine: AsyncEngine,
) -> None:
    """§13.5: *"`Indeterminate` … never counts toward the consecutive-`NotFound` requirement."*

    Scripted so an indeterminate answer lands in the middle of a run of negatives. If it counted,
    the fourth query would reach three and resolve to ``REJECTED``; because it does not, and because
    it *breaks* the run, the count restarts and the fifth query is the first to reach three.

    A failed query is not a negative answer, and a system that let a run of timeouts accumulate into
    "the posting is not there" would be inferring a financial fact from its own connectivity.
    """
    script = iter([None, Indeterminate(detail="gateway timeout"), None, None, None])

    def query_responder(_operation_id: str) -> QueryOutcome | None:
        return next(script, None)

    adapter = _queryable(responder=_lost_response, query_responder=query_responder)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="indet")

    reports = [
        await reconcile_once(
            engine,
            adjustment_id=case.adjustment_id,
            adapter=adapter,
            now=AFTER_WINDOWS + dt.timedelta(seconds=index),
        )
        for index in range(5)
    ]

    assert [r.answer.value if r.answer else None for r in reports] == [
        "not_found",
        "indeterminate",
        "not_found",
        "not_found",
        "not_found",
    ]
    assert [r.consecutive_not_found for r in reports] == [1, 0, 1, 2, 3]
    assert [r.resolution for r in reports[:4]] == [Resolution.UNRESOLVED] * 4
    assert reports[4].resolution is Resolution.REJECTED


@pytest.mark.asyncio
async def test_every_query_and_the_windows_it_was_judged_against_are_recorded(
    engine: AsyncEngine,
) -> None:
    """§13.5: *"The windows used, the query results observed and the resolution reached are all
    recorded … so an auditor can reconstruct why a resolution was considered safe."*

    The windows are stamped per row rather than looked up at resolution time, so a later revision to
    a provider's documented bound cannot silently restate yesterday's reasoning in today's terms.
    """
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="evidence")

    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    row = await _one("reconciliation_query", adjustment_id=case.adjustment_id)
    assert row["query_no"] == 1
    assert row["answer"] == "not_found"
    assert row["operation_id"] == case.operation_id
    assert row["posting_ref"] is None
    assert row["visibility_bound"] == dt.timedelta(0), "LINEARIZABLE, recorded as zero"
    assert row["max_inflight_window"] == INFLIGHT


@pytest.mark.asyncio
async def test_reconciliation_is_bounded_and_lands_in_recovery_rather_than_looping(
    engine: AsyncEngine,
) -> None:
    """§13.5: reconciliation is *"bounded and scheduled, never an unbounded retry loop"*, and
    *"on bound exhaustion … the operation routes to manual recovery"*.

    Driven with an adapter that can only ever answer ``Indeterminate``, which is the case that never
    resolves on its own — the count can never rise, so without a bound this would query forever.
    """

    def always_indeterminate(_operation_id: str) -> QueryOutcome:
        return Indeterminate(detail="the ledger's query API is down")

    adapter = _queryable(responder=_lost_response, query_responder=always_indeterminate)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="exhaust")
    policy = ReconciliationPolicy(consecutive_not_found=2, max_queries=3)

    resolutions = [
        (
            await reconcile_once(
                engine,
                adjustment_id=case.adjustment_id,
                adapter=adapter,
                now=AFTER_WINDOWS + dt.timedelta(seconds=index),
                policy=policy,
            )
        ).resolution
        for index in range(4)
    ]

    assert resolutions == [
        Resolution.UNRESOLVED,
        Resolution.UNRESOLVED,
        Resolution.ROUTED_TO_RECOVERY,
        Resolution.ALREADY_IN_RECOVERY,
    ]
    assert len(await _rows("reconciliation_query", adjustment_id=case.adjustment_id)) == 3, (
        "the fourth pass asked nothing: the bound is on queries, not on passes"
    )
    item = await _one("recovery_queue", adjustment_id=case.adjustment_id)
    assert item["reason"] == RecoveryReason.RECONCILIATION_EXHAUSTED.value


# ======================================================================================
# Configuration 2 — a bounded re-send under ENFORCES_KEY (§13.5 clause 3)
# ======================================================================================


@pytest.mark.asyncio
async def test_a_resend_inside_both_bounds_is_made_and_the_ledger_suppresses_it(
    engine: AsyncEngine,
) -> None:
    """The permitted automatic path, and the only one that touches the ledger again.

    The posting was applied and the response lost, so the re-send is a duplicate — and the whole
    justification for making it is that this adapter's ``ENFORCES_KEY`` claim has been *proven* by
    the conformance suite. The applied-count is the measurement that says the claim held.
    """
    adapter = _resendable(responder=_lost_then_lands())
    case = await _dispatched_into_ambiguity(engine, adapter, marker="resend")
    await adapter.post(case.operation_id, case.instruction)
    assert adapter.applied_count(case.operation_id) == 1

    report = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(minutes=1),
    )

    assert report.resolution is Resolution.RESENT
    assert adapter.applied_count(case.operation_id) == 1, "suppressed, not applied twice"
    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    assert (intent["last_outcome"], intent["state"]) == ("confirmed", "settled")


@pytest.mark.asyncio
async def test_a_resend_outside_the_idempotency_window_is_refused(engine: AsyncEngine) -> None:
    """§13.5: *"a re-send outside either bound is an ordinary duplicate write wearing an
    idempotency header"*, and outside the bounds *"the operation routes to manual recovery"*."""
    adapter = _resendable(window=dt.timedelta(minutes=10), responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="expired")
    posts_before = adapter.posts_received

    report = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(hours=2),
    )

    assert report.resolution is Resolution.ROUTED_TO_RECOVERY
    assert report.recovery_reason is RecoveryReason.RESEND_WINDOW_EXPIRED
    assert adapter.posts_received == posts_before, "nothing was sent"
    item = await _one("recovery_queue", adjustment_id=case.adjustment_id)
    assert "do not re-send" in item["evidence_procedure"].lower()


@pytest.mark.asyncio
async def test_a_resend_to_a_different_endpoint_is_refused(engine: AsyncEngine) -> None:
    """The scope half of the same clause, inside a window that is still open.

    A key enforced per endpoint says nothing about a different endpoint, so the window being live is
    not the question — and a system that checked only the window would send here.
    """
    adapter = _resendable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="scope")
    posts_before = adapter.posts_received

    report = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(minutes=1),
        target_endpoint="sim://moved/postings",
    )

    assert report.resolution is Resolution.ROUTED_TO_RECOVERY
    assert report.recovery_reason is RecoveryReason.RESEND_SCOPE_UNPROVEN
    assert adapter.posts_received == posts_before


@pytest.mark.asyncio
async def test_the_query_branch_is_chosen_by_declaration_not_by_the_method_existing(
    engine: AsyncEngine,
) -> None:
    """**§13.4: capability is declared data, never inferred from the adapter's shape.**

    This adapter *has* ``get_by_operation_id`` and declares ``posting_identity_query = NONE``. A
    branch that reached for the method because it was there would query a provider whose contract
    does not offer the answer — and would then act on whatever came back.
    """
    adapter = _resendable(responder=_lost_response)
    assert hasattr(adapter, "get_by_operation_id")
    assert not capabilities_for(adapter).queryable_by_operation_id

    case = await _dispatched_into_ambiguity(engine, adapter, marker="declared")
    report = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(minutes=1),
    )

    assert report.resolution is Resolution.RESENT
    assert await _rows("reconciliation_query") == [], "no query was asked; none was permitted"


@pytest.mark.asyncio
async def test_querying_is_preferred_over_resending_when_both_are_available(
    engine: AsyncEngine,
) -> None:
    """A read that can be wrong for free comes before an irreversible write that cannot.

    The default configuration declares both capabilities. §13.5 clause 4 says *"reconcile against
    the downstream system where possible"*, and the ordering is the safety property: an
    implementation that preferred the re-send would post again in every case where a query would
    have answered.
    """
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="prefer")
    posts_before = adapter.posts_received

    report = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(minutes=1),
    )

    assert report.resolution is Resolution.UNRESOLVED
    assert report.answer is not None
    assert adapter.posts_received == posts_before, "it asked; it did not send"


# ======================================================================================
# Configuration 3 — NONE/NONE: manual recovery, and no automatic re-send at all
# ======================================================================================


@pytest.mark.asyncio
async def test_no_automatic_resend_occurs_under_none_none(engine: AsyncEngine) -> None:
    """**The assertion this whole increment exists to be able to make.**

    The adapter genuinely double-books — its applied-count rises with every post — and the posting
    *was* applied before the response was lost. So an applied-count of 1 after reconciliation is a
    measurement of our restraint, not of the ledger's forgiveness. A system that retried the
    ambiguous write on the assumption it had failed would read 2 here.
    """
    adapter = _weak(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="weak")
    assert adapter.applied_count(case.operation_id) == 1, "the books moved; the answer was lost"

    report = await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    assert report.resolution is Resolution.ROUTED_TO_RECOVERY
    assert report.recovery_reason is RecoveryReason.NO_SUPPRESSION_OR_QUERY
    assert adapter.applied_count(case.operation_id) == 1
    assert adapter.posts_received == 1
    assert await _rows("reconciliation_query") == []


@pytest.mark.asyncio
async def test_repeated_reconciliation_under_none_none_never_escalates_into_a_send(
    engine: AsyncEngine,
) -> None:
    """Called ten times, because "it did not re-send" is only interesting if it keeps not doing it.

    A bound that leaked would show up as a second application on some later pass; a queue entry that
    duplicated would show up as ten recovery items for one operation.
    """
    adapter = _weak(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="weakloop")

    for index in range(10):
        await reconcile_once(
            engine,
            adjustment_id=case.adjustment_id,
            adapter=adapter,
            now=AFTER_WINDOWS + dt.timedelta(minutes=index),
        )

    assert adapter.applied_count(case.operation_id) == 1
    assert len(await _rows("recovery_queue", adjustment_id=case.adjustment_id)) == 1


@pytest.mark.asyncio
async def test_a_partially_applied_outcome_goes_straight_to_an_operator(
    engine: AsyncEngine,
) -> None:
    """§14 routes this to manual recovery and never retries it, **whatever the adapter can do**.

    Driven against the strongest configuration on purpose: a query answers "is it there", and for a
    posting whose legs disagree that is not a resolution. The check sits before every capability
    branch so no adapter's strength can route it into an automatic path.
    """
    from ledger_exception_control_plane.ledger import PartiallyApplied

    adapter = _queryable(
        responder=lambda _op, _i: PartiallyApplied(applied_legs=1, posting_refs=("SIM-leg-1",))
    )
    exception_id = await _seed_residual(marker="partial")
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )
    await dispatch_once(engine, adjustment_id=record.adjustment_id, adapter=adapter, sent_at=EPOCH)

    report = await reconcile_once(
        engine, adjustment_id=record.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    assert report.recovery_reason is RecoveryReason.PARTIALLY_APPLIED
    assert await _rows("reconciliation_query") == [], "it did not ask; §14 does not ask"


@pytest.mark.asyncio
async def test_a_crash_between_the_send_and_the_response_is_ambiguous_without_any_outcome(
    engine: AsyncEngine,
) -> None:
    """**The case a `last_outcome` check alone cannot see.**

    ``last_outcome`` is written when a dispatch *completes*. A crash mid-send leaves the column NULL
    and an ``in_flight`` attempt row — which §12.1.1 defines as ``UNKNOWN`` — so reconciliation must
    read the attempt rather than the pointer. Simulated by committing the write-ahead record and no
    outcome, which is exactly the durable state such a crash leaves behind.
    """
    exception_id = await _seed_residual(marker="crash")
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )
    await _execute(
        "INSERT INTO posting_attempt (id, adjustment_id, operation_id, attempt_no, sent_at,"
        " endpoint, state) VALUES ($1, $2, $3, 1, $4, 'sim://weak/postings', 'in_flight')",
        uuid.uuid4(),
        record.adjustment_id,
        record.identity.operation_id,
        EPOCH,
    )

    intent = await _one("outbox", adjustment_id=record.adjustment_id)
    assert intent["last_outcome"] is None, "nothing was ever recorded"

    report = await reconcile_once(
        engine, adjustment_id=record.adjustment_id, adapter=_weak(), now=AFTER_WINDOWS
    )

    assert report.resolution is Resolution.ROUTED_TO_RECOVERY


# ======================================================================================
# Monotonic transitions, enforced by the database
# ======================================================================================


@pytest.mark.asyncio
async def test_a_recorded_attempt_outcome_cannot_be_changed(engine: AsyncEngine) -> None:
    """Attempted directly against the table, because the point is that the *database* refuses.

    An application that never tries this is not evidence; a trigger that refuses it is.
    """
    case = await _dispatched_into_ambiguity(
        engine, _queryable(responder=_lost_response), marker="im"
    )

    with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="immutable"):
        await _execute(
            "UPDATE posting_attempt SET outcome = 'confirmed' WHERE adjustment_id = $1",
            case.adjustment_id,
        )


@pytest.mark.asyncio
async def test_a_confirmed_dispatch_cannot_be_moved_off_its_outcome(engine: AsyncEngine) -> None:
    """§13.5 clause 4: *"`CONFIRMED → anything` is not"* a permitted transition."""
    exception_id = await _seed_residual(marker="terminal")
    approval_id = await _seed_approval(exception_id)
    async with AsyncSession(engine) as session, session.begin():
        record = await enqueue_posting(
            session, approval_id=approval_id, instruction=_instruction(exception_id)
        )
    await dispatch_once(
        engine, adjustment_id=record.adjustment_id, adapter=_queryable(), sent_at=EPOCH
    )

    with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="terminal"):
        await _execute(
            "UPDATE outbox SET last_outcome = 'unknown' WHERE adjustment_id = $1",
            record.adjustment_id,
        )
    with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="reopened"):
        await _execute(
            "UPDATE outbox SET state = 'pending' WHERE adjustment_id = $1", record.adjustment_id
        )


@pytest.mark.asyncio
async def test_the_query_record_is_append_only(engine: AsyncEngine) -> None:
    """The count of consecutive negative answers is the safety argument for declaring an ambiguous
    financial write un-applied. An argument made of editable rows is not one."""
    adapter = _queryable(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="appendonly")
    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    for statement in (
        "UPDATE reconciliation_query SET answer = 'found'",
        "DELETE FROM reconciliation_query",
        "TRUNCATE reconciliation_query",
    ):
        with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="append-only"):
            await _execute(statement)


# ======================================================================================
# The supersession interlock (§12.1)
# ======================================================================================


@pytest.mark.asyncio
async def test_a_new_resolution_version_is_blocked_while_a_prior_one_is_ambiguous(
    engine: AsyncEngine,
) -> None:
    """§12.1: *"A new `resolution_version` may not be approved while a prior operation on the same
    exception is `IN_FLIGHT`, `UNKNOWN` or open in recovery."*

    One exception with two live resolutions is two authorised postings for one residual, and the
    second would be authorised precisely because nobody could say what the first did.
    """
    case = await _dispatched_into_ambiguity(engine, _weak(responder=_lost_response), marker="sup")

    async with AsyncSession(engine) as session:
        with pytest.raises(ApprovalRefusedError) as refused:
            await record_decision(
                session,
                exception_id=case.exception_id,
                resolution_version=2,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=uuid.uuid4().hex,
                now=AFTER_WINDOWS,
                treatment=TreatmentCode.REBOOK,
            )

    assert refused.value.reason is RefusalReason.SUPERSESSION_BLOCKED


@pytest.mark.asyncio
async def test_the_interlock_is_a_database_control_and_not_an_application_check(
    engine: AsyncEngine,
) -> None:
    """Written straight at the table, bypassing the service entirely.

    The service refuses first and with a better message; this proves which of the two is the
    control. A check in application code is one refactor from being skipped, and this one sits
    between an ambiguous financial write and a second authorisation to post.
    """
    case = await _dispatched_into_ambiguity(engine, _weak(responder=_lost_response), marker="trig")

    with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="in flight, ambiguous"):
        await _execute(
            "INSERT INTO approval (id, exception_id, resolution_version, decision,"
            " approved_treatment, principal, approval_token, decided_at)"
            " VALUES ($1, $2, 2, 'approved', 'rebook', 'controller-a', $3, $4)",
            uuid.uuid4(),
            case.exception_id,
            uuid.uuid4().hex,
            AFTER_WINDOWS,
        )


@pytest.mark.asyncio
async def test_an_open_recovery_item_blocks_supersession_and_a_closed_one_releases_it(
    engine: AsyncEngine,
) -> None:
    """**The one place the database defers to a human, and the trade is deliberate.**

    While an operator holds the question, the prior resolution is live and a second is refused.
    Once they have adjudicated it, it is not, and a correction may be approved. That release is what
    stops an unresolvable ambiguity becoming a dead end — and it is not free: after a
    ``RESOLVED_UNVERIFIED`` the original may in fact have applied, so superseding can still
    double-post. The design makes the judgement recorded and attributable, not correct, and this
    test pins both halves rather than only the reassuring one.
    """
    adapter = _weak(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="release")
    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )
    item = await _one("recovery_queue", adjustment_id=case.adjustment_id)

    async with AsyncSession(engine) as session:
        with pytest.raises(ApprovalRefusedError):
            await record_decision(
                session,
                exception_id=case.exception_id,
                resolution_version=2,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=uuid.uuid4().hex,
                now=AFTER_WINDOWS,
                treatment=TreatmentCode.REBOOK,
            )

    await resolve_item(
        engine,
        recovery_id=item["id"],
        principal=OPERATOR,
        resolution=RecoveryResolution.RESOLVED_UNVERIFIED,
        now=AFTER_WINDOWS,
    )

    async with AsyncSession(engine) as session, session.begin():
        record = await record_decision(
            session,
            exception_id=case.exception_id,
            resolution_version=2,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=uuid.uuid4().hex,
            now=AFTER_WINDOWS,
            treatment=TreatmentCode.REBOOK,
        )
    assert record.resolution_version == 2


# ======================================================================================
# The manual-recovery queue (§13.5 clause 5)
# ======================================================================================


async def _queued(engine: AsyncEngine, *, marker: str) -> tuple[Ambiguous, asyncpg.Record]:
    adapter = _weak(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker=marker)
    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )
    return case, await _one("recovery_queue", adjustment_id=case.adjustment_id)


@pytest.mark.asyncio
async def test_a_recovery_item_carries_a_procedure_an_operator_can_act_on(
    engine: AsyncEngine,
) -> None:
    """§13.5: *"A queue is not a control on its own, so the design specifies what the operator
    actually does."*"""
    case, item = await _queued(engine, marker="proc")

    assert case.operation_id in item["evidence_procedure"]
    assert "Inspect:" in item["evidence_procedure"]
    assert "Sufficient for confirmed_by_evidence:" in item["evidence_procedure"]
    assert "Sufficient for rejected_by_evidence:" in item["evidence_procedure"]
    assert item["approving_principal"] == "controller-a"
    assert item["sla_due_at"] > item["opened_at"]


@pytest.mark.asyncio
async def test_a_second_queue_entry_loses_to_the_index_without_harming_its_caller(
    engine: AsyncEngine,
) -> None:
    """**The race the pre-check cannot close, and the recovery that must not cost the caller.**

    Two reconciliation passes can both find no open item and both try to queue one; the partial
    unique index on open items is what makes the second lose. Driven by calling ``open_item``
    directly, past the pre-check, because that is the state a race produces and a read-then-write
    check cannot prevent.

    The second assertion is the one worth having: the losing insert is contained in a savepoint, so
    the caller's transaction survives and the row it wrote alongside is still there. Rolling the
    whole transaction back would have looked like correct error handling and would have discarded
    the resolution the caller had just decided.
    """
    from ledger_exception_control_plane.operations.recovery import open_item

    case, first = await _queued(engine, marker="race")

    async with AsyncSession(engine) as session, session.begin():
        marker_event = await open_item(
            session,
            adjustment_id=case.adjustment_id,
            reason=RecoveryReason.NO_SUPPRESSION_OR_QUERY,
            opened_at=AFTER_WINDOWS,
            sla=dt.timedelta(hours=24),
        )
        assert marker_event is None, "the second entry loses to the partial unique index"
        # The caller's transaction is still usable, which is the point of the savepoint.
        await session.execute(select(RecoveryItem).where(RecoveryItem.id == first["id"]))

    assert len(await _rows("recovery_queue", adjustment_id=case.adjustment_id)) == 1


@pytest.mark.asyncio
async def test_an_item_past_its_sla_is_reported_as_stale(engine: AsyncEngine) -> None:
    """§13.5: *"a stale `UNKNOWN` is an alertable condition"*. A query, not a hope."""
    case, item = await _queued(engine, marker="sla")

    async with AsyncSession(engine) as session:
        assert await stale_items(session, now=item["sla_due_at"] - dt.timedelta(minutes=1)) == []
        stale = await stale_items(session, now=item["sla_due_at"] + dt.timedelta(minutes=1))
        listed = await open_items(session, now=item["opened_at"])

    assert [row.adjustment_id for row in stale] == [case.adjustment_id]
    assert stale[0].overdue is True
    assert [row.overdue for row in listed] == [False]


@pytest.mark.asyncio
async def test_the_approver_may_not_judge_what_happened_to_their_own_posting(
    engine: AsyncEngine,
) -> None:
    """§13.5's segregation of duties, refused by the service **and** by a check constraint."""
    _, item = await _queued(engine, marker="sod")
    approver = Principal("controller-a", Role.OPERATOR)

    with pytest.raises(RecoveryRefusedError) as refused:
        await resolve_item(
            engine,
            recovery_id=item["id"],
            principal=approver,
            resolution=RecoveryResolution.REJECTED_BY_EVIDENCE,
            now=AFTER_WINDOWS,
        )
    assert refused.value.refusal is RecoveryRefusal.APPROVER_MAY_NOT_RESOLVE

    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="segregation_of_duties"):
        await _execute(
            "UPDATE recovery_queue SET state = 'resolved', resolution = 'rejected_by_evidence',"
            " resolved_by = 'controller-a', resolved_at = $1 WHERE id = $2",
            AFTER_WINDOWS,
            item["id"],
        )


@pytest.mark.asyncio
async def test_only_the_operator_role_may_work_the_queue(engine: AsyncEngine) -> None:
    """The other half of §16's separation: a controller who could unstick a posting could authorise
    one and then drive it."""
    _, item = await _queued(engine, marker="role")

    with pytest.raises(RecoveryRefusedError) as refused:
        await resolve_item(
            engine,
            recovery_id=item["id"],
            principal=Principal("someone-else", Role.CONTROLLER),
            resolution=RecoveryResolution.REJECTED_BY_EVIDENCE,
            now=AFTER_WINDOWS,
        )
    assert refused.value.refusal is RecoveryRefusal.ROLE_MAY_NOT_RECOVER


@pytest.mark.asyncio
async def test_a_verified_confirmation_settles_the_dispatch_and_records_the_reference(
    engine: AsyncEngine,
) -> None:
    case, item = await _queued(engine, marker="confirm")

    await resolve_item(
        engine,
        recovery_id=item["id"],
        principal=OPERATOR,
        resolution=RecoveryResolution.CONFIRMED_BY_EVIDENCE,
        now=AFTER_WINDOWS,
        posting_ref="LEDGER-SNAPSHOT-88213",
    )

    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    assert (intent["last_outcome"], intent["state"]) == ("confirmed", "settled")
    adjustment = await _one("adjustment", id=case.adjustment_id)
    assert adjustment["posting_ref"] == "LEDGER-SNAPSHOT-88213"


@pytest.mark.asyncio
async def test_a_confirmation_without_a_reference_is_refused(engine: AsyncEngine) -> None:
    """The reference *is* the evidence. A confirmation without one is an assertion."""
    _, item = await _queued(engine, marker="noref")

    with pytest.raises(RecoveryRefusedError) as refused:
        await resolve_item(
            engine,
            recovery_id=item["id"],
            principal=OPERATOR,
            resolution=RecoveryResolution.CONFIRMED_BY_EVIDENCE,
            now=AFTER_WINDOWS,
        )
    assert refused.value.refusal is RecoveryRefusal.CONFIRMED_WITHOUT_REFERENCE
    assert (await _one("recovery_queue", id=item["id"]))["state"] == "open"


@pytest.mark.asyncio
async def test_an_unverified_resolution_closes_the_item_and_settles_nothing(
    engine: AsyncEngine,
) -> None:
    """**The resolution that must not look like the other two.**

    §13.5 requires ``RESOLVED_UNVERIFIED`` to be *"visible to an auditor rather than
    indistinguishable from a verified one"*. Settling the dispatch would write ``confirmed`` or
    ``rejected`` as though the answer were known — there is no third terminal value meaning "a human
    judged without evidence", and inventing one would be the coercion §13.5 forbids wearing a new
    name. So the operation stays ambiguous, closed to every automatic path, with a name against it.
    """
    case, item = await _queued(engine, marker="unverified")

    await resolve_item(
        engine,
        recovery_id=item["id"],
        principal=OPERATOR,
        resolution=RecoveryResolution.RESOLVED_UNVERIFIED,
        now=AFTER_WINDOWS,
    )

    resolved = await _one("recovery_queue", id=item["id"])
    assert resolved["state"] == "resolved"
    assert resolved["resolution"] == "resolved_unverified"
    assert resolved["resolved_by"] == "operator-a"

    intent = await _one("outbox", adjustment_id=case.adjustment_id)
    assert (intent["last_outcome"], intent["state"]) == ("unknown", "pending")
    assert (await _one("adjustment", id=case.adjustment_id))["posting_ref"] is None


@pytest.mark.asyncio
async def test_an_item_is_judged_once(engine: AsyncEngine) -> None:
    """A second judgement would overwrite the first and the trail would show only the survivor."""
    _, item = await _queued(engine, marker="twice")
    await resolve_item(
        engine,
        recovery_id=item["id"],
        principal=OPERATOR,
        resolution=RecoveryResolution.REJECTED_BY_EVIDENCE,
        now=AFTER_WINDOWS,
    )

    with pytest.raises(RecoveryRefusedError) as refused:
        await resolve_item(
            engine,
            recovery_id=item["id"],
            principal=OPERATOR,
            resolution=RecoveryResolution.CONFIRMED_BY_EVIDENCE,
            now=AFTER_WINDOWS,
            posting_ref="LATE-DISCOVERY",
        )
    assert refused.value.refusal is RecoveryRefusal.ALREADY_RESOLVED


# ======================================================================================
# /recovery over HTTP
# ======================================================================================


@pytest.mark.asyncio
async def test_the_recovery_queue_is_readable_and_requires_authentication(
    engine: AsyncEngine, client: TestClient
) -> None:
    case, _ = await _queued(engine, marker="http")

    assert client.get("/api/v1/recovery").status_code == 401

    response = client.get("/api/v1/recovery", headers={"Authorization": f"Bearer {ANALYST_TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert [row["adjustment_id"] for row in body] == [str(case.adjustment_id)]
    assert case.operation_id in body[0]["evidence_procedure"]


@pytest.mark.asyncio
async def test_an_approval_role_cannot_resolve_over_http(
    engine: AsyncEngine, client: TestClient
) -> None:
    """The role comes from the token, never from the body — so this is a 403 rather than a 200 with
    the wrong name recorded against a financial judgement."""
    _, item = await _queued(engine, marker="httprole")

    response = client.post(
        f"/api/v1/recovery/{item['id']}/resolve",
        json={"resolution": "rejected_by_evidence"},
        headers={"Authorization": f"Bearer {CONTROLLER_TOKEN}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "role_may_not_recover"
    assert (await _one("recovery_queue", id=item["id"]))["state"] == "open"


@pytest.mark.asyncio
async def test_an_operator_resolves_over_http(engine: AsyncEngine, client: TestClient) -> None:
    case, item = await _queued(engine, marker="httpok")

    response = client.post(
        f"/api/v1/recovery/{item['id']}/resolve",
        json={"resolution": "confirmed_by_evidence", "posting_ref": "SNAPSHOT-4471"},
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
    )

    assert response.status_code == 200
    assert (await _one("adjustment", id=case.adjustment_id))["posting_ref"] == "SNAPSHOT-4471"
    assert (
        client.get("/api/v1/recovery", headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}).json()
        == []
    )


@pytest.mark.asyncio
async def test_a_blocked_supersession_is_refused_over_http_and_audited(
    engine: AsyncEngine, client: TestClient
) -> None:
    """**Blocked *and* audited**, which §4.4's test list asks for and §14 requires generally:
    a refused action's expected behaviour is *"Rejected; audit event recorded"*.

    The event cannot come from the service. It refuses inside the caller's transaction, and that
    transaction is rolled back precisely because the action did not happen — an event written there
    would roll back with it. So the boundary records the refusal in a transaction of its own, and
    this test drives the boundary rather than the service for exactly that reason.
    """
    case = await _dispatched_into_ambiguity(
        engine, _weak(responder=_lost_response), marker="httpsup"
    )
    before = len(await _rows("audit_event", correlation_id="lecp:httpsup"))

    response = client.post(
        f"/api/v1/exceptions/{case.exception_id}/approve",
        json={"resolution_version": 2, "approval_token": uuid.uuid4().hex, "treatment": "rebook"},
        headers={"Authorization": f"Bearer {CONTROLLER_TOKEN}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "supersession_blocked"
    assert len(await _rows("approval", exception_id=case.exception_id)) == 1, "nothing was recorded"

    events = await _rows("audit_event", correlation_id="lecp:httpsup")
    assert len(events) == before + 1
    refusal = events[-1]
    assert (refusal["tool"], refusal["outcome"], refusal["principal"]) == (
        "approve",
        "failure",
        "controller-a",
    )
    assert refusal["scope_granted"] == "approval:controller"


@pytest.mark.asyncio
async def test_the_http_surface_offers_no_way_to_resend(client: TestClient) -> None:
    """**Reaching this queue *is* the automatic path stopping.**

    A route that let an operator trigger a send would be the ambiguity gate with an operator-shaped
    hole in it — and it would be reached exactly when nobody could say whether the first send
    applied. Asserted against the generated schema so a future route cannot appear unnoticed.
    """
    paths = client.get("/openapi.json").json()["paths"]

    recovery_paths = {path for path in paths if "/recovery" in path}
    assert recovery_paths == {"/api/v1/recovery", "/api/v1/recovery/{recovery_id}/resolve"}
    for path in recovery_paths:
        for method, operation in paths[path].items():
            assert method in {"get", "post"}
            assert "resend" not in json.dumps(operation).lower()


# ======================================================================================
# The audit trail, in all three branches
# ======================================================================================


async def _events(correlation_id: str) -> list[tuple[str, str, str]]:
    rows = await _rows("audit_event", correlation_id=correlation_id)
    return sorted((row["tool"], row["outcome"], row["principal"]) for row in rows)


@pytest.mark.asyncio
async def test_the_query_branch_leaves_a_complete_trail(engine: AsyncEngine) -> None:
    adapter = _queryable(responder=_lost_then_lands())
    case = await _dispatched_into_ambiguity(engine, adapter, marker="trail1")
    await adapter.post(case.operation_id, case.instruction)
    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )

    assert await _events("lecp:trail1") == [
        ("post", "quarantined", "system"),
        ("post", "quarantined", "system"),
        ("reconcile", "success", "system"),
    ]


@pytest.mark.asyncio
async def test_the_resend_branch_leaves_a_complete_trail(engine: AsyncEngine) -> None:
    adapter = _resendable(responder=_lost_then_lands())
    case = await _dispatched_into_ambiguity(engine, adapter, marker="trail2")
    await adapter.post(case.operation_id, case.instruction)
    await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(minutes=1),
    )

    assert await _events("lecp:trail2") == [
        ("post", "quarantined", "system"),
        ("post", "quarantined", "system"),
        ("post", "quarantined", "system"),
        ("post", "success", "system"),
    ], "two events for the ambiguous send, two for the suppressed re-send"


@pytest.mark.asyncio
async def test_the_manual_branch_leaves_a_complete_trail_naming_the_operator(
    engine: AsyncEngine,
) -> None:
    """The only branch whose events carry a human name, which is the point of recording them."""
    adapter = _weak(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="trail3")
    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )
    item = await _one("recovery_queue", adjustment_id=case.adjustment_id)
    await resolve_item(
        engine,
        recovery_id=item["id"],
        principal=OPERATOR,
        resolution=RecoveryResolution.RESOLVED_UNVERIFIED,
        now=AFTER_WINDOWS,
    )

    assert await _events("lecp:trail3") == [
        ("post", "quarantined", "system"),
        ("post", "quarantined", "system"),
        ("recover", "abstained", "operator-a"),
        ("recover", "quarantined", "system"),
    ], "an unverified resolution is an abstention, not a success and not a failure"


@pytest.mark.asyncio
async def test_the_audit_trail_is_append_only(engine: AsyncEngine) -> None:
    await _dispatched_into_ambiguity(engine, _weak(responder=_lost_response), marker="audit")

    for statement in (
        "UPDATE audit_event SET outcome = 'success'",
        "DELETE FROM audit_event",
    ):
        with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="append-only"):
            await _execute(statement)


@pytest.mark.asyncio
async def test_the_migration_refuses_to_downgrade_over_an_audit_trail_it_would_break(
    engine: AsyncEngine,
) -> None:
    """**A migration must not decide, on an operator's behalf, to destroy history.**

    Re-narrowing the audit vocabulary is impossible while `reconcile` or `recover` events exist:
    the constraint would be violated by rows that are real history, and the obvious escape — delete
    them — is both wrong and unavailable, because the table is append-only to every role including
    the owner. So the downgrade stops and says what it found.

    Nothing is applied when it stops: the check runs before any DDL in ``downgrade()``, and Alembic
    wraps the migration in a transaction regardless, so the schema is unchanged after this test.
    """
    adapter = _weak(responder=_lost_response)
    case = await _dispatched_into_ambiguity(engine, adapter, marker="downgrade")
    await reconcile_once(
        engine, adjustment_id=case.adjustment_id, adapter=adapter, now=AFTER_WINDOWS
    )
    assert await _rows("audit_event") != []

    result = subprocess.run(
        ["uv", "run", "alembic", "downgrade", "-1"],
        cwd=REPO_ROOT,
        env={**os.environ, "LECP_POSTGRES_DSN": DSN},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing to downgrade" in result.stderr
    # Unchanged, so the rest of the module still has a schema to run against.
    assert await _rows("reconciliation_query", adjustment_id=case.adjustment_id) == []
    await _execute("SELECT 1 FROM reconciliation_query LIMIT 1")


@pytest.mark.asyncio
async def test_a_human_decision_names_the_role_it_was_taken_under(engine: AsyncEngine) -> None:
    """§11's *"authorisation under which the action ran"*, for the one action a person takes."""
    exception_id = await _seed_residual(marker="approved")
    async with AsyncSession(engine) as session, session.begin():
        await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=uuid.uuid4().hex,
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    row = await _one("audit_event", correlation_id="lecp:approved")
    assert (row["tool"], row["approval_decision"], row["approver"]) == (
        "approve",
        "approved",
        "controller-a",
    )
    assert row["scope_granted"] == "approval:controller"


# ======================================================================================
# The capability record the three configurations actually present
# ======================================================================================


@pytest.mark.asyncio
async def test_the_three_configurations_are_what_they_claim_to_be() -> None:
    """Asserted against the **effective** record, which is the only one a branch may act on.

    A configuration table in a docstring is a comment. This is the same table, checked — and checked
    after the conformance downgrade, so a configuration that looked strong because nobody had proven
    it would fail here rather than silently take the wrong branch.
    """
    strong = capabilities_for(_queryable())
    assert strong.suppresses_duplicates and strong.queryable_by_operation_id

    resendable = capabilities_for(_resendable())
    assert resendable.suppresses_duplicates and not resendable.queryable_by_operation_id

    weak = capabilities_for(_weak())
    assert not weak.suppresses_duplicates and not weak.queryable_by_operation_id
    assert not weak.permits_effectively_once_claim, (
        "no effectively-once claim may be made about this adapter, in code or in prose"
    )


@pytest.mark.asyncio
async def test_an_eventual_adapter_waits_out_its_declared_visibility_bound(
    engine: AsyncEngine,
) -> None:
    """The ``EVENTUAL`` half of §13.5's ``NotFound`` rule — one the reference adapter cannot show.

    Declared with a visibility bound far longer than the in-flight window, so the bound is the
    binding constraint and a system that consulted only ``max_inflight_window`` would resolve early.
    """
    adapter = SimulatedLedger(
        responder=_lost_response,
        capabilities=LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.ENFORCES_KEY,
            idempotency_window=dt.timedelta(days=1),
            posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
            query_consistency=Eventual(visibility_bound=dt.timedelta(hours=6)),
            max_inflight_window=INFLIGHT,
        ),
    )
    case = await _dispatched_into_ambiguity(engine, adapter, marker="eventual")
    policy = ReconciliationPolicy(consecutive_not_found=2, max_queries=20)

    inside = [
        await reconcile_once(
            engine,
            adjustment_id=case.adjustment_id,
            adapter=adapter,
            now=EPOCH + dt.timedelta(hours=5, seconds=index),
            policy=policy,
        )
        for index in range(2)
    ]
    assert [r.resolution for r in inside] == [Resolution.UNRESOLVED, Resolution.UNRESOLVED]
    assert inside[-1].consecutive_not_found == 2, "the count is met; the visibility bound is not"

    after = await reconcile_once(
        engine,
        adjustment_id=case.adjustment_id,
        adapter=adapter,
        now=EPOCH + dt.timedelta(hours=7),
        policy=policy,
    )
    assert after.resolution is Resolution.REJECTED
    row = await _one("reconciliation_query", adjustment_id=case.adjustment_id, query_no=1)
    assert row["visibility_bound"] == dt.timedelta(hours=6)


@pytest.mark.asyncio
async def test_a_notfound_answer_from_a_queryable_adapter_carries_no_posting_reference() -> None:
    """A constraint, not a convention: ``(answer = 'found') = (posting_ref IS NOT NULL)``.

    Written as an equality of two booleans rather than as an implication, because the implication
    form permits a ``found`` row with no reference — a resolution to ``CONFIRMED`` with no evidence
    behind it.
    """
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="posting_ref_iff_found"):
        await _execute(
            "INSERT INTO reconciliation_query (id, adjustment_id, operation_id, query_no,"
            " queried_at, answer, posting_ref, visibility_bound, max_inflight_window)"
            " VALUES ($1, $2, $3, 1, $4, 'found', NULL, interval '0', interval '0')",
            uuid.uuid4(),
            uuid.uuid4(),
            "a" * 64,
            EPOCH,
        )


@pytest.mark.asyncio
async def test_a_query_row_cannot_be_recorded_against_a_different_operation() -> None:
    """The composite foreign key. An observation filed under the wrong operation would look entirely
    well-formed while being about something else, and recovery reads these rows to decide whether an
    irreversible write may be repeated."""
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await _execute(
            "INSERT INTO reconciliation_query (id, adjustment_id, operation_id, query_no,"
            " queried_at, answer, visibility_bound, max_inflight_window)"
            " VALUES ($1, $2, $3, 1, $4, 'not_found', interval '0', interval '0')",
            uuid.uuid4(),
            uuid.uuid4(),
            "b" * 64,
            EPOCH,
        )


@pytest.mark.asyncio
async def test_notfound_is_the_ledgers_honest_answer_when_nothing_was_applied() -> None:
    """A guard on the fixture rather than on the system.

    Every ``NotFound`` assertion above is only meaningful if the simulated ledger really would have
    said ``Found`` had the posting been there.
    """
    adapter = _queryable()
    instruction = PostingInstruction(
        adjustment_id=uuid.uuid4(),
        amount=decimal.Decimal("1.0000"),
        currency="EUR",
        account_code=REBOOK_ACCOUNT,
        period="2026-06",
    )
    operation = "c" * 64

    assert isinstance(await adapter.get_by_operation_id(operation), NotFound)
    await adapter.post(operation, instruction)
    found = await adapter.get_by_operation_id(operation)
    assert not isinstance(found, NotFound)
