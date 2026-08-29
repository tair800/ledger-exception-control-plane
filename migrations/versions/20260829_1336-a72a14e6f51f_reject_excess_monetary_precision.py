"""reject excess monetary precision

Corrects a silent-rounding defect in the initial schema.

``NUMERIC(20, 4)`` does not reject an over-precise value — it *rounds* it, before any check
constraint can observe the original. Measured on PostgreSQL 16 against the previous schema:
inserting ``Decimal("1.23456")`` stored ``1.2346``, with no error and no warning, leaving the
application unable to tell the value had changed.

This migration therefore does two things per monetary column, **in this order**:

1. drops the fixed-scale typmod so the original decimal reaches the constraint unchanged;
2. adds checks that *reject* an over-precise, non-numeric or over-large value.

Step 1 is the load-bearing half and is easy to miss: Alembic's autogenerate detected only the
new check constraints, not the type change. Adding the checks without widening the columns
would leave the typmod rounding values before the checks ever ran — a correction that appears
to succeed while changing nothing.

The scale check uses ``trunc``, not ``scale``. ``scale(1.230000)`` is 6, so a scale-based rule
would reject a value numerically identical to ``1.2300`` that loses nothing when stored.
``trunc(v, 4) = v`` is value-based and rejects exactly the values that would lose information.

``NaN`` is excluded explicitly. ``trunc('NaN', 4) = 'NaN'`` is true in PostgreSQL, so the scale
rule alone admits it. Note that ``numeric`` ``NaN`` compares *equal* to itself, unlike IEEE
floats, so ``col = col`` does not detect it and ``col <> 'NaN'`` is used.

Revision ID: a72a14e6f51f
Revises: cf6581793e0c
Create Date: 2026-08-29 13:36:24.876781
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a72a14e6f51f"
down_revision: str | None = "cf6581793e0c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column, nullable) for every monetary column in the M1.1 schema.
MONEY_COLUMNS: Sequence[tuple[str, str, bool]] = (
    ("settlement_line", "amount", False),
    ("ledger_entry", "amount", False),
    ("match_result", "tolerance_applied", True),
)

MAX_SCALE = 4
MAGNITUDE_BOUND = 10**16


def _short_names(column: str) -> tuple[str, str]:
    """Short constraint names; the metadata naming convention expands them.

    Passing an already-expanded ``ck_<table>_...`` name here would double the prefix and
    produce a database name the models do not match, which ``alembic check`` reports as drift.
    """
    stem = "amount" if column == "amount" else "tolerance"
    return f"{stem}_scale", f"{stem}_magnitude"


def upgrade() -> None:
    for table, column, nullable in MONEY_COLUMNS:
        # 1. Widen: drop the fixed scale so an over-precise value survives to the checks.
        #    Widening numeric never loses data, so existing rows are unaffected.
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(precision=20, scale=4),
            type_=sa.Numeric(),
            existing_nullable=nullable,
        )
        # 2. Reject, rather than round. Two constraints, not one, so a violation names its
        #    cause — asyncpg reports the constraint name but not the column.
        scale_name, magnitude_name = _short_names(column)
        op.create_check_constraint(
            scale_name,
            table,
            f"{column} <> 'NaN'::numeric AND trunc({column}, {MAX_SCALE}) = {column}",
        )
        op.create_check_constraint(magnitude_name, table, f"abs({column}) < {MAGNITUDE_BOUND}")


def downgrade() -> None:
    for table, column, nullable in MONEY_COLUMNS:
        scale_name, magnitude_name = _short_names(column)
        # Short names here too: op.drop_constraint applies the same naming convention as
        # create, so passing an already-expanded name doubles the prefix and the DROP fails
        # against a constraint that does not exist. Caught by actually running the downgrade.
        op.drop_constraint(magnitude_name, table, type_="check")
        op.drop_constraint(scale_name, table, type_="check")

        # Narrowing back to a fixed scale restores the original — and restores its
        # silent-rounding behaviour. Refuse rather than round: a downgrade must not be the
        # thing that quietly corrupts a value. The upgrade's checks make such a row
        # impossible to have created, so in practice this never fires; it exists so that if
        # it ever could, it fails loudly instead of destroying data.
        op.execute(
            sa.text(
                f"""
                DO $$
                DECLARE offending bigint;
                BEGIN
                    SELECT count(*) INTO offending FROM {table}
                    WHERE {column} IS NOT NULL
                      AND trunc({column}, {MAX_SCALE}) <> {column};
                    IF offending > 0 THEN
                        RAISE EXCEPTION
                            'downgrade would silently round % row(s) in {table}.{column}; '
                            'quantise them deliberately first', offending;
                    END IF;
                END $$;
                """
            )
        )
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(),
            type_=sa.Numeric(precision=20, scale=4),
            existing_nullable=nullable,
        )
