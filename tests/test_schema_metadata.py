"""Deterministic schema tests — model metadata only, no database required.

These run in the ordinary unit suite. They check properties that are true of the *declared*
schema and can therefore be checked without PostgreSQL: that money is never floating point,
that every amount has a currency, that timestamps are timezone-aware, and that this
increment did not quietly grow M1.2 tables.

Behaviour only a real database can prove — that a constraint actually rejects a bad row,
that the migration applies and reverses — lives in ``test_schema_postgres.py`` behind the
``integration`` marker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Numeric,
    Table,
    UniqueConstraint,
)

from ledger_exception_control_plane.db.base import MONEY_PRECISION, MONEY_SCALE, Base
from ledger_exception_control_plane.db.models import (
    BatchStatus,
    LedgerEntry,
    MatchResult,
    MatchState,
    SettlementBatch,
    SettlementLine,
)

#: The complete set of tables M1.1 is allowed to define.
EXPECTED_TABLES = {"settlement_batch", "settlement_line", "ledger_entry", "match_result"}

#: Tables assigned to M1.2. None may exist yet.
M1_2_TABLES = {
    "exception",
    "evidence",
    "treatment_proposal",
    "approval",
    "adjustment",
    "outbox",
    "posting_attempt",
    "dlq",
    "recovery_queue",
    "audit_event",
}

#: Columns holding a monetary value, and the currency column each must be paired with.
MONEY_COLUMNS = {
    ("settlement_line", "amount"): "currency",
    ("ledger_entry", "amount"): "currency",
    ("match_result", "tolerance_applied"): "tolerance_currency",
}


def _tables() -> dict[str, Table]:
    return dict(Base.metadata.tables)


# --------------------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------------------


def test_metadata_builds_and_models_import() -> None:
    assert set(_tables()) == EXPECTED_TABLES


def test_no_m1_2_tables_were_introduced() -> None:
    """M1.1 must not pre-build the exception, outbox, DLQ or audit tables."""
    leaked = M1_2_TABLES & set(_tables())
    assert not leaked, f"M1.2 tables defined during M1.1: {sorted(leaked)}"


# --------------------------------------------------------------------------------------
# Financial data integrity
# --------------------------------------------------------------------------------------


def test_no_binary_floating_point_anywhere_in_the_schema() -> None:
    """Binary floating point must not appear on any column, monetary or otherwise.

    Checked across the whole metadata rather than only the known money columns, so a future
    column cannot introduce a `Float` without failing here. Binary floating point cannot
    represent 0.10 exactly; a financial system that stores it has already lost.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in _tables().values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert not offenders, f"floating-point columns found: {offenders}"


@pytest.mark.parametrize(("table_name", "column_name"), list(MONEY_COLUMNS))
def test_money_columns_are_exact_numeric_with_declared_precision(
    table_name: str, column_name: str
) -> None:
    column = _tables()[table_name].columns[column_name]

    assert isinstance(column.type, Numeric), f"{table_name}.{column_name} is not NUMERIC"
    assert column.type.precision == MONEY_PRECISION
    assert column.type.scale == MONEY_SCALE
    assert column.type.asdecimal is True
    assert column.type.python_type is Decimal


@pytest.mark.parametrize(("key", "currency_column"), list(MONEY_COLUMNS.items()))
def test_every_monetary_column_has_a_currency_column(
    key: tuple[str, str], currency_column: str
) -> None:
    """An amount without a unit is financially ambiguous, so the pairing is structural."""
    table_name, _ = key
    assert currency_column in _tables()[table_name].columns


@pytest.mark.parametrize(("key", "currency_column"), list(MONEY_COLUMNS.items()))
def test_amount_and_currency_share_nullability(key: tuple[str, str], currency_column: str) -> None:
    """A nullable amount needs a nullable currency, and vice versa.

    Mismatched nullability would let the database hold an amount with no currency, or a
    currency with no amount — the exact ambiguity the pairing constraint exists to prevent.
    """
    table_name, amount_column = key
    table = _tables()[table_name]
    assert table.columns[amount_column].nullable == table.columns[currency_column].nullable


def test_scale_accommodates_every_iso_minor_unit() -> None:
    """Four decimal places, so no currency rounds at the storage boundary.

    JPY has 0 minor digits, most currencies 2, and BHD/KWD/TND have 3; intermediate
    fee-split and FX values commonly need a fourth.
    """
    assert MONEY_SCALE >= 4


# --------------------------------------------------------------------------------------
# Timestamps and identifiers
# --------------------------------------------------------------------------------------


