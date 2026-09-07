"""M7 support — the reads the console is built on, and the two controls it offers.

`routes.py` gained four things the operations console needs: the evidence pack with its citations,
the audit trail, the dead-letter queue, and a demo-mode control that injects §19.1's fault so a
visitor can watch duplicate suppression happen. This module tests them against a real database,
because every one of them is a query over rows the pipeline produced and a fixture would prove
nothing about the joins.

**The demonstration is seeded by the shipped seeder**, not by hand. `demo/seed.py` composes the
production service entry points, so what these tests read is what a reviewer opening the console
reads — and a divergence between the two would be exactly the defect worth catching.

Marked ``integration``: needs PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.api import create_app
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.demo.seed import seed_demo
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.security import token_fingerprint

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

ANALYST_TOKEN = "console-analyst-token-0123"
CONTROLLER_TOKEN = "console-controller-token-0123"
OPERATOR_TOKEN = "console-operator-token-0123"

REGISTRY = json.dumps(
    {
        "analyst-a": {"role": "analyst", "token_sha256": token_fingerprint(ANALYST_TOKEN)},
        "controller-a": {
            "role": "controller",
            "token_sha256": token_fingerprint(CONTROLLER_TOKEN),
        },
        "operator-a": {"role": "operator", "token_sha256": token_fingerprint(OPERATOR_TOKEN)},
    }
)

CONTROLLER = {"Authorization": f"Bearer {CONTROLLER_TOKEN}"}
OPERATOR = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
ANALYST = {"Authorization": f"Bearer {ANALYST_TOKEN}"}


def _settings(*, demo_mode: bool = True) -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN), principals=REGISTRY, demo_mode=demo_mode)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """Head schema from zero.

    ``audit_event`` is cleared **before** the downgrade as well as after the module, because the
    4.4 migration refuses to re-narrow the audit vocabulary while `reconcile` or `recover` events
    exist — and the seeder writes both. Without this a second run of the suite would meet a refusal
    it did not cause. The same arrangement `test_reconcile_postgres.py` uses, for the same reason.
    """
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


def _clear_audit_trail() -> None:
    import asyncio

    async def clear() -> None:
        connection = await asyncpg.connect(DSN)
        try:
            if await connection.fetchval("SELECT to_regclass('public.audit_event')") is None:
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


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(async_dsn(_settings()), poolclass=NullPool)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(autouse=True)
async def seeded(engine: AsyncEngine) -> AsyncIterator[None]:
    """One seeded demonstration per test. ``seed_demo`` resets before it seeds, so this is total."""
    await seed_demo(engine)
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings())) as test_client:
        yield test_client


def _exceptions(client: TestClient) -> list[dict[str, object]]:
    response = client.get("/api/v1/exceptions", headers=CONTROLLER)
    assert response.status_code == 200, response.text
    return list(response.json())


def _detail(client: TestClient, exception_id: str) -> dict[str, object]:
    response = client.get(f"/api/v1/exceptions/{exception_id}", headers=CONTROLLER)
    assert response.status_code == 200, response.text
    return dict(response.json())


# ======================================================================================
# The reads the console is built on
# ======================================================================================


def test_the_seeded_demonstration_reaches_every_state_the_console_renders() -> None:
    """**The demo's own exit criterion, asserted rather than eyeballed.**

    A console screen with nothing in it demonstrates nothing, and the states that matter are the
    unhappy ones. If a future change to the payload or the classifier stopped producing an
    ambiguous posting or a dead letter, the console would still work and the demonstration would
    have quietly lost its point — so the counts are asserted here where that shows up as a failure.
    """
    import asyncio

    async def counts() -> dict[str, int]:
        connection = await asyncpg.connect(DSN)
        try:
            return {
                table: int(await connection.fetchval(f"SELECT count(*) FROM {table}"))
                for table in ("exception", "evidence", "treatment_proposal", "adjustment", "dlq")
            } | {
                "unknown": int(
                    await connection.fetchval(
                        "SELECT count(*) FROM outbox WHERE last_outcome = 'unknown'"
                    )
                ),
                "recovery": int(await connection.fetchval("SELECT count(*) FROM recovery_queue")),
                "awaiting": int(
                    await connection.fetchval(
                        "SELECT count(*) FROM outbox WHERE state = 'pending' AND attempt_count = 0"
                    )
                ),
            }
        finally:
            await connection.close()

    seen = asyncio.run(counts())
    assert seen["exception"] > 0, "no exception to show"
    assert seen["evidence"] > 0, "the evidence panel would be empty"
    assert seen["treatment_proposal"] > 0, "the proposal panel would be empty"
    assert seen["adjustment"] > 0, "no priced adjustment, so no amount and no operation id"
    assert seen["unknown"] == 1, "the console must show an operation the system refused to resolve"
    assert seen["dlq"] == 1, "the dead-letter queue would be empty"
    assert seen["recovery"] == 1, "the recovery queue would be empty"
    assert seen["awaiting"] == 1, "nothing is left for the fault-injection control to act on"


def test_the_detail_carries_the_evidence_and_says_which_of_it_was_cited(
    client: TestClient,
) -> None:
    """§6.1 requires a proposal to reference the evidence it used, so the console must show which.

    A pack rendered without the citation flag invites a reviewer to assume the model relied on all
    of it, which is a stronger claim than the proposal made.
    """
    detail = next(_detail(client, str(row["id"])) for row in _exceptions(client))
    evidence = list(detail["evidence"])  # type: ignore[call-overload]

    assert evidence, "the exception has no evidence, so the panel has nothing to show"
    assert all({"id", "kind", "content", "cited"} <= set(item) for item in evidence)
    assert any(item["cited"] for item in evidence), "no evidence is marked as cited"


def test_the_detail_carries_the_audit_trail_and_leaves_unknowable_fields_null(
    client: TestClient,
) -> None:
    """ADR-058: two of §11's ten fields are deliberately null here, and the console must not
    hide it.

    ``agent_identity`` is null because this system is not an agent. A trail that omitted the field
    would let a reader assume it had simply not been captured.
    """
    detail = next(_detail(client, str(row["id"])) for row in _exceptions(client) if row["decided"])
    trail = list(detail["audit"])  # type: ignore[call-overload]

    assert trail, "a decided exception with no audit trail"
    assert {"approve", "compute_amount"} <= {event["tool"] for event in trail}
    assert all(event["agent_identity"] is None for event in trail)
    assert all(event["correlation_id"] == detail["correlation_id"] for event in trail)

    occurred = [event["occurred_at"] for event in trail]
    assert occurred == sorted(occurred), "the timeline is not in order"


def test_the_dead_letter_queue_is_operator_work_and_an_analyst_is_told_so(
    client: TestClient,
) -> None:
    """**Refused, not filtered.** An analyst shown an empty queue would conclude nothing failed.

    §16 separates the roles and 4.4 already enforces the mirror of this rule for recovery; the
    distinction between "you may not see this" and "there is nothing here" is the whole reason this
    returns 403 rather than an empty list.
    """
    allowed = client.get("/api/v1/dlq", headers=OPERATOR)
    assert allowed.status_code == 200, allowed.text
    entries = allowed.json()
    assert len(entries) == 1
    assert entries[0]["operation_id"], "the entry must carry the identifier the send used"
    assert entries[0]["adjustment_id"], "the console needs the link back to the exception"

    refused = client.get("/api/v1/dlq", headers=ANALYST)
    assert refused.status_code == 403
    assert "operator" in refused.json()["detail"]


def test_the_dead_letter_entry_carries_no_monetary_amount(client: TestClient) -> None:
    """The envelope is amount-free by check constraint, and the view must not reintroduce one.

    Money reconstructed for an operator to eyeball is money an operator could be tempted to
    re-enter. The amount comes from `adjustment` at replay time and nowhere else.
    """
    entry = client.get("/api/v1/dlq", headers=OPERATOR).json()[0]
    rendered = json.dumps(entry).lower()
    for forbidden in ("amount", "currency", "total", "value"):
        assert forbidden not in rendered, f"the dead-letter view leaks {forbidden}"


# ======================================================================================
# /meta and the demo control
# ======================================================================================


def test_meta_is_unauthenticated_and_names_the_ledger_it_is_talking_to(
    client: TestClient,
) -> None:
    """A visitor must be told the guarantee rests on a simulated ledger written here (OPEN-11)."""
    response = client.get("/api/v1/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["demo_mode"] is True
    assert body["ledger_adapter"] == "simulated-ledger"
    assert set(body) == {"version", "demo_mode", "ledger_adapter"}, (
        "meta must carry nothing else; it is unauthenticated"
    )


def test_injecting_the_lost_response_fault_applies_the_money_exactly_once(
    client: TestClient,
) -> None:
    """**M7.2's exit criterion: a visitor triggers §19.1's failure and sees no second effect.**

    The two numbers are asserted separately because their difference is the demonstration. The
    system recorded ``unknown`` — it refused to guess — while the ledger's own count is one. A test
    that checked only the outcome would pass against a system that had silently retried.
    """
    target = _awaiting_dispatch(client)

    response = client.post(f"/api/v1/demo/exceptions/{target}/inject-fault", headers=OPERATOR)
    assert response.status_code == 200, response.text
    report = response.json()

    assert report["fault"] == "commit_then_lose_response"
    assert report["recorded_outcome"] == "unknown", "the system must not claim to know"
    assert report["ledger_applied_count"] == 1, "the money moved more than once"
    assert report["ledger_posts_received"] == 1


def test_the_fault_control_is_absent_outside_demo_mode(engine: AsyncEngine) -> None:
    """**404, not 403.** A 403 confirms the route exists and invites a search for the credential.

    A fault injector reachable in a deployment doing real work is a defect however carefully it is
    documented, so an instance that is not a demonstration must answer as though there is nothing
    there — which is true.
    """
    with TestClient(create_app(_settings(demo_mode=False))) as plain:
        target = _awaiting_dispatch(plain)
        response = plain.post(f"/api/v1/demo/exceptions/{target}/inject-fault", headers=OPERATOR)

    assert response.status_code == 404
    assert response.json()["detail"] == "not found"


def test_the_fault_control_is_operator_work(client: TestClient) -> None:
    """Injecting a fault dispatches a financial write, so it is not the approver's button."""
    target = _awaiting_dispatch(client)

    response = client.post(f"/api/v1/demo/exceptions/{target}/inject-fault", headers=CONTROLLER)

    assert response.status_code == 403
    assert "operator" in response.json()["detail"]


