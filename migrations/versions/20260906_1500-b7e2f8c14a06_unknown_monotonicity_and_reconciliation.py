"""unknown monotonicity, reconciliation evidence and the supersession interlock

Increment M4.4. Four changes, and every one of them moves a rule out of application code and into
the database, because each guards a decision that ends in an irreversible financial write.

**1. ``reconciliation_query`` — the evidence behind resolving an ``UNKNOWN``.** `PROJECT_SPEC.md`
§13.5 permits resolving to ``REJECTED`` only after both declared windows have elapsed *"observed on
N consecutive queries"*, and requires that *"the windows used, the query results observed and the
resolution reached"* be recorded so an auditor can reconstruct why a resolution was considered safe.

A mutable counter would satisfy the letter of that and none of its purpose: the number that decides
whether an ambiguous financial write may be declared un-applied would be a value somebody can set.
So the count is **derived** from an append-only table, guarded by the same trigger shape
``audit_event`` uses, and the windows are stamped on each row as the declarations in force when that
question was asked.

**2. Monotonic transitions.** §13.5 clause 4: *"``UNKNOWN → CONFIRMED`` and ``UNKNOWN → REJECTED``
are permitted; ``CONFIRMED → anything`` is not"*, and clause 6: *"``UNKNOWN`` is never
overwritten in place; resolution is an appended transition."* Two triggers:

- ``posting_attempt`` is **evidence about one send** and a recorded outcome is immutable. The row
  that saw an ambiguity keeps it forever; the resolution lands on ``outbox``, which is the
  current-state pointer, and on the appended rows above.
- ``outbox.last_outcome`` may not move off a terminal value, and a settled row may not un-settle.

**3. The supersession interlock.** §12.1: a new ``resolution_version`` may not be approved while a
prior operation on the same exception is ``IN_FLIGHT``, ``UNKNOWN`` or open in recovery. A CHECK
cannot reach another table and this rule spans four, so it is a trigger — and it is a trigger rather
than a service check because the failure it prevents is two live resolutions for one exception, each
able to post money.

The interlock releases when the prior ambiguity has been **adjudicated**: an open recovery item
blocks, and a closed one does not. That is deliberate and it is the one place here where the
database defers to a human — including for ``RESOLVED_UNVERIFIED``, where the operator has recorded
that no evidence was obtainable. Superseding after an unverified resolution can still double-post if
the original did in fact apply; the design makes that judgement *visible and attributable* rather
than safe, and claiming otherwise would be the kind of unconditional guarantee this project exists
to avoid.

**4. Two new audit verbs.** ``reconcile`` and ``recover``. Querying a ledger about a posting and
judging what happened to one are neither ``post`` nor ``approve``, and folding them into either
would make the segregation of duties unreadable in the trail.

Revision ID: b7e2f8c14a06
Revises: 9d4a71c3e5b2
Create Date: 2026-09-06 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2f8c14a06"
down_revision: str | None = "9d4a71c3e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TOOLS_BEFORE = (
    "tool IN ('match', 'propose_treatment', 'approve', 'compute_amount', 'post', "
    "'retry', 'dlq', 'replay')"
)
_TOOLS_AFTER = (
    "tool IN ('match', 'propose_treatment', 'approve', 'compute_amount', 'post', "
    "'retry', 'dlq', 'replay', 'reconcile', 'recover')"
)


def upgrade() -> None:
    # ------------------------------------------------------------------------------
    # 1. Reconciliation evidence
    # ------------------------------------------------------------------------------
    op.create_table(
        "reconciliation_query",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("adjustment_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("query_no", sa.Integer(), nullable=False),
        sa.Column("queried_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answer", sa.String(length=16), nullable=False),
        sa.Column("posting_ref", sa.String(length=128), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("visibility_bound", sa.Interval(), nullable=False),
        sa.Column("max_inflight_window", sa.Interval(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "answer IN ('found', 'not_found', 'indeterminate')",
            name=op.f("ck_reconciliation_query_answer_valid"),
        ),
        sa.CheckConstraint(
            "operation_id ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_reconciliation_query_operation_id_is_sha256_hex"),
        ),
        sa.CheckConstraint(
            "(answer = 'found') = (posting_ref IS NOT NULL)",
            name=op.f("ck_reconciliation_query_posting_ref_iff_found"),
        ),
        sa.CheckConstraint("query_no >= 1", name=op.f("ck_reconciliation_query_query_no_positive")),
        sa.CheckConstraint(
            "visibility_bound >= interval '0' AND max_inflight_window >= interval '0'",
            name=op.f("ck_reconciliation_query_windows_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_id", "operation_id"],
            ["adjustment.id", "adjustment.operation_id"],
            name="fk_reconciliation_query_adjustment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_query")),
        sa.UniqueConstraint("adjustment_id", "query_no", name=op.f("uq_reconciliation_query_no")),
    )
    op.create_index(
        "ix_reconciliation_query_adjustment_no",
        "reconciliation_query",
        ["adjustment_id", "query_no"],
        unique=False,
    )

    # Append-only, for the reason the table exists: the count of consecutive negative answers is
    # the safety argument for declaring an ambiguous financial write un-applied, and an argument
    # made of editable rows is not one.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION reconciliation_query_reject_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION
                    'reconciliation_query is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER reconciliation_query_append_only_row
            BEFORE UPDATE OR DELETE ON reconciliation_query
            FOR EACH ROW EXECUTE FUNCTION reconciliation_query_reject_mutation();
            """
        )
    )
    # TRUNCATE bypasses row-level triggers, so a table that permits it is not append-only.
    op.execute(
        sa.text(
            """
            CREATE TRIGGER reconciliation_query_append_only_truncate
            BEFORE TRUNCATE ON reconciliation_query
            FOR EACH STATEMENT EXECUTE FUNCTION reconciliation_query_reject_mutation();
            """
        )
    )

    # ------------------------------------------------------------------------------
    # 1b. Where an attempt was sent
    #
    # Nullable on purpose. A backfilled placeholder would be a fiction that the scope comparison
    # then treats as a recorded endpoint; NULL says plainly that the original send's endpoint is
    # unknown, and 4.4 refuses the bounded re-send on that basis.
    # ------------------------------------------------------------------------------
    op.add_column("posting_attempt", sa.Column("endpoint", sa.String(length=256), nullable=True))

    # ------------------------------------------------------------------------------
    # 2. Monotonic transitions
    # ------------------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE FUNCTION posting_attempt_enforce_immutable_evidence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.outcome IS NOT NULL AND NEW.outcome IS DISTINCT FROM OLD.outcome THEN
                    RAISE EXCEPTION
                        'posting_attempt %: a recorded outcome (%) is immutable; resolution of an '
                        'ambiguous send is an appended transition, not an edit (PROJECT_SPEC 13.5)',
                        OLD.id, OLD.outcome
                        USING ERRCODE = 'restrict_violation';
                END IF;

                IF OLD.state = 'resolved' AND NEW.state <> 'resolved' THEN
                    RAISE EXCEPTION
                        'posting_attempt %: a resolved attempt cannot return to in_flight',
                        OLD.id
                        USING ERRCODE = 'restrict_violation';
                END IF;

                IF NEW.adjustment_id <> OLD.adjustment_id
                   OR NEW.operation_id <> OLD.operation_id
                   OR NEW.attempt_no <> OLD.attempt_no
                   OR NEW.sent_at <> OLD.sent_at THEN
                    RAISE EXCEPTION
                        'posting_attempt %: identity and send time are the evidence and are '
                        'immutable', OLD.id
                        USING ERRCODE = 'restrict_violation';
                END IF;

                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER posting_attempt_immutable_evidence
            BEFORE UPDATE ON posting_attempt
            FOR EACH ROW EXECUTE FUNCTION posting_attempt_enforce_immutable_evidence();
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION outbox_enforce_monotonic_outcome() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.last_outcome IN ('confirmed', 'rejected')
                   AND NEW.last_outcome IS DISTINCT FROM OLD.last_outcome THEN
                    RAISE EXCEPTION
                        'outbox %: outcome % is terminal and cannot be changed to %; '
                        'CONFIRMED to anything is not a permitted transition '
                        '(PROJECT_SPEC 13.5)',
                        OLD.id, OLD.last_outcome, COALESCE(NEW.last_outcome, 'NULL')
                        USING ERRCODE = 'restrict_violation';
                END IF;

                IF OLD.state = 'settled' AND NEW.state <> 'settled' THEN
                    RAISE EXCEPTION
                        'outbox %: a settled dispatch cannot be reopened', OLD.id
                        USING ERRCODE = 'restrict_violation';
                END IF;

                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER outbox_monotonic_outcome
            BEFORE UPDATE ON outbox
            FOR EACH ROW EXECUTE FUNCTION outbox_enforce_monotonic_outcome();
            """
        )
    )

    # ------------------------------------------------------------------------------
    # 3. The supersession interlock
    # ------------------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            CREATE FUNCTION approval_enforce_supersession_interlock() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE
                blocking_operation text;
            BEGIN
                SELECT adj.operation_id INTO blocking_operation
                FROM approval prior
                JOIN adjustment adj ON adj.approval_id = prior.id
                LEFT JOIN outbox o ON o.adjustment_id = adj.id
                WHERE prior.exception_id = NEW.exception_id
                  AND prior.resolution_version < NEW.resolution_version
                  AND (
                        EXISTS (
                            SELECT 1 FROM posting_attempt pa
                            WHERE pa.adjustment_id = adj.id AND pa.state = 'in_flight'
                        )
                     OR EXISTS (
                            SELECT 1 FROM recovery_queue rq
                            WHERE rq.adjustment_id = adj.id AND rq.state = 'open'
                        )
                     OR (
                            o.last_outcome IN ('unknown', 'partially_applied')
                            AND NOT EXISTS (
                                SELECT 1 FROM recovery_queue rq2
                                WHERE rq2.adjustment_id = adj.id
                            )
                        )
                  )
                LIMIT 1;

                IF blocking_operation IS NOT NULL THEN
                    RAISE EXCEPTION
                        'exception %: resolution version % cannot be approved while operation % '
                        'is in flight, ambiguous or open in recovery (PROJECT_SPEC 12.1)',
                        NEW.exception_id, NEW.resolution_version, left(blocking_operation, 12)
                        USING ERRCODE = 'restrict_violation';
                END IF;

                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER approval_supersession_interlock
            BEFORE INSERT ON approval
            FOR EACH ROW EXECUTE FUNCTION approval_enforce_supersession_interlock();
            """
        )
    )

    # ------------------------------------------------------------------------------
    # 4. Two new audit verbs
    # ------------------------------------------------------------------------------
    op.drop_constraint(op.f("ck_audit_event_tool_valid"), "audit_event", type_="check")
    op.create_check_constraint(op.f("ck_audit_event_tool_valid"), "audit_event", _TOOLS_AFTER)