def test_all_timestamp_columns_are_timezone_aware() -> None:
    """Naive timestamps in a financial system are a latent correctness bug."""
    naive = [
        f"{table.name}.{column.name}"
        for table in _tables().values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert not naive, f"naive datetime columns: {naive}"


def test_value_date_is_a_date_not_a_timestamp() -> None:
    """Settlement files state a value date; widening it would invent precision."""
    assert isinstance(_tables()["settlement_line"].columns["value_date"].type, Date)


def test_created_at_is_generated_by_the_database() -> None:
    """A row must carry a creation time even when inserted outside the application."""
    for table in _tables().values():
        column = table.columns["created_at"]
        assert column.server_default is not None, f"{table.name}.created_at has no server default"
        assert column.nullable is False


def test_every_table_has_a_uuid_primary_key() -> None:
    for table in _tables().values():
        primary_key = list(table.primary_key.columns)
        assert len(primary_key) == 1, f"{table.name} has a composite primary key"
        assert primary_key[0].name == "id"
        assert primary_key[0].default is not None, "primary key is application-generated"


# --------------------------------------------------------------------------------------
# Relationships and constraints (declaration; enforcement is proven against PostgreSQL)
# --------------------------------------------------------------------------------------


def test_required_foreign_keys_exist_with_intended_delete_behaviour() -> None:
    expected = {
        ("settlement_line", "settlement_batch_id", "settlement_batch", "CASCADE"),
        ("match_result", "settlement_line_id", "settlement_line", "CASCADE"),
        # RESTRICT, not CASCADE: deleting a ledger entry that a match depends on would
        # silently erase the evidence that a settlement line was reconciled.
        ("match_result", "ledger_entry_id", "ledger_entry", "RESTRICT"),
    }
    actual = {
        (table.name, fk.parent.name, fk.column.table.name, fk.ondelete)
        for table in _tables().values()
        for fk in table.foreign_keys
    }
    assert expected == actual


def test_required_unique_constraints_are_declared() -> None:
    expected = {
        ("settlement_batch", ("content_hash",)),
        ("settlement_line", ("settlement_batch_id", "line_number")),
        ("ledger_entry", ("external_ref",)),
        ("match_result", ("settlement_line_id",)),
        ("match_result", ("ledger_entry_id",)),
    }
    actual = {
        (table.name, tuple(c.name for c in constraint.columns))
        for table in _tables().values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert expected == actual


def test_required_check_constraints_are_declared() -> None:
    expected = {
        "ck_settlement_batch_content_hash_is_sha256_hex",
        "ck_settlement_batch_status_valid",
        "ck_settlement_batch_quarantine_reason_iff_quarantined",
        "ck_settlement_line_line_number_positive",
        "ck_settlement_line_currency_format",
        "ck_settlement_line_match_state_valid",
        "ck_ledger_entry_currency_format",
        "ck_match_result_tolerance_currency_pairing",
        "ck_match_result_tolerance_currency_format",
        "ck_match_result_tolerance_non_negative",
    }
    actual: set[str] = {
        str(constraint.name)
        for table in _tables().values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
    assert expected <= actual, f"missing: {sorted(expected - actual)}"


def test_not_null_rules_on_financially_significant_columns() -> None:
    """Nullable defaults must not create financially ambiguous rows."""
    required_not_null = {
        "settlement_batch": ["content_hash", "source", "raw_payload", "received_at", "status"],
        "settlement_line": [
            "settlement_batch_id",
            "line_number",
            "psp_reference",
            "amount",
            "currency",
            "value_date",
            "match_state",
        ],
        "ledger_entry": ["external_ref", "account_code", "amount", "currency", "booked_at"],
        "match_result": ["settlement_line_id", "ledger_entry_id", "rule_id", "matched_at"],
    }
    for table_name, columns in required_not_null.items():
        for column_name in columns:
            column = _tables()[table_name].columns[column_name]
            assert column.nullable is False, f"{table_name}.{column_name} must be NOT NULL"


def test_only_intended_indexes_exist() -> None:
    """Guards against speculative indexes accumulating."""
    expected = {
        ("settlement_line", ("settlement_batch_id", "match_state")),
        ("ledger_entry", ("account_code", "booked_at")),
    }
    actual = {
        (table.name, tuple(c.name for c in index.columns))
        for table in _tables().values()
        for index in table.indexes
    }
    assert expected == actual


def test_status_enums_are_closed_and_minimal() -> None:
    """M1.1 must not encode workflow states it does not own."""
    assert {s.value for s in BatchStatus} == {"received", "parsed", "quarantined"}
    assert {s.value for s in MatchState} == {"unmatched", "matched"}


def test_models_are_exported() -> None:
    for model in (SettlementBatch, SettlementLine, LedgerEntry, MatchResult):
        assert model.__tablename__ in EXPECTED_TABLES


# --------------------------------------------------------------------------------------
# Engine DSN handling
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("postgres://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("postgresql+asyncpg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
    ],
)
def test_async_dsn_applies_the_driver_without_duplicating_it(
    configured: str, expected: str
) -> None:
    """Configuration holds a plain DSN; SQLAlchemy needs a driver-qualified one.

    Applying the driver here keeps a single DSN in configuration. Re-applying it to an
    already-qualified DSN would produce an unusable scheme, so that case is covered too.
    """
    from pydantic import SecretStr

    from ledger_exception_control_plane.config import Settings
    from ledger_exception_control_plane.db.engine import async_dsn

    settings = Settings(postgres_dsn=SecretStr(configured))
    assert async_dsn(settings) == expected


def test_async_dsn_rejects_a_non_postgres_scheme() -> None:
    """Failing loudly beats handing SQLAlchemy a DSN it cannot use."""
    from pydantic import SecretStr

    from ledger_exception_control_plane.config import Settings
    from ledger_exception_control_plane.db.engine import async_dsn

    settings = Settings(postgres_dsn=SecretStr("mysql://u:p@h/d"))
    with pytest.raises(ValueError, match="postgresql"):
        async_dsn(settings)


def test_async_dsn_error_does_not_echo_the_secret() -> None:
    """The DSN carries a password, so the error message must not repeat it."""
    from pydantic import SecretStr

    from ledger_exception_control_plane.config import Settings
    from ledger_exception_control_plane.db.engine import async_dsn

    settings = Settings(postgres_dsn=SecretStr("mysql://user:hunter2@host/db"))
    with pytest.raises(ValueError) as error:
        async_dsn(settings)
    assert "hunter2" not in str(error.value)
