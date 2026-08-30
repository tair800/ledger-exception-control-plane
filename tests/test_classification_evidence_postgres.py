"""Movement type as evidence — the adversarial cases direction alone cannot separate.

``chargeback_reversal`` was originally reached from *direction*: a credit that exactly reverses a
debit the ledger already carries. This file exists because that is not evidence of a chargeback. A
credit reversing a booked debit is equally a fee reversal, an operational correction, or a clawback
being undone — the shapes are identical in every field the classifier could see, and the one field
that separates them was parsed by M2.1 and then dropped for want of a column.

**Every case here is built through real ingestion**, not seeded with SQL, and that is the point:
the PSP states the movement type on every row of the approved format (ADR-031), so the
information exists in the input and was lost at the persistence boundary, not absent from the feed.

The suite runs the whole path — ingest, match, classify — and asserts on what reaches ``exception``.

Marked ``integration``; needs PostgreSQL only::

    make db-up
    LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test \\
        uv run pytest tests/test_classification_evidence_postgres.py -m integration
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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ledger_exception_control_plane.classification import run_classification
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable
from ledger_exception_control_plane.ingest import ingest
from ledger_exception_control_plane.matching import run_matching

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DSN = os.environ.get(
    "LECP_POSTGRES_DSN",
    "postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test",
)

HEADER = (
    "psp_reference,merchant_reference,transaction_type,amount,currency,value_date,"
    "presentment_amount,presentment_currency,fx_rate,memo"
)
RECEIVED_AT = dt.datetime(2026, 6, 15, 8, 30, tzinfo=dt.UTC)
MATCHED_AT = dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC)

#: One amount per case, and they differ — deliberately, and it is a property of the *harness* rather
#: than of the cases.
#:
#: Each debit has to reconcile against its own ledger entry for the reversal rules to have anything
#: booked to work with, and M2.2 refuses a line with more than one candidate (ADR-043). Three debits
#: of one size against three identical entries is exactly that ambiguity, so nothing matched and the
#: cases never reached the classifier at all. Magnitude is not what a rule keys on; sign,
#: currency, date behaviour and the exact-negation relationship are, and those are identical.
AMOUNTS: dict[str, decimal.Decimal] = {
    "ORD-2026-000001": decimal.Decimal("326.92"),
    "ORD-2026-000002": decimal.Decimal("411.55"),
    "ORD-2026-000003": decimal.Decimal("502.18"),
}
SOLO = decimal.Decimal("326.92")
DEBIT_DAY = "2026-06-08"
CREDIT_DAY = "2026-06-10"


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
        for table in ("exception", "match_result", "settlement_line", "settlement_batch"):
            await connection.execute(f"DELETE FROM {table}")
        await connection.execute("DELETE FROM ledger_entry")
    finally:
        await connection.close()
    yield


def _row(reference: str, order: str, movement: str, amount: str, day: str, memo: str) -> str:
    return f"{reference},{order},{movement},{amount},EUR,{day},,,,{memo}"


async def _book_the_debit(order: str, amount: decimal.Decimal) -> None:
    """Give the order's debit a ledger entry so matching reconciles it.

    The counterpart every credit below reverses must be *booked*, because that is what the reversal
    rules require.
    """
    connection = await asyncpg.connect(DSN)
    try:
        entry_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO ledger_entry (id, external_ref, account_code, amount, currency, booked_at)"
            " VALUES ($1, $2, '4900', $3, 'EUR', $4)",
            entry_id,
            f"GL-{order}",
            -amount,
            dt.datetime.fromisoformat(DEBIT_DAY).replace(hour=12, tzinfo=dt.UTC),
        )
    finally:
        await connection.close()


async def _classify_declared_credits(
    engine: AsyncEngine, declared: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Ingest one debit + one credit per case, reconcile, classify, and report each credit's class.

    ``declared`` maps an order id to the ``transaction_type`` its credit row declares. Every other
    field is identical across cases — same sign, same magnitude, same currency, same two dates — so
    the declared type is the only thing that varies.
    """
    lines = [HEADER]
    for order, movement in declared.items():
        amount = AMOUNTS[order]
        lines.append(
            _row(f"psp-{order}-dr", order, "chargeback", f"-{amount}", DEBIT_DAY, "disputed")
        )
        lines.append(_row(f"psp-{order}-cr", order, movement, f"{amount}", CREDIT_DAY, "credit"))
        await _book_the_debit(order, amount)

    payload = ("\n".join(lines) + "\n").encode("utf-8")
    outcome = await ingest(engine, payload, source="file-drop", received_at=RECEIVED_AT)
    assert outcome.accepted, outcome.quarantine_reason

    run = await run_matching(engine, matched_at=MATCHED_AT)
    assert run.matched == len(declared), "each debit must reconcile against its ledger entry"

    await run_classification(engine)

    connection = await asyncpg.connect(DSN)
    try:
        rows = await connection.fetch(
            "SELECT l.merchant_reference AS ord, l.psp_reference, e.classification, e.rule_id"
            " FROM exception e JOIN settlement_line l ON l.id = e.settlement_line_id"
        )
    finally:
        await connection.close()
    return {
        row["ord"]: (row["classification"], row["rule_id"])
        for row in rows
        if row["psp_reference"].endswith("-cr")
    }


