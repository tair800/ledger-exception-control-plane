"""record a transport failure as not_sent

Increment M4.3. The persisted outcome vocabulary gains ``not_sent`` on ``outbox.last_outcome`` and
``posting_attempt.outcome``.

**Why the vocabulary had to widen, rather than reuse a value it already had.** §12.1.1 commits the
write-ahead attempt record *before* the socket write, so an allowlisted transport failure — DNS,
TCP connect, TLS handshake, connect-timeout before first byte — leaves an attempt row behind for a
request that provably never left the client. That row has to say what happened, and none of the five
existing values says it:

- ``in_flight`` (leaving it unresolved) is what a crash *mid-send* leaves, which is the one state
  §13.5 forbids retrying. It would make a safe retry indistinguishable from an ambiguous one, and
  the ambiguity gate added at 4.2 would refuse the retry §15 explicitly permits.
- ``rejected`` asserts the ledger declined. It never received the request. §14 says so in as many
  words: *"Distinct from `Rejected`, which means the ledger declined"*.
- ``unknown`` is the safe default for everything unclassified, and using it here would collapse the
  distinction the enumerated classifier exists to draw — every connect refusal would route to
  manual recovery instead of retrying, and the retry path would be unreachable.

§14 already names the classification (*"Classified `NOT_SENT`"*) and the README's dispatch diagram
already carries it as its own branch. This migration makes the name storable; it does not introduce
a concept.

**The adapter's own outcome union is untouched and stays at exactly five variants.** ``not_sent`` is
not something an adapter can return — it records a failure that happened *instead of* an answer —
so acceptance criterion 8d, which rejects any adapter whose posting type cannot express ``Unknown``,
is unaffected, and the contract test holding that union at five still passes.

**Nothing becomes settleable that was not before.** ``ck_outbox_settled_requires_terminal_outcome``
still admits only ``confirmed`` and ``rejected``, so a ``not_sent`` row cannot be filed as done. It
is dead-lettered on exhaustion instead, which is the state ``DispatchState.DEAD_LETTERED`` has been
waiting for since M1.2.

**Text plus CHECK, not a native enum** (ADR from M1.1): the constraints are dropped and recreated
rather than altered, because a CHECK has no ``ALTER`` and recreating it is the only honest way to
widen it.

**The downgrade rewrites the rows it makes unrepresentable, and rewrites them to what the older
schema actually used for the same event.** The first version simply narrowed the constraint and let
PostgreSQL refuse, which read as protective and was not: ``alembic downgrade base`` failed partway
through with a check violation, leaving the schema half torn down, and no integration module could
reset its database afterwards.

Refusing was also the wrong principle. Before this migration a connect failure left its write-
ahead record ``in_flight`` with no outcome — that is precisely the state the docstring above
criticises, and precisely what the old schema forced. So the downgrade restores it: ``not_sent``
attempts go back to ``in_flight`` with a NULL outcome and NULL resolution, and the outbox row's
``last_outcome`` goes back to NULL. Both remain legal under every other constraint —
``resolved_iff_outcome_recorded`` is satisfied because all three columns move together, and
``posting_ref_requires_applied_outcome`` because a ``not_sent`` attempt never carried a reference.

Nothing is deleted. The rows, the attempt numbers, the send times and the dead letters all survive;
what is lost is the *distinction* this migration introduced, which is exactly what a downgrade of it
should cost and no more.

Revision ID: 7c1e9b4f2a08
Revises: 46dcf131f47d
Create Date: 2026-09-06 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7c1e9b4f2a08"
down_revision: str | None = "46dcf131f47d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The five values the vocabulary held before this migration.
_WITHOUT_NOT_SENT = "'confirmed', 'rejected', 'throttled', 'unknown', 'partially_applied'"

#: The six it holds after.
_WITH_NOT_SENT = _WITHOUT_NOT_SENT + ", 'not_sent'"


def _recreate(values: str) -> None:
    """Rewrite both outcome constraints to admit exactly ``values``.

    One helper for both directions, so the upgrade and the downgrade cannot drift into admitting
    different sets — a mismatch there would make the migration irreversible in practice while
    reporting success.
    """
    op.drop_constraint(op.f("ck_outbox_last_outcome_valid"), "outbox", type_="check")
    op.create_check_constraint(
        op.f("ck_outbox_last_outcome_valid"),
        "outbox",
        f"last_outcome IS NULL OR last_outcome IN ({values})",
    )

    op.drop_constraint(op.f("ck_posting_attempt_outcome_valid"), "posting_attempt", type_="check")
    op.create_check_constraint(
        op.f("ck_posting_attempt_outcome_valid"),
        "posting_attempt",
        f"outcome IS NULL OR outcome IN ({values})",
    )


def upgrade() -> None:
    _recreate(_WITH_NOT_SENT)


def downgrade() -> None:
    # Order matters: the rows must stop carrying the value before the constraint that forbids it is
    # added back, or PostgreSQL refuses the ALTER and the downgrade dies half-applied.
    op.execute(
        """
        UPDATE posting_attempt
           SET state = 'in_flight', outcome = NULL, resolved_at = NULL
         WHERE outcome = 'not_sent'
        """
    )
    op.execute("UPDATE outbox SET last_outcome = NULL WHERE last_outcome = 'not_sent'")
    _recreate(_WITHOUT_NOT_SENT)
