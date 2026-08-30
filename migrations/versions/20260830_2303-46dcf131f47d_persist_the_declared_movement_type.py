"""persist the declared movement type

Increment M2.3, correction. ``settlement_line`` gains ``transaction_type``: what the PSP declared
the movement to be.

**Why this column and not the others.** The approved settlement format (ADR-031) carries ten fields
and ``settlement_line`` stored six of them. M2.1 normalised and validated the rest, then discarded
them, recording that they "remain available in the immutable raw payload for the increments that
need them". This is that increment, and this is that field.

FR-4's taxonomy is a taxonomy of *movement kinds* — partial capture, fee split, chargeback reversal,
cross-period refund. Without the kind, a classifier can only read the sign of the amount, and a
credit reversing a booked debit is equally a chargeback reversal, a fee reversal or an operational
correction. Measured before this migration: three credits identical in sign, amount, currency and
date, differing only in declared type, were all classified ``chargeback_reversal``. Two of those
three statements were false, and each would have carried a wrong class into a treatment, an approval
and a posting.

The other three declared fields — presentment amount, presentment currency, FX rate — are **not**
added. They would not make ``fx_rounding`` reachable, because that class needs the ledger entry
identified and no deterministic key does that. A column that changes no outcome is schema for its
own sake.

**Nullable, and not constrained to a value set.** Nullable because rows ingested before this
migration genuinely have no recorded type; inventing one would be worse than admitting it is
unknown, and the classifier treats absence as no evidence. Unconstrained in value because an
unfamiliar movement type is a product this system has not met, not a malformed file: the receipt is
committed before the payload is read (ADR-041), so a rejected INSERT would strand a batch that can
never reach ``parsed`` or ``quarantined``, and re-delivery would reproduce it forever. Quarantining
instead would condemn a whole settlement file over one unfamiliar row. The closed vocabulary lives
in the classifier, which maps anything it does not recognise to no evidence at all.

The only constraint is that a present value is not blank, because an empty declared type is not a
type and would give the classifier a value to special-case.

**No behaviour changes outside classification.** Matching does not read this column and a scope test
asserts it cannot. Parsing is unchanged except for the storability checks the new column requires —
length and control characters, the same rules the reference fields already carry, and the same
reason: a value this boundary accepts but the column cannot hold turns a quarantine into a jammed
batch.

Revision ID: 46dcf131f47d
Revises: 138145789fda
Create Date: 2026-08-30 23:03:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "46dcf131f47d"
down_revision: str | None = "138145789fda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settlement_line",
        sa.Column("transaction_type", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_settlement_line_transaction_type_not_blank"),
        "settlement_line",
        "transaction_type IS NULL OR length(btrim(transaction_type)) > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_settlement_line_transaction_type_not_blank"),
        "settlement_line",
        type_="check",
    )
    op.drop_column("settlement_line", "transaction_type")
