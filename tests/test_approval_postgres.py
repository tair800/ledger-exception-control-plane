"""M5.1 against real PostgreSQL — the human gate, role separation and single use.

`IMPLEMENTATION_PLAN.md` §5.1's exit criterion is not "an endpoint exists". It is:

    **The gate blocks the write, proven by test.**

So the load-bearing test here is not that a rejection returns 403 — it is that a rejected decision
**cannot be referenced by an adjustment at all**, because the composite foreign key M1.2 put on
``adjustment`` refuses it. That is a property of the database, and it can only be established
against a real one.

The rest is §16: authenticated principals, role separation between analyst, controller and operator,
the countersignature rule on an edit, and §14's *"replay of a consumed approval token"*.

Marked ``integration``; needs PostgreSQL only.
"""

from __future__ import annotations

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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    ApprovalDecision,
    TreatmentCode,
)
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.operations.approval import (
    ApprovalRefusedError,
    RefusalReason,
    record_decision,
)
from ledger_exception_control_plane.security import Principal, Role

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)
EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

ANALYST = Principal(id="analyst-a", role=Role.ANALYST)
OTHER_ANALYST = Principal(id="analyst-b", role=Role.ANALYST)
CONTROLLER = Principal(id="controller-a", role=Role.CONTROLLER)
OPERATOR = Principal(id="operator-a", role=Role.OPERATOR)


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
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_slate() -> AsyncIterator[None]:
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
    yield


async def _seed_exception(marker: str) -> uuid.UUID:
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


def _token() -> str:
    return uuid.uuid4().hex


# ======================================================================================
# The exit criterion: the gate blocks the write
# ======================================================================================


@pytest.mark.asyncio
async def test_a_rejected_decision_cannot_be_referenced_by_an_adjustment(
    engine: AsyncEngine,
) -> None:
    """**The exit criterion, and it is the database that enforces it.**

    §5.1: *"The gate blocks the write, proven by test."* A rejection carries
    ``approved_treatment IS NULL``; ``adjustment.approved_treatment`` is ``NOT NULL`` and the
    composite foreign key references ``(id, approved_treatment, principal)``. So an adjustment
    referencing a rejection has nothing to point at — there is no trigger, no application check and
    no ordering assumption involved, which is why this holds even against code that has never heard
    of the rule.
    """
    exception_id = await _seed_exception("blocked")

    async with AsyncSession(engine) as session, session.begin():
        rejection = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=ANALYST,
            decision=ApprovalDecision.REJECTED,
            approval_token=_token(),
            now=EPOCH,
        )

    assert rejection.approved_treatment is None

    # Now try to post money against it, the way a defect in a later increment would.
    with pytest.raises(IntegrityError, match="fk_adjustment_approval"):
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Adjustment(
                    approval_id=rejection.approval_id,
                    approved_treatment=TreatmentCode.REBOOK,
                    approving_principal=rejection.principal,
                    amount=decimal.Decimal("2799.97"),
                    currency="EUR",
                    account_code="4100",
                    period="2026-06",
                    operation_id="a" * 64,
                    instruction_payload_hash="b" * 64,
                )
            )

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM adjustment") == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_an_approved_decision_can_be_referenced(engine: AsyncEngine) -> None:
    """The control. A foreign key that refused *everything* would pass the test above."""
    exception_id = await _seed_exception("permitted")

    async with AsyncSession(engine) as session, session.begin():
        approval = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=_token(),
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    async with AsyncSession(engine) as session, session.begin():
        session.add(
            Adjustment(
                approval_id=approval.approval_id,
                approved_treatment=TreatmentCode.REBOOK,
                approving_principal=approval.principal,
                amount=decimal.Decimal("2799.97"),
                currency="EUR",
                account_code="4100",
                period="2026-06",
                operation_id="c" * 64,
                instruction_payload_hash="d" * 64,
            )
        )

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM adjustment") == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_an_adjustment_cannot_name_a_treatment_the_approval_did_not_authorise(
    engine: AsyncEngine,
) -> None:
    """**The subtler half of the same key**, and the reason it is composite.

    A plain foreign key to ``approval.id`` would prove only that *an* approval exists. This one
    carries the treatment and the principal into the reference, so an adjustment claiming a
    treatment nobody authorised has nothing to point at either.
    """
    exception_id = await _seed_exception("mismatch")

    async with AsyncSession(engine) as session, session.begin():
        approval = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=_token(),
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    with pytest.raises(IntegrityError, match="fk_adjustment_approval"):
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Adjustment(
                    approval_id=approval.approval_id,
                    # Authorised REBOOK; this claims WRITE_OFF.
                    approved_treatment=TreatmentCode.WRITE_OFF,
                    approving_principal=approval.principal,
                    amount=decimal.Decimal("2799.97"),
                    currency="EUR",
                    account_code="4100",
                    period="2026-06",
                    operation_id="e" * 64,
                    instruction_payload_hash="f" * 64,
                )
            )