def downgrade() -> None:
    # ------------------------------------------------------------------------------
    # Refuse rather than fail obscurely, and refuse rather than destroy evidence.
    #
    # Re-narrowing the vocabulary is impossible while events carrying the new verbs exist: the
    # constraint would be violated by rows that are real history. The obvious escape — delete them
    # — is both wrong and unavailable, because `audit_event` is append-only and its trigger refuses
    # `DELETE` to every role including the owner.
    #
    # So the downgrade stops and says what it found. An operator who genuinely wants the older
    # schema has to decide, deliberately, what to do about an audit trail that records actions the
    # older schema cannot express — which is exactly the decision a migration must not make on
    # their behalf.
    # ------------------------------------------------------------------------------
    recorded = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM audit_event WHERE tool IN ('reconcile', 'recover')"))
        .scalar_one()
    )
    if recorded:
        raise RuntimeError(
            f"refusing to downgrade: {recorded} audit event(s) record a 'reconcile' or 'recover' "
            "action, which the previous vocabulary cannot express. The table is append-only, so "
            "they cannot be removed here; decide explicitly what should happen to that history "
            "before narrowing the constraint."
        )

    op.drop_constraint(op.f("ck_audit_event_tool_valid"), "audit_event", type_="check")
    op.create_check_constraint(op.f("ck_audit_event_tool_valid"), "audit_event", _TOOLS_BEFORE)

    op.execute(sa.text("DROP TRIGGER IF EXISTS approval_supersession_interlock ON approval"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS approval_enforce_supersession_interlock()"))

    op.execute(sa.text("DROP TRIGGER IF EXISTS outbox_monotonic_outcome ON outbox"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS outbox_enforce_monotonic_outcome()"))

    op.execute(
        sa.text("DROP TRIGGER IF EXISTS posting_attempt_immutable_evidence ON posting_attempt")
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS posting_attempt_enforce_immutable_evidence()"))

    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS reconciliation_query_append_only_truncate "
            "ON reconciliation_query"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS reconciliation_query_append_only_row ON reconciliation_query"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS reconciliation_query_reject_mutation()"))

    op.drop_column("posting_attempt", "endpoint")

    op.drop_index("ix_reconciliation_query_adjustment_no", table_name="reconciliation_query")
    op.drop_table("reconciliation_query")
