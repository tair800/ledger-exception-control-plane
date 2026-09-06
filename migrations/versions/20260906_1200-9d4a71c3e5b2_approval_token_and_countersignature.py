"""approval token and countersignature

Increment M5.1. ``approval`` gains the two columns that turn §16's role separation from a policy
sentence into database controls.

**``approval_token`` — single use, enforced by a unique index.** §14 lists *"Replay of a consumed
approval token"* as a named failure whose expected behaviour is *"Rejected; audit event recorded"*,
and §10 says the approve endpoint *"returns the claimed idempotency key"*. Uniqueness is what makes
the replay lose: a check in application code is one refactor away from being skipped, and this one
sits between a human decision and money moving.

Added in three steps rather than one, which is the standard safe shape for a NOT NULL column on a
table that may already hold rows: add nullable, backfill, then tighten. The backfill uses each row's
own primary key, so historical decisions get a token that is unique by construction and obviously
synthetic — it is the row's own id, not a value that could be mistaken for one somebody issued.

**``requested_by`` — the other half of the countersignature.** §16: *"The approver cannot be the
same principal as the requester where an edit changed the treatment."* A CHECK cannot reach another
table, so both principals are carried on the row and compared there. This is the same shape
``recovery_queue`` already uses for its segregation-of-duties rule, and it is deliberate reuse: an
auditor reading either table finds the control in the same place.

Two constraints, and the second is what stops the first being vacuous:

- ``approver_is_not_the_requester`` — ``requested_by IS NULL OR requested_by <> principal``.
- ``requested_by_iff_edited`` — an edit has a requester and nothing else does. Without it, an edit
  that forgot to record who asked would carry ``requested_by IS NULL``, satisfy the first constraint
  trivially, and defeat the very control it was written for.

Revision ID: 9d4a71c3e5b2
Revises: 7c1e9b4f2a08
Create Date: 2026-09-06 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d4a71c3e5b2"
down_revision: str | None = "7c1e9b4f2a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approval", sa.Column("requested_by", sa.String(length=128), nullable=True))
    op.add_column("approval", sa.Column("approval_token", sa.String(length=64), nullable=True))

    # Backfill from the row's own id: unique by construction, and recognisably not an issued token.
    op.execute("UPDATE approval SET approval_token = id::text WHERE approval_token IS NULL")

    op.alter_column("approval", "approval_token", nullable=False)
    op.create_unique_constraint(op.f("uq_approval_token"), "approval", ["approval_token"])

    op.create_check_constraint(
        op.f("ck_approval_approver_is_not_the_requester"),
        "approval",
        "requested_by IS NULL OR requested_by <> principal",
    )
    op.create_check_constraint(
        op.f("ck_approval_requested_by_iff_edited"),
        "approval",
        "(decision = 'edited') = (requested_by IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_approval_requested_by_iff_edited"), "approval", type_="check")
    op.drop_constraint(op.f("ck_approval_approver_is_not_the_requester"), "approval", type_="check")
    op.drop_constraint(op.f("uq_approval_token"), "approval", type_="unique")
    op.drop_column("approval", "approval_token")
    op.drop_column("approval", "requested_by")
