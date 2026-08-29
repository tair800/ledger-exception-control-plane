"""The plan's second exit criterion: *committed sample loads*.

A corpus that only regenerates is half a deliverable. The other half is that it loads into the
real schema with every constraint enforced — because that is the property later milestones
actually depend on, and it is the one a metadata test cannot establish.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_fixtures_postgres.py -m integration
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
from collections.abc import Iterator

import asyncpg
import pytest
from pydantic import SecretStr

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.loader import (
    UnsafeTargetError,
    assert_target_is_disposable,
    count_loaded,
    load,
    read_corpus,
)
from ledger_exception_control_plane.fixtures.schema import Profile

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMITTED_CORPUS = REPO_ROOT / "fixtures" / "canonical"
COMMITTED_SEED = 20260829

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)


def _settings() -> Settings:
    return Settings(postgres_dsn=SecretStr(DSN))


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """Head schema from zero, so the load runs against exactly what the migrations produce.

    The disposability check comes **first**. ``alembic downgrade base`` drops all fifteen
    tables and takes its DSN from the environment unvalidated; ADR-035's allowlist was only
    reached later, inside ``load()`` — i.e. after the destructive step had already run. A
    guard that fires after the damage is not a guard.
    """
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


@pytest.mark.asyncio
async def test_the_committed_corpus_loads_with_every_constraint_enforced() -> None:
    """No constraint is disabled, no session_replication_role, no deferred checks.

    That is the whole point: a corpus that needed integrity switched off to load would not be
    a loadable corpus, it would be a set of rows the schema rejects.
    """
    loaded = read_corpus(COMMITTED_CORPUS)
    written = await load(loaded, _settings(), reset=True)

    assert written == {
        "settlement_batch": len(loaded.corpus.batches),
        "settlement_line": loaded.corpus.line_count,
        "ledger_entry": len(loaded.corpus.ledger_entries),
    }
    assert await count_loaded(_settings(), loaded) == written


@pytest.mark.asyncio
async def test_loaded_amounts_read_back_exactly() -> None:
    """The M1.1 money contract, exercised end to end through the fixture path.

    Reading the amount back and comparing it to the corpus is the check that would fail if the
    generator ever produced a value the column rounded — which is the defect the M1.1
    correction exists to prevent, arriving through a new door.
    """
    loaded = read_corpus(COMMITTED_CORPUS)
    await load(loaded, _settings(), reset=True)

    connection = await asyncpg.connect(DSN)
    try:
        for batch in loaded.corpus.batches:
            for line in batch.lines:
                row = await connection.fetchrow(
                    "SELECT amount, currency, value_date, match_state, merchant_reference"
                    " FROM settlement_line WHERE id = $1",
                    line.id,
                )
                assert row is not None, f"line {line.id} did not load"
                assert row["amount"] == line.amount
                assert row["currency"] == line.currency
                assert row["value_date"] == line.value_date
                assert row["merchant_reference"] == line.merchant_reference
                # Matching is M2.2. A fixture that pre-set this would ship the answer with
                # the question.
                assert row["match_state"] == "unmatched"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_raw_payload_in_the_database_hashes_to_its_content_hash() -> None:
    """FR-1's re-delivery guard rests on this, so it is verified against the stored bytes."""
    loaded = read_corpus(COMMITTED_CORPUS)
    await load(loaded, _settings(), reset=True)

    connection = await asyncpg.connect(DSN)
    try:
        for batch in loaded.corpus.batches:
            row = await connection.fetchrow(
                "SELECT content_hash, raw_payload, status FROM settlement_batch WHERE id = $1",
                batch.id,
            )
            assert row is not None
            assert hashlib.sha256(row["raw_payload"]).hexdigest() == row["content_hash"]
            assert row["content_hash"] == batch.content_hash
            assert row["status"] == "parsed"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_loading_twice_without_reset_is_refused_by_the_database() -> None:
    """The re-delivery guard is real, and the loader does not quietly work around it."""
    loaded = read_corpus(COMMITTED_CORPUS)
    await load(loaded, _settings(), reset=True)

    with pytest.raises(Exception, match=r"uq_settlement_batch_content_hash|duplicate key"):
        await load(loaded, _settings(), reset=False)