# --------------------------------------------------------------------------------------
# The adversarial cases
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credits_of_different_movement_types_are_not_all_chargeback_reversals(
    engine: AsyncEngine,
) -> None:
    """Three credits, identical in every field the old rule could see, three different meanings.

    Each is a positive EUR amount on the same value date, exactly reversing a debit the ledger has
    booked for the same order. They differ only in what the PSP *declared* the movement to be:

    * ``chargeback_reversal`` — the dispute was resolved in the merchant's favour;
    * ``refund_reversal`` — a refund that was itself reversed, which is not a chargeback;
    * ``adjustment`` — an operational correction, which is not a reversal of anything in particular.

    Direction cannot separate these, and the old rule did not try to: it labelled all three
    ``chargeback_reversal`` because a credit reversed a booked debit. Two of those three statements
    were false, and each would have carried a wrong class into a treatment, an approval and a
    posting.
    """
    assigned = await _classify_declared_credits(
        engine,
        {
            "ORD-2026-000001": "chargeback_reversal",
            "ORD-2026-000002": "refund_reversal",
            "ORD-2026-000003": "adjustment",
        },
    )

    assert len(assigned) == 3, "every credit must become an exception"
    classes = {order: klass for order, (klass, _) in assigned.items()}
    assert len(set(classes.values())) > 1, (
        "three different declared movement types were all given the same class: "
        f"{classes} — direction is not evidence of a chargeback"
    )


@pytest.mark.asyncio
async def test_only_a_declared_chargeback_reversal_is_classified_as_one(
    engine: AsyncEngine,
) -> None:
    """The positive and negative halves of the rule, stated as one assertion.

    The declared chargeback reversal is classified; the other two are not, and land in the fallback
    rather than in a neighbouring class. A rule that assigned nothing would pass the test above and
    fail this one.
    """
    assigned = await _classify_declared_credits(
        engine,
        {
            "ORD-2026-000001": "chargeback_reversal",
            "ORD-2026-000002": "refund_reversal",
            "ORD-2026-000003": "adjustment",
        },
    )

    assert assigned["ORD-2026-000001"] == (
        "chargeback_reversal",
        "reversal_of_booked_chargeback",
    )
    assert assigned["ORD-2026-000002"] == ("unclassified", "no_rule_matched")
    assert assigned["ORD-2026-000003"] == ("unclassified", "no_rule_matched")


@pytest.mark.asyncio
async def test_a_declared_chargeback_reversal_without_a_booked_chargeback_is_not_one(
    engine: AsyncEngine,
) -> None:
    """The declaration alone is not enough either, and the rule requires both halves.

    Here the counterpart the ledger booked is a *capture*, not a chargeback, so there is no
    chargeback for this credit to reverse whatever the PSP called it. Evidence has to agree with
    itself: a declared type nobody corroborates is a claim, not a fact.
    """
    order = "ORD-2026-000009"
    payload = (
        "\n".join(
            [
                HEADER,
                _row(f"psp-{order}-dr", order, "capture", f"-{SOLO}", DEBIT_DAY, "capture"),
                _row(
                    f"psp-{order}-cr",
                    order,
                    "chargeback_reversal",
                    f"{SOLO}",
                    CREDIT_DAY,
                    "credit",
                ),
            ]
        )
        + "\n"
    ).encode("utf-8")
    await _book_the_debit(order, SOLO)

    outcome = await ingest(engine, payload, source="file-drop", received_at=RECEIVED_AT)
    assert outcome.accepted, outcome.quarantine_reason
    assert (await run_matching(engine, matched_at=MATCHED_AT)).matched == 1
    await run_classification(engine)

    connection = await asyncpg.connect(DSN)
    try:
        rows = await connection.fetch(
            "SELECT l.psp_reference, e.classification FROM exception e"
            " JOIN settlement_line l ON l.id = e.settlement_line_id"
        )
    finally:
        await connection.close()

    credit = next(row for row in rows if row["psp_reference"].endswith("-cr"))
    assert credit["classification"] == "unclassified"


@pytest.mark.asyncio
async def test_an_unrecognised_movement_type_is_ingested_and_left_unclassified(
    engine: AsyncEngine,
) -> None:
    """A movement type this system has never heard of is data, not a malformed file.

    Quarantining the batch would condemn a whole settlement file because a PSP added a product, and
    a settlement file is not wrong for containing a movement we cannot classify. So it ingests
    normally and the classifier declines: unknown type, no evidence, fallback. Fail-closed at the
    classification boundary rather than at the parsing one.
    """
    order = "ORD-2026-000011"
    payload = (
        "\n".join(
            [
                HEADER,
                _row(f"psp-{order}-dr", order, "chargeback", f"-{SOLO}", DEBIT_DAY, "disputed"),
                _row(
                    f"psp-{order}-cr",
                    order,
                    "instalment_true_up",
                    f"{SOLO}",
                    CREDIT_DAY,
                    "credit",
                ),
            ]
        )
        + "\n"
    ).encode("utf-8")
    await _book_the_debit(order, SOLO)

    outcome = await ingest(engine, payload, source="file-drop", received_at=RECEIVED_AT)
    assert outcome.accepted, "an unknown movement type must not quarantine the batch"
    assert outcome.line_count == 2

    await run_matching(engine, matched_at=MATCHED_AT)
    await run_classification(engine)

    connection = await asyncpg.connect(DSN)
    try:
        rows = await connection.fetch(
            "SELECT l.psp_reference, e.classification FROM exception e"
            " JOIN settlement_line l ON l.id = e.settlement_line_id"
        )
    finally:
        await connection.close()

    credit = next(row for row in rows if row["psp_reference"].endswith("-cr"))
    assert credit["classification"] == "unclassified"
