"""Core reconciliation schema — the four tables assigned to increment M1.1.

Scope is exactly what ``IMPLEMENTATION_PLAN.md`` §1.1 lists: ``settlement_batch``,
``settlement_line``, ``ledger_entry`` and ``match_result``. The exception, evidence,
proposal, approval, adjustment, outbox, posting-attempt, DLQ, recovery-queue and
audit-event tables belong to M1.2 and are deliberately absent — a test asserts that.

**This module defines structure, not behaviour.** No parsing, no normalisation, no matching,
no tolerance arithmetic, no classification. Those are M2. Columns exist here so the later
increments have somewhere to write; nothing populates them yet.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ledger_exception_control_plane.db.base import (
    Base,
    created_at_column,
    currency_column,
    currency_format_constraint,
    currency_pairing_constraint,
    money_column,
    money_magnitude_constraint,
    money_scale_constraint,
    uuid_pk,
)


class BatchStatus(enum.StrEnum):
    """Lifecycle of an ingested settlement batch.

    Deliberately three values. ``PARSED`` is the terminal success state *for ingestion*;
    what happens to the lines afterwards is tracked on the lines, not here. Residual and
    exception states belong to M1.2 and are not represented.
    """

    RECEIVED = "received"
    PARSED = "parsed"
    QUARANTINED = "quarantined"


class MatchState(enum.StrEnum):
    """Whether a settlement line has been matched to a ledger entry.

    Only two values at M1.1. A line that fails to match becomes an *exception*, and the
    exception table is M1.2 — so there is no ``RESIDUAL`` state here to avoid encoding a
    workflow this increment does not own.
    """

    UNMATCHED = "unmatched"
    MATCHED = "matched"


class SettlementBatch(Base):
    """A settlement file as received from the PSP, stored immutably with its content hash.

    FR-1 requires the raw payload to be persisted *before* parsing, and re-delivery of an
    identical batch not to create duplicate work. The unique ``content_hash`` is what makes
    the second half true, enforced by the database rather than by a pre-insert lookup that
    two concurrent deliveries could both pass.
    """

    __tablename__ = "settlement_batch"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: SHA-256 of the raw payload, lower-case hex. Unique: this is the re-delivery guard.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Where the batch came from, e.g. a webhook endpoint name or a drop location.
    source: Mapped[str] = mapped_column(String(128), nullable=False)

    #: The bytes exactly as received. Immutable; never rewritten after insert.
    raw_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: When the batch arrived. Application-supplied: only the ingest path knows the real
    #: arrival time, which may differ from row-insert time on replay.
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[BatchStatus] = mapped_column(
        String(16), nullable=False, default=BatchStatus.RECEIVED
    )

    #: Populated only when quarantined, and required then — see the check constraint.
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    lines: Mapped[list[SettlementLine]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Re-delivery of an identical payload must be a no-op (FR-1). In the database
        # because two concurrent deliveries can both pass an application-level check.
        UniqueConstraint("content_hash", name="uq_settlement_batch_content_hash"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="content_hash_is_sha256_hex",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(s.value) for s in BatchStatus)})",
            name="status_valid",
        ),
        # A quarantined batch without a reason is an unactionable dead end, and a reason on
        # a healthy batch is a contradiction. FR-2 requires the reason, so the database
        # requires it too.
        CheckConstraint(
            "(status = 'quarantined') = (quarantine_reason IS NOT NULL)",
            name="quarantine_reason_iff_quarantined",
        ),
    )


class SettlementLine(Base):
    """One normalised line within a settlement batch.

    Every monetary value carries its currency explicitly (FR-2). Amounts are signed —
    refunds, chargeback reversals and fee deductions are legitimately negative — so no sign
    constraint is imposed.
    """

    __tablename__ = "settlement_line"

    id: Mapped[uuid.UUID] = uuid_pk()

    settlement_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("settlement_batch.id", ondelete="CASCADE"), nullable=False
    )

    #: Position within the batch file, 1-based. Unique per batch.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The PSP's identifier for this movement.
    psp_reference: Mapped[str] = mapped_column(String(128), nullable=False)

    #: The merchant's own reference, where the PSP passes one through.
    merchant_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)

    amount: Mapped[decimal.Decimal] = money_column(nullable=False)
    currency: Mapped[str] = currency_column(nullable=False)

    #: Business value date from the settlement file. A date, not a timestamp: settlement
    #: files state a value date, and widening it to a timestamp would invent precision.
    value_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    match_state: Mapped[MatchState] = mapped_column(
        String(16), nullable=False, default=MatchState.UNMATCHED
    )

    created_at: Mapped[dt.datetime] = created_at_column()

    batch: Mapped[SettlementBatch] = relationship(back_populates="lines")

    __table_args__ = (
        # A batch cannot contain two line 7s. Protects against a partial re-parse writing
        # duplicate lines for the same source row.
        UniqueConstraint(
            "settlement_batch_id", "line_number", name="uq_settlement_line_batch_line_number"
        ),
        # Redundant as a uniqueness claim — ``id`` is already the primary key — and required
        # anyway, because a foreign key must reference a uniquely-constrained column list.
        # ``exception`` references this pair so that "the line this exception was raised for is
        # unmatched" is a *referential* fact rather than an application convention. See the
        # composite key on ``exception`` and ADR-044.
        UniqueConstraint("id", "match_state", name="uq_settlement_line_id_match_state"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        currency_format_constraint("currency", "currency_format"),
        # Reject an over-precise amount instead of letting storage round it. Split in two
        # so a rejection names its cause: asyncpg reports the constraint, not the column.
        money_scale_constraint("amount", "amount_scale"),
        money_magnitude_constraint("amount", "amount_magnitude"),
        CheckConstraint(
            f"match_state IN ({', '.join(repr(s.value) for s in MatchState)})",
            name="match_state_valid",
        ),
        # Supports the matcher's primary access pattern: pull the unmatched lines of a
        # batch. Required now rather than speculative — it is the only way M2 reads this
        # table, and a sequential scan over a full batch is the default without it.
        Index("ix_settlement_line_batch_match_state", "settlement_batch_id", "match_state"),
    )


class LedgerEntry(Base):
    """A general-ledger row available for matching, as of a snapshot.

    Read-only from this system's perspective: the ledger is an external system behind an
    adapter, and these rows are a local snapshot to match against.
    """

    __tablename__ = "ledger_entry"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: The ledger's own identifier for the entry. Unique: importing the same snapshot twice
    #: must not create two matchable copies of one entry, which would let a settlement line
    #: match a duplicate.
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Ledger account the entry is booked against.
    account_code: Mapped[str] = mapped_column(String(64), nullable=False)

    amount: Mapped[decimal.Decimal] = money_column(nullable=False)
    currency: Mapped[str] = currency_column(nullable=False)

    #: When the ledger booked the entry. Application-supplied from the source system.
    booked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("external_ref", name="uq_ledger_entry_external_ref"),
        currency_format_constraint("currency", "currency_format"),
        money_scale_constraint("amount", "amount_scale"),
        money_magnitude_constraint("amount", "amount_magnitude"),
        # The matcher looks up candidates by account and booking time. Justified now for the
        # same reason as the settlement-line index: it is M2's only read path into this table.
        Index("ix_ledger_entry_account_code_booked_at", "account_code", "booked_at"),
    )


class MatchResult(Base):
    """A settlement line matched to a ledger entry, with the rule that produced it.

    FR-3 requires recording, for every line, which rule matched it. The row exists only for
    a successful match; an unmatched line becomes an exception, which is M1.2.

    ``tolerance_applied`` records the monetary difference the matching rule accepted. It is
    nullable — an exact match applies no tolerance — and is therefore paired with its own
    nullable currency under a both-or-neither constraint.
    """

    __tablename__ = "match_result"

    id: Mapped[uuid.UUID] = uuid_pk()

    settlement_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("settlement_line.id", ondelete="CASCADE"), nullable=False
    )
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_entry.id", ondelete="RESTRICT"), nullable=False
    )

    #: Identifier of the deterministic rule that produced the match, recorded so any match
    #: can be explained after the fact.
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)

    tolerance_applied: Mapped[decimal.Decimal | None] = money_column(nullable=True)
    tolerance_currency: Mapped[str | None] = currency_column(nullable=True)

    matched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[dt.datetime] = created_at_column()

    line: Mapped[SettlementLine] = relationship()
    entry: Mapped[LedgerEntry] = relationship()

    __table_args__ = (
        # A settlement line matches at most one ledger entry. A line requiring several
        # entries is a split, which is residual by definition and becomes an exception.
        UniqueConstraint("settlement_line_id", name="uq_match_result_settlement_line_id"),
        # And a ledger entry is consumed at most once. Without this, two settlement lines
        # could both claim the same entry and the ledger would appear to reconcile twice —
        # a silent double-count, which is the class of error this project exists to prevent.
        UniqueConstraint("ledger_entry_id", name="uq_match_result_ledger_entry_id"),
        currency_pairing_constraint(
            "tolerance_applied",
            "tolerance_currency",
            "tolerance_currency_pairing",
        ),
        CheckConstraint(
            "tolerance_currency IS NULL OR tolerance_currency ~ '^[A-Z]{3}$'",
            name="tolerance_currency_format",
        ),
        # A negative tolerance is not a tolerance. The magnitude of the difference is what
        # was accepted, so it cannot be below zero.
        CheckConstraint(
            "tolerance_applied IS NULL OR tolerance_applied >= 0",
            name="tolerance_non_negative",
        ),
        # NULL passes a bare CHECK, so the nullable tolerance needs no guard clause here.
        money_scale_constraint("tolerance_applied", "tolerance_scale"),
        money_magnitude_constraint("tolerance_applied", "tolerance_magnitude"),
    )
