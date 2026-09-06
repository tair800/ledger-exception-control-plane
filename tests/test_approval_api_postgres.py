"""M5.1 — the `/api/v1` surface, driven over HTTP against real PostgreSQL.

The service layer is tested in ``test_approval_postgres.py``. What is tested here is the part a
client actually touches, and specifically the part where an authorization mistake becomes a
financial one: whether an unauthenticated request can act, whether a client can name its own actor,
and whether the role recorded in the database is the one the token carried rather than the one the
request body asked for.

Marked ``integration``; needs PostgreSQL only.
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

from ledger_exception_control_plane.api import create_app
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.security import token_fingerprint

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)
EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

ANALYST_TOKEN = "analyst-token-for-tests-0123"
CONTROLLER_TOKEN = "controller-token-for-tests-0123"
OPERATOR_TOKEN = "operator-token-for-tests-0123"

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


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN), principals=REGISTRY)


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


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings())) as test_client:
        yield test_client


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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "resolution_version": 1,
        "approval_token": uuid.uuid4().hex,
        "treatment": "rebook",
    }
    payload.update(overrides)
    return payload


# ======================================================================================
# Authentication
# ======================================================================================


@pytest.mark.asyncio
async def test_an_unauthenticated_request_cannot_approve(client: TestClient) -> None:
    """**Fail closed, and say nothing useful about why.**

    The 401 carries ``WWW-Authenticate: Bearer`` so a client knows the scheme, and a message that
    does not distinguish absent from malformed from unknown — distinguishing them turns the endpoint
    into an oracle for guessing tokens.
    """
    exception_id = await _seed_exception("noauth")

    response = client.post(f"/api/v1/exceptions/{exception_id}/approve", json=_body())

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM approval") == 0
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        ("no header", {}),
        ("an unknown token", {"Authorization": "Bearer nope"}),
        ("the wrong scheme", {"Authorization": "Basic YWRtaW46YWRtaW4="}),
        ("a bare token", {"Authorization": ANALYST_TOKEN}),
        ("an empty bearer", {"Authorization": "Bearer "}),
    ],
)
@pytest.mark.asyncio
async def test_every_unauthenticated_shape_is_refused(
    client: TestClient, label: str, headers: dict[str, str]
) -> None:
    exception_id = await _seed_exception(f"shape{abs(hash(label)) % 10000}")

    response = client.post(
        f"/api/v1/exceptions/{exception_id}/approve", json=_body(), headers=headers
    )

    assert response.status_code == 401, label


@pytest.mark.asyncio
async def test_reading_the_queue_also_requires_a_principal(client: TestClient) -> None:
    """The queue carries merchant references and amounts; it is not public."""
    assert client.get("/api/v1/exceptions").status_code == 401
    assert client.get("/api/v1/exceptions", headers=_auth(ANALYST_TOKEN)).status_code == 200


# ======================================================================================
# The client cannot name its own actor
# ======================================================================================


@pytest.mark.asyncio
async def test_a_client_cannot_assert_which_principal_it_is(client: TestClient) -> None:
    """**The property that makes the audit trail worth anything.**

    There is no ``principal`` field on the request model and ``extra="forbid"`` rejects one, so a
    caller cannot supply the actor even by guessing the field name. The recorded principal comes
    from the token, always.
    """
    exception_id = await _seed_exception("selfassert")

    response = client.post(
        f"/api/v1/exceptions/{exception_id}/approve",
        json=_body(principal="controller-a"),
        headers=_auth(ANALYST_TOKEN),
    )

    assert response.status_code == 422, "an unknown field must be rejected, not ignored"

    connection = await asyncpg.connect(DSN)
    try:
        assert await connection.fetchval("SELECT count(*) FROM approval") == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_recorded_principal_is_the_one_the_token_carried(client: TestClient) -> None:
    exception_id = await _seed_exception("recorded")

    response = client.post(
        f"/api/v1/exceptions/{exception_id}/approve",
        json=_body(),
        headers=_auth(CONTROLLER_TOKEN),
    )

    assert response.status_code == 200
    assert response.json()["principal"] == "controller-a"

    connection = await asyncpg.connect(DSN)
    try:
        stored = await connection.fetchval("SELECT principal FROM approval")
    finally:
        await connection.close()
    assert stored == "controller-a"


# ======================================================================================
# Role separation over HTTP
# ======================================================================================


@pytest.mark.asyncio
async def test_an_operator_is_refused_by_the_endpoint(client: TestClient) -> None:
    exception_id = await _seed_exception("apioperator")

    response = client.post(
        f"/api/v1/exceptions/{exception_id}/approve",
        json=_body(),
        headers=_auth(OPERATOR_TOKEN),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "role_may_not_approve"


@pytest.mark.asyncio
async def test_an_analyst_is_refused_the_edit_route(client: TestClient) -> None:
    exception_id = await _seed_exception("apiedit")

    response = client.post(
        f"/api/v1/exceptions/{exception_id}/edit",
        json=_body(treatment="write_off", requested_by="analyst-b"),
        headers=_auth(ANALYST_TOKEN),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "role_may_not_edit"


@pytest.mark.asyncio
async def test_a_controller_may_not_countersign_their_own_edit_over_http(
    client: TestClient,
) -> None:
    exception_id = await _seed_exception("apiselfsign")

    response = client.post(
        f"/api/v1/exceptions/{exception_id}/edit",
        json=_body(treatment="write_off", requested_by="controller-a"),
        headers=_auth(CONTROLLER_TOKEN),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "self_countersigned_edit"


# ======================================================================================
# Single use, over HTTP
# ======================================================================================


@pytest.mark.asyncio
async def test_the_endpoint_returns_the_claimed_key_and_refuses_its_replay(
    client: TestClient,
) -> None:
    """§10: *"returns the claimed idempotency key"*. §14: a replay of a consumed one is rejected."""
    first = await _seed_exception("apitoken1")
    second = await _seed_exception("apitoken2")
    token = uuid.uuid4().hex

    accepted = client.post(
        f"/api/v1/exceptions/{first}/approve",
        json=_body(approval_token=token),
        headers=_auth(CONTROLLER_TOKEN),
    )
    assert accepted.status_code == 200
    assert accepted.json()["approval_token"] == token

    replayed = client.post(
        f"/api/v1/exceptions/{second}/approve",
        json=_body(approval_token=token),
        headers=_auth(CONTROLLER_TOKEN),
    )
    assert replayed.status_code == 409
    assert replayed.json()["detail"]["reason"] == "token_already_used"


# ======================================================================================
# Reads
# ======================================================================================


@pytest.mark.asyncio
async def test_the_queue_lists_exceptions_and_the_detail_carries_provenance(
    client: TestClient,
) -> None:
    """The two reads the console is built on. Detail answers "what happened to this exception"."""
    exception_id = await _seed_exception("detail")

    queue = client.get("/api/v1/exceptions", headers=_auth(ANALYST_TOKEN))
    assert queue.status_code == 200
    listed = queue.json()
    assert len(listed) == 1
    assert listed[0]["id"] == str(exception_id)
    assert listed[0]["decided"] is False

    client.post(
        f"/api/v1/exceptions/{exception_id}/approve",
        json=_body(),
        headers=_auth(CONTROLLER_TOKEN),
    )

    detail = client.get(f"/api/v1/exceptions/{exception_id}", headers=_auth(ANALYST_TOKEN))
    assert detail.status_code == 200
    payload = detail.json()

    assert payload["line"]["currency"] == "EUR"
    assert payload["approval"]["decision"] == "approved"
    assert payload["approval"]["principal"] == "controller-a"
    assert payload["adjustment"] is None, "approving authorises a treatment; money comes later"
    assert payload["attempts"] == []


@pytest.mark.asyncio
async def test_an_unknown_exception_is_a_404_not_a_500(client: TestClient) -> None:
    response = client.get(f"/api/v1/exceptions/{uuid.uuid4()}", headers=_auth(ANALYST_TOKEN))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_no_response_carries_a_credential_or_a_dsn(client: TestClient) -> None:
    """A 401's body, a 403's body and a successful read are all checked.

    Error paths are where secrets leak, because they are the paths written under time pressure and
    read least often.
    """
    exception_id = await _seed_exception("noleak")

    responses = [
        client.post(f"/api/v1/exceptions/{exception_id}/approve", json=_body()),
        client.post(
            f"/api/v1/exceptions/{exception_id}/approve",
            json=_body(),
            headers=_auth(OPERATOR_TOKEN),
        ),
        client.get("/api/v1/exceptions", headers=_auth(ANALYST_TOKEN)),
    ]

    for response in responses:
        body = response.text
        assert "lecp_local_dev" not in body
        assert CONTROLLER_TOKEN not in body
        assert ANALYST_TOKEN not in body
        assert token_fingerprint(CONTROLLER_TOKEN) not in body