# ======================================================================================
# Role separation — §16
# ======================================================================================


@pytest.mark.asyncio
async def test_an_operator_may_not_approve(engine: AsyncEngine) -> None:
    """The operator works the dead-letter and recovery queues and authorises no money.

    A role that could both unstick a stalled posting and authorise its amount is not a separation of
    duties, which is the whole point of §16 naming three roles rather than one.
    """
    exception_id = await _seed_exception("operator")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=exception_id,
                resolution_version=1,
                principal=OPERATOR,
                decision=ApprovalDecision.APPROVED,
                approval_token=_token(),
                now=EPOCH,
                treatment=TreatmentCode.REBOOK,
            )
    assert refusal.value.reason is RefusalReason.ROLE_MAY_NOT_APPROVE

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM approval") == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_an_analyst_may_reject_but_may_not_authorise_an_edit(engine: AsyncEngine) -> None:
    """An analyst may decline — that authorises nothing — but may not override the proposal."""
    exception_id = await _seed_exception("analystedit")

    async with AsyncSession(engine) as session, session.begin():
        rejected = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=ANALYST,
            decision=ApprovalDecision.REJECTED,
            approval_token=_token(),
            now=EPOCH,
        )
    assert rejected.decision is ApprovalDecision.REJECTED

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=exception_id,
                resolution_version=2,
                principal=ANALYST,
                decision=ApprovalDecision.EDITED,
                approval_token=_token(),
                now=EPOCH,
                treatment=TreatmentCode.WRITE_OFF,
                requested_by=OTHER_ANALYST.id,
            )
    assert refusal.value.reason is RefusalReason.ROLE_MAY_NOT_EDIT


@pytest.mark.asyncio
async def test_a_controller_may_not_countersign_their_own_edit(engine: AsyncEngine) -> None:
    """§16: *"The approver cannot be the same principal as the requester where an edit changed the
    treatment."*

    An edit is where a human overrides the model, which is exactly where one person acting alone is
    worth refusing.
    """
    exception_id = await _seed_exception("selfsign")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=exception_id,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.EDITED,
                approval_token=_token(),
                now=EPOCH,
                treatment=TreatmentCode.WRITE_OFF,
                requested_by=CONTROLLER.id,
            )
    assert refusal.value.reason is RefusalReason.SELF_COUNTERSIGNED_EDIT


@pytest.mark.asyncio
async def test_a_controller_may_authorise_an_edit_somebody_else_requested(
    engine: AsyncEngine,
) -> None:
    """The control, and the row records both halves so an auditor can see the countersignature."""
    exception_id = await _seed_exception("countersigned")

    async with AsyncSession(engine) as session, session.begin():
        record = await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.EDITED,
            approval_token=_token(),
            now=EPOCH,
            treatment=TreatmentCode.WRITE_OFF,
            requested_by=ANALYST.id,
        )

    assert record.principal == CONTROLLER.id
    assert record.requested_by == ANALYST.id

    async with AsyncSession(engine) as session, session.begin():
        stored = (
            await session.execute(select(Approval).where(Approval.id == record.approval_id))
        ).scalar_one()
        assert stored.requested_by == ANALYST.id
        assert stored.principal == CONTROLLER.id


@pytest.mark.asyncio
async def test_the_database_refuses_a_self_countersigned_edit_even_without_the_service(
    engine: AsyncEngine,
) -> None:
    """**The application check is not the control; this is.**

    Written directly against the table, the way a future increment with its own insert would. If the
    only thing standing between one principal and a self-authorised override were a function in
    ``operations/approval.py``, the rule would last exactly until somebody wrote a second path.
    """
    exception_id = await _seed_exception("dbcountersign")

    with pytest.raises(IntegrityError, match="approver_is_not_the_requester"):
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Approval(
                    exception_id=exception_id,
                    resolution_version=1,
                    decision=ApprovalDecision.EDITED,
                    approved_treatment=TreatmentCode.WRITE_OFF,
                    principal="controller-a",
                    requested_by="controller-a",
                    approval_token=_token(),
                    decided_at=EPOCH,
                )
            )