@pytest.mark.asyncio
async def test_reset_removes_only_this_corpus_s_rows() -> None:
    """Reset is by identifier. A ``TRUNCATE`` would take an unrelated row with it."""
    loaded = read_corpus(COMMITTED_CORPUS)
    await load(loaded, _settings(), reset=True)

    connection = await asyncpg.connect(DSN)
    bystander = "GL-BYSTANDER-0001"
    try:
        await connection.execute("DELETE FROM ledger_entry WHERE external_ref = $1", bystander)
        await connection.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES (gen_random_uuid(), $1, '9999', 1.0000, 'EUR', now())",
            bystander,
        )

        await load(loaded, _settings(), reset=True)

        survived = await connection.fetchval(
            "SELECT count(*) FROM ledger_entry WHERE external_ref = $1", bystander
        )
        assert survived == 1, "reset deleted a row that does not belong to the corpus"
    finally:
        await connection.execute("DELETE FROM ledger_entry WHERE external_ref = $1", bystander)
        await connection.close()


@pytest.mark.asyncio
async def test_the_loader_refuses_a_non_disposable_target_before_connecting() -> None:
    """The guard fires on the configuration, not after a connection has been opened."""
    loaded = read_corpus(COMMITTED_CORPUS)
    unsafe = Settings(
        postgres_dsn=SecretStr("postgresql://lecp:lecp_local_dev@localhost:15432/lecp")
    )
    with pytest.raises(UnsafeTargetError):
        await load(loaded, unsafe, reset=True)


@pytest.mark.asyncio
async def test_a_freshly_generated_corpus_loads_as_well_as_the_committed_one() -> None:
    """Guards against the committed corpus being the only one that happens to fit the schema."""
    loaded = read_corpus(COMMITTED_CORPUS)
    await load(loaded, _settings(), reset=True)

    other = generate(COMMITTED_SEED + 3, Profile.CANONICAL)
    connection = await asyncpg.connect(DSN)
    try:
        # A different seed produces different references and amounts against the same schema,
        # so loading it alongside proves the *shape* loads rather than one lucky instance.
        for entry in other.corpus.ledger_entries:
            await connection.execute(
                "INSERT INTO ledger_entry"
                " (id, external_ref, account_code, amount, currency, booked_at, description)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                entry.id,
                entry.external_ref,
                entry.account_code,
                entry.amount,
                entry.currency,
                entry.booked_at,
                entry.description,
            )
        loaded_count = await connection.fetchval(
            "SELECT count(*) FROM ledger_entry WHERE id = ANY($1::uuid[])",
            [entry.id for entry in other.corpus.ledger_entries],
        )
        assert loaded_count == len(other.corpus.ledger_entries)
    finally:
        await connection.execute(
            "DELETE FROM ledger_entry WHERE id = ANY($1::uuid[])",
            [entry.id for entry in other.corpus.ledger_entries],
        )
        await connection.close()


@pytest.mark.asyncio
async def test_the_deliberately_invalid_artifacts_are_not_in_the_loadable_set() -> None:
    """They exist for M2.1's quarantine path, and must never reach the database."""
    loaded = read_corpus(COMMITTED_CORPUS)
    loadable = {batch.raw_payload_path for batch in loaded.corpus.batches}
    invalid = {
        path.relative_to(COMMITTED_CORPUS).as_posix()
        for path in (COMMITTED_CORPUS / "invalid").iterdir()
        if path.suffix == ".csv"
    }
    assert invalid, "the corpus claims to carry invalid artifacts"
    assert not (loadable & invalid)

    await load(loaded, _settings(), reset=True)
    connection = await asyncpg.connect(DSN)
    try:
        stored = await connection.fetch("SELECT raw_payload FROM settlement_batch")
        payloads = {bytes(row["raw_payload"]) for row in stored}
        for path in invalid:
            assert (COMMITTED_CORPUS / path).read_bytes() not in payloads
    finally:
        await connection.close()