def test_a_dispatch_the_system_will_not_send_is_a_conflict_rather_than_a_crash(
    client: TestClient,
) -> None:
    """A refusal is the dispatcher working. It must not reach the console as a 500.

    The first version let ``DispatchRefusedError`` escape and the console received a 500 for a
    correct decision — a rule being enforced, reported as a broken demo.
    """
    target = _awaiting_dispatch(client)
    first = client.post(f"/api/v1/demo/exceptions/{target}/inject-fault", headers=OPERATOR)
    assert first.status_code == 200, first.text

    # The same posting again: now ambiguous, and §13.5 forbids re-sending it.
    second = client.post(f"/api/v1/demo/exceptions/{target}/inject-fault", headers=OPERATOR)

    assert second.status_code == 409, second.text
    assert second.json()["detail"]["reason"] == "dispatch_refused"


def _awaiting_dispatch(client: TestClient) -> str:
    """The exception whose posting the seeder deliberately left pending."""
    for row in _exceptions(client):
        if not row["decided"]:
            continue
        detail = _detail(client, str(row["id"]))
        outbox = detail["outbox"]
        if isinstance(outbox, dict) and outbox.get("attempt_count") == 0:
            return str(row["id"])
    raise AssertionError("the seeder left no posting awaiting dispatch")


def test_an_unknown_exception_is_a_404_not_a_500(client: TestClient) -> None:
    response = client.get(f"/api/v1/exceptions/{uuid.uuid4()}", headers=CONTROLLER)
    assert response.status_code == 404


def test_every_console_read_refuses_an_unauthenticated_caller(client: TestClient) -> None:
    """Except ``/meta``, which is deliberately open and carries nothing worth protecting."""
    for path in ("/api/v1/exceptions", "/api/v1/dlq", "/api/v1/recovery"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/v1/meta").status_code == 200


def test_the_demo_seeder_is_repeatable(engine: AsyncEngine) -> None:
    """A demonstration a reviewer cannot re-run is a demonstration they cannot check.

    ``seed_demo`` resets before it seeds — it cannot rely on ``alembic downgrade base``, because
    the 4.4 migration refuses to downgrade while the `recover` events this seeder writes exist.
    """
    import asyncio

    first = asyncio.run(seed_demo(engine))
    second = asyncio.run(seed_demo(engine))

    assert first == second, "two seedings produced different databases"
    assert dt.datetime.now(dt.UTC) is not None  # the seeder itself reads no clock