@pytest.mark.asyncio
async def test_an_edit_must_record_who_requested_it(engine: AsyncEngine) -> None:
    """Otherwise the countersignature constraint is vacuous: NULL satisfies it trivially."""
    exception_id = await _seed_exception("editnorequester")

    with pytest.raises(IntegrityError, match="requested_by_iff_edited"):
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Approval(
                    exception_id=exception_id,
                    resolution_version=1,
                    decision=ApprovalDecision.EDITED,
                    approved_treatment=TreatmentCode.WRITE_OFF,
                    principal="controller-a",
                    requested_by=None,
                    approval_token=_token(),
                    decided_at=EPOCH,
                )
            )


# ======================================================================================
# Single use — §14's "replay of a consumed approval token"
# ======================================================================================


@pytest.mark.asyncio
async def test_a_consumed_approval_token_is_refused_on_replay(engine: AsyncEngine) -> None:
    """§14: *"Replay of a consumed approval token → Rejected."*

    The replay targets a *different* exception, so the refusal cannot be attributed to the
    one-decision-per-resolution-version constraint. It is the token that is spent.
    """
    first = await _seed_exception("tokenone")
    second = await _seed_exception("tokentwo")
    token = _token()

    async with AsyncSession(engine) as session, session.begin():
        await record_decision(
            session,
            exception_id=first,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=token,
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=second,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=token,
                now=EPOCH,
                treatment=TreatmentCode.REBOOK,
            )
    assert refusal.value.reason is RefusalReason.TOKEN_ALREADY_USED

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM approval") == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_one_decision_per_resolution_version(engine: AsyncEngine) -> None:
    """§13.1: at most one approved resolution per (exception, resolution_version).

    A second decision on the same version — even with a fresh token — is refused, so an exception
    cannot end up with two live authorisations at the same version.
    """
    exception_id = await _seed_exception("oneversion")

    async with AsyncSession(engine) as session, session.begin():
        await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=_token(),
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=exception_id,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=_token(),
                now=EPOCH,
                treatment=TreatmentCode.WRITE_OFF,
            )
    assert refusal.value.reason is RefusalReason.ALREADY_DECIDED


# ======================================================================================
# Shape refusals
# ======================================================================================


@pytest.mark.asyncio
async def test_a_rejection_may_not_name_a_treatment(engine: AsyncEngine) -> None:
    """A rejection authorises nothing, and a rejection carrying a treatment is exactly the row the
    adjustment foreign key exists to make unreferenceable."""
    exception_id = await _seed_exception("rejecttreat")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=exception_id,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.REJECTED,
                approval_token=_token(),
                now=EPOCH,
                treatment=TreatmentCode.REBOOK,
            )
    assert refusal.value.reason is RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION


@pytest.mark.asyncio
async def test_an_approval_must_name_the_treatment_it_authorises(engine: AsyncEngine) -> None:
    exception_id = await _seed_exception("approvenotreat")

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=exception_id,
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=_token(),
                now=EPOCH,
            )
    assert refusal.value.reason is RefusalReason.TREATMENT_INCONSISTENT_WITH_DECISION


@pytest.mark.asyncio
async def test_a_decision_on_an_unknown_exception_is_refused(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(ApprovalRefusedError) as refusal:
            await record_decision(
                session,
                exception_id=uuid.uuid4(),
                resolution_version=1,
                principal=CONTROLLER,
                decision=ApprovalDecision.APPROVED,
                approval_token=_token(),
                now=EPOCH,
                treatment=TreatmentCode.REBOOK,
            )
    assert refusal.value.reason is RefusalReason.UNKNOWN_SUBJECT


@pytest.mark.asyncio
async def test_the_gate_computes_no_amount(engine: AsyncEngine) -> None:
    """**The AI/money boundary, at the human gate.**

    An approval authorises a treatment *code*. It writes no adjustment and computes no amount —
    M2.4 does that afterwards, deterministically, from the approved code and ledger data. Asserted
    on the database rather than on the source, so a future path that computed money inside the gate
    would fail here even if it kept the module's shape.
    """
    exception_id = await _seed_exception("noamount")

    async with AsyncSession(engine) as session, session.begin():
        await record_decision(
            session,
            exception_id=exception_id,
            resolution_version=1,
            principal=CONTROLLER,
            decision=ApprovalDecision.APPROVED,
            approval_token=_token(),
            now=EPOCH,
            treatment=TreatmentCode.REBOOK,
        )

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM adjustment") == 0
        assert await connection.fetchval("SELECT count(*) FROM outbox") == 0
        assert await connection.fetchval("SELECT count(*) FROM posting_attempt") == 0
    finally:
        await connection.close()
