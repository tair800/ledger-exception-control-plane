"""classified exception provenance and residual integrity

Increment M2.3. Two things the exception table could not say, and one it could not enforce.

**Provenance.** ``rule_id`` and ``classifier_version`` record *which* deterministic rule assigned
a classification and *which revision of the rule set* it belonged to. FR-3 already requires a
matched line to record the rule that matched it; a classified line owes the same answer, and
``unclassified`` is a decision that needs explaining as much as any other. Classification is
deterministic only for a given rule set, so a row carrying the outcome without the ruleset version
says what was decided and nothing about what would decide it the same way again.

**Integrity.** An exception is the control record for a line the ledger did *not* reconcile, and
until now nothing stopped one being written for a matched line — a check constraint cannot
reference another table, and an application check is a convention that holds until someone writes a
second code path. The fix is the pattern ADR-028 arrived at: carry the value and let a composite
foreign key verify it. ``line_match_state`` is pinned to ``unmatched`` by a check constraint and
carried into ``fk_exception_settlement_line``, so the referenced pair is
``(settlement_line.id, 'unmatched')`` and the row can exist only while that is true. The same key
refuses the reverse — marking a line matched while an exception claims it — because both are the
same invariant read from different ends. See ADR-044.

Three deviations from autogenerate output, each load-bearing:

1. **Ordering.** Autogenerate emitted the composite foreign key *before* the unique constraint it
   references, which cannot execute: PostgreSQL requires the referenced column list to be uniquely
   constrained at the moment the key is created. ``uq_settlement_line_id_match_state`` is created
   first here.
2. **``line_match_state`` is backfilled and then verified.** Autogenerate emitted a ``NOT NULL``
   column with no default, which fails outright on a populated table. It is added with a server
   default of ``unmatched``, the default is dropped so the application must state the value, and
   the foreign key created afterwards *proves* the backfill: any pre-existing row whose line is
   matched makes the key creation fail, loudly, rather than quietly asserting something untrue.
3. **``rule_id`` and ``classifier_version`` are not backfilled.** They are added nullable and then
   set ``NOT NULL``, which fails if any row exists. That is deliberate. There is no honest value to
   invent for a classification this rule set did not make, and writing ``unknown`` into financial
   control provenance would be worse than a failed deploy — the row would look explained.

Revision ID: 138145789fda
Revises: 133ac0053abd
Create Date: 2026-08-30 16:07:08.371076
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "138145789fda"
down_revision: str | None = "133ac0053abd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Must exist before the composite key below can reference it.
    op.create_unique_constraint(
        "uq_settlement_line_id_match_state", "settlement_line", ["id", "match_state"]
    )

    op.add_column(
        "exception",
        sa.Column(
            "line_match_state",
            sa.String(length=16),
            nullable=False,
            server_default="unmatched",
        ),
    )
    # The value is the application's to state on every insert; the default existed only to
    # populate rows written before this migration.
    op.alter_column("exception", "line_match_state", server_default=None)

    # Nullable first, then NOT NULL — which fails if a row exists, because no provenance can
    # honestly be invented for a classification this rule set did not make.
    op.add_column("exception", sa.Column("rule_id", sa.String(length=64), nullable=True))
    op.add_column("exception", sa.Column("classifier_version", sa.String(length=32), nullable=True))
    op.alter_column("exception", "rule_id", nullable=False)
    op.alter_column("exception", "classifier_version", nullable=False)

    op.drop_constraint(
        op.f("fk_exception_settlement_line_id_settlement_line"), "exception", type_="foreignkey"
    )
    # Verifies the backfill above as a side effect of existing.
    op.create_foreign_key(
        "fk_exception_settlement_line",
        "exception",
        "settlement_line",
        ["settlement_line_id", "line_match_state"],
        ["id", "match_state"],
        ondelete="RESTRICT",
    )

    op.create_check_constraint(
        op.f("ck_exception_line_is_unmatched"), "exception", "line_match_state = 'unmatched'"
    )
    op.create_check_constraint(
        op.f("ck_exception_rule_id_shape"), "exception", "rule_id ~ '^[a-z][a-z0-9_]{0,63}$'"
    )
    op.create_check_constraint(
        op.f("ck_exception_classifier_version_shape"),
        "exception",
        "classifier_version ~ '^[a-z0-9][a-z0-9.-]{0,31}$'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_exception_classifier_version_shape"), "exception", type_="check")
    op.drop_constraint(op.f("ck_exception_rule_id_shape"), "exception", type_="check")
    op.drop_constraint(op.f("ck_exception_line_is_unmatched"), "exception", type_="check")
    op.drop_constraint("fk_exception_settlement_line", "exception", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_exception_settlement_line_id_settlement_line"),
        "exception",
        "settlement_line",
        ["settlement_line_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_column("exception", "classifier_version")
    op.drop_column("exception", "rule_id")
    op.drop_column("exception", "line_match_state")
    op.drop_constraint("uq_settlement_line_id_match_state", "settlement_line", type_="unique")
