"""Exception, resolution and reliability schema — increment M1.2.

Scope is exactly what ``IMPLEMENTATION_PLAN.md`` §1.2 assigns: ``exception``, ``evidence``,
``treatment_proposal``, ``approval``, ``adjustment``, ``outbox``, ``posting_attempt``,
``dlq``, ``recovery_queue`` and ``audit_event``, plus one association table realising the
``evidence_refs`` field ``PROJECT_SPEC.md`` §6.1 requires on a proposal.

**Structure only.** Nothing here creates exceptions, assembles evidence, calls a model,
computes an amount, dispatches a posting, retries, replays a dead letter, or resolves a
recovery item. Those are M2 through M5. Columns exist so those increments have somewhere to write.

Two invariants from the specification are load-bearing and are enforced by the database
rather than described in prose:

* **The model can never carry money.** ``treatment_proposal`` has no numeric column of any
  kind — no amount, no confidence score, no count. Confidence is a closed band
  (§6.1). A test walks the table and fails if any numeric column appears.
* **``UNKNOWN`` is a first-class outcome and must not collapse into failure.** ``outbox``
  and ``posting_attempt`` can each represent an ambiguous result, and a check constraint
  forbids an ``UNKNOWN`` outbox row from being marked settled (§13.5).
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ledger_exception_control_plane.db.base import (
    Base,
    created_at_column,
    currency_column,
    currency_format_constraint,
    money_column,
    money_magnitude_constraint,
    money_scale_constraint,
    uuid_pk,
)
from ledger_exception_control_plane.db.models import MatchState, SettlementLine

SHA256_HEX = "^[0-9a-f]{64}$"


def _closed(column: str, values: type[enum.StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


# ======================================================================================
# Enumerations — all closed, all stored as text with a check constraint.
#
# Deliberately not PostgreSQL native ENUM types: M1.1 established the text + check pattern,
# and native enums carry an awkward migration lifecycle (a value cannot be removed, and
# ALTER TYPE ... ADD VALUE cannot run inside a transaction in older versions). Consistency
# across the schema is worth more here than the marginal storage saving.
# ======================================================================================


class ExceptionClassification(enum.StrEnum):
    """FR-4's closed taxonomy for a residual line."""

    PARTIAL_CAPTURE = "partial_capture"
    FEE_SPLIT = "fee_split"
    CHARGEBACK_REVERSAL = "chargeback_reversal"
    FX_ROUNDING = "fx_rounding"
    CROSS_PERIOD_REFUND = "cross_period_refund"
    UNCLASSIFIED = "unclassified"


class ExceptionStatus(enum.StrEnum):
    """Whether the exception still needs a decision. Two values, deliberately.

    Richer workflow states belong to the increments that implement the workflow.
    """

    OPEN = "open"
    RESOLVED = "resolved"


class EvidenceKind(enum.StrEnum):
    """The evidence sources FR-5 names."""

    DISPUTE_REASON = "dispute_reason"
    MERCHANT_MEMO = "merchant_memo"
    SUPPORT_TICKET_NOTE = "support_ticket_note"
    REMITTANCE_REFERENCE = "remittance_reference"
    CANDIDATE_LEDGER_ENTRY = "candidate_ledger_entry"


class TreatmentCode(enum.StrEnum):
    """The closed treatment set (§6.1). The *only* channel from model to money path.

    **This is the canonical declaration.** Every other place a treatment appears — the three columns
    below, the account policy, the calculator — refers to it rather than repeating its values, with
    exactly one exception: the two check constraints below spell ``'escalate'`` in SQL, because a
    check constraint cannot import Python. A test asserts both strings against ``ESCALATE.value``,
    so that repetition cannot drift, and another fails if a second treatment vocabulary is ever
    declared anywhere in the package. One declaration is what makes "closed" checkable rather than
    merely intended.

    A treatment is a **categorical instruction and nothing else**. It selects *what to do*, never
    *how much*: the amount, the account and the period all come from deterministic context, and a
    treatment carrying a number — ``write_off_125_50``, ``adjust_by_0_7_percent`` — would be the
    numeric escape hatch the whole containment argument exists to prevent. A guard test asserts no
    member's name or value contains a digit.

    Being a *valid* treatment and being *deterministically priceable* are different contracts, and
    conflating them is how a vocabulary grows. Every member below is always valid; whether M2.4 can
    price it depends on the exception it is applied to, and where it cannot the outcome is
    ``ESCALATE`` rather than a guess (§7) or a new member.

    ``REBOOK``
        Post the movement the ledger is missing, in the period it settled. Priceable wherever the
        exception's class has a configured account (ADR-047).

    ``ACCRUE``
        Recognise the same movement in the period it economically belongs to — the period of the
        movement it reverses. Priceable on the same classes as ``REBOOK``, and only when that
        originating period is known; without one there is nothing to accrue into and it refuses.

    ``WRITE_OFF``
        Recognise the residual as a loss rather than as the movement it appeared to be. Priceable
        wherever an account is configured for it.

    ``ESCALATE``
        Refer the case to a human because it cannot be resolved deterministically. **This is the
        member that closes the set.** Without it, every condition the system cannot price would
        need its own treatment and the vocabulary would grow with the taxonomy; with it, the set of
        *actions* stays at four while the set of *conditions* can grow freely. It is never priced
        and never posted — ``adjustment`` refuses a row for it outright, and the account policy
        refuses to map one.

    Abstention is **not** a fifth member. ``treatment_proposal`` carries a separate ``abstained``
    flag, and a check constraint requires an abstaining proposal to carry ``ESCALATE``: a model that
    declines to answer has still not chosen an action, and giving that its own code would let a
    refusal to decide masquerade as a decision.
    """

    REBOOK = "rebook"
    ACCRUE = "accrue"
    WRITE_OFF = "write_off"
    ESCALATE = "escalate"


class ConfidenceBand(enum.StrEnum):
    """Confidence as a closed band, never a number (§6.1)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalDecision(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class DispatchState(enum.StrEnum):
    """Delivery state of an outbox row — distinct from the *outcome* of an attempt."""

    PENDING = "pending"
    SETTLED = "settled"
    DEAD_LETTERED = "dead_lettered"


class PostingOutcome(enum.StrEnum):
    """The **persisted** outcome vocabulary (§13.4). Never a boolean.

    ``UNKNOWN`` is the reason this enum exists: a timeout after the request was sent is
    indistinguishable from a ledger that committed and lost the response, and coercing it to
    either is the defect the whole design exists to prevent.

    **This vocabulary is one member wider than the adapter's**, and the difference is deliberate.
    An adapter answers with one of five variants (`PROJECT_SPEC.md` §10.1, closed, and a contract
    test holds it at five). ``NOT_SENT`` is not one of them because it is not an answer: it records
    a transport failure that happened *instead of* an answer, classified by the enumerated
    allowlist in :mod:`~ledger_exception_control_plane.ledger.transport`. §14 names it —
    *"Classified `NOT_SENT`; nothing applied; bounded retry, then DLQ. Distinct from `Rejected`,
    which means the ledger declined"* — and the dispatch diagram in the README carries it as its
    own branch.

    It had to become storable at 4.3. The write-ahead record is committed *before* the send, so a
    connect failure leaves a row behind; leaving it ``in_flight`` would make it indistinguishable
    from a crash mid-send and would trip the ambiguity gate that exists to stop exactly that being
    retried, while recording ``rejected`` would assert the ledger declined something it never
    received. Neither is true, so the vocabulary gained the value that is.

    ``NOT_SENT`` can never appear on a *settled* outbox row: ``settled_requires_terminal_outcome``
    admits only ``confirmed`` and ``rejected``, and that constraint is unchanged.
    """

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    THROTTLED = "throttled"
    UNKNOWN = "unknown"
    PARTIALLY_APPLIED = "partially_applied"
    NOT_SENT = "not_sent"


class AttemptState(enum.StrEnum):
    """Write-ahead attempt lifecycle (§12.1.1)."""

    IN_FLIGHT = "in_flight"
    RESOLVED = "resolved"


class ReplayState(enum.StrEnum):
    PENDING = "pending"
    REPLAYED = "replayed"
    ABANDONED = "abandoned"


class RecoveryState(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class RecoveryResolution(enum.StrEnum):
    """The three permitted operator resolutions (§13.5 clause 5).

    ``RESOLVED_UNVERIFIED`` exists so that a judgement made without obtainable evidence is
    *visible* to an auditor rather than indistinguishable from a verified one.
    """

    CONFIRMED_BY_EVIDENCE = "confirmed_by_evidence"
    REJECTED_BY_EVIDENCE = "rejected_by_evidence"
    RESOLVED_UNVERIFIED = "resolved_unverified"


class AuditTool(enum.StrEnum):
    """Operations recorded in the audit trail (§11)."""

    MATCH = "match"
    PROPOSE_TREATMENT = "propose_treatment"
    APPROVE = "approve"
    COMPUTE_AMOUNT = "compute_amount"
    POST = "post"
    RETRY = "retry"
    DLQ = "dlq"
    REPLAY = "replay"


class AuditApprovalDecision(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    NOT_APPLICABLE = "n_a"


class AuditOutcome(enum.StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    ABSTAINED = "abstained"
    QUARANTINED = "quarantined"


# ======================================================================================
# Tables
# ======================================================================================


class ExceptionRecord(Base):
    """A residual settlement line requiring a decision.

    Named ``ExceptionRecord`` in Python because ``Exception`` is a builtin; the table is
    ``exception``, as the specification names it.

    **An exception can only exist for a line the ledger did not reconcile**, and that is a
    referential fact rather than an application convention — see ``line_match_state`` and
    ADR-044.
    """

    __tablename__ = "exception"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: The residual line. The foreign key is declared at table level because it is composite —
    #: see ``fk_exception_settlement_line``.
    #:
    #: RESTRICT, not CASCADE: an exception is the control record for a line that failed to
    #: reconcile. Deleting the line must not erase the evidence that it needed a decision.
    settlement_line_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    #: Pinned to ``unmatched`` by a check constraint, and carried into the composite foreign
    #: key above so the *database* refuses an exception for a line that reconciled. The column
    #: can hold exactly one value, so unlike the denormalised copy ADR-028 had to correct, there
    #: is nothing here for a second code path to get wrong.
    line_match_state: Mapped[MatchState] = mapped_column(
        String(16), nullable=False, default=MatchState.UNMATCHED
    )

    classification: Mapped[ExceptionClassification] = mapped_column(String(32), nullable=False)
    status: Mapped[ExceptionStatus] = mapped_column(
        String(16), nullable=False, default=ExceptionStatus.OPEN
    )

    #: Which deterministic rule assigned the classification, including the fallback. FR-3
    #: requires a matched line to record the rule that matched it; a *classified* line owes the
    #: same answer, and "unclassified" is a decision that needs explaining as much as any other.
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Which version of the rule set produced it. Classification is deterministic only *for a
    #: given rule set*, so without this a later revision makes every historical decision
    #: unexplainable — the row would say what was decided and nothing about what would decide it
    #: the same way again.
    classifier_version: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Spans ingestion → posting (§11). Not nullable: an action with no correlation id cannot
    #: be traced, which defeats the audit trail.
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[dt.datetime] = created_at_column()

    line: Mapped[SettlementLine] = relationship()

    __table_args__ = (
        # FR-4: one exception per residual line. Without this, a re-run of exception creation
        # would produce a second decision path for one line.
        UniqueConstraint("settlement_line_id", name="uq_exception_settlement_line_id"),
        # A check constraint cannot reference another table, so "the line is unmatched" is
        # enforced the way ADR-028 enforces segregation of duties: carry the value and let a
        # composite foreign key verify it. The pinned column makes the pair
        # ``(settlement_line_id, 'unmatched')``, so the row exists only while the line really is
        # unmatched — and the same key refuses the reverse, marking a line matched while an
        # exception claims it. Both directions are the same invariant. See ADR-044.
        ForeignKeyConstraint(
            ["settlement_line_id", "line_match_state"],
            ["settlement_line.id", "settlement_line.match_state"],
            ondelete="RESTRICT",
            name="fk_exception_settlement_line",
        ),
        CheckConstraint(
            f"line_match_state = '{MatchState.UNMATCHED.value}'",
            name="line_is_unmatched",
        ),
        CheckConstraint(
            _closed("classification", ExceptionClassification), name="classification_valid"
        ),
        CheckConstraint(_closed("status", ExceptionStatus), name="status_valid"),
        # Stable machine identifiers, not prose. The permitted shape is constrained rather than
        # the permitted values: the rule set evolves with ``classifier_version``, so enumerating
        # rule ids here would demand a migration for every new rule and would leave rows written
        # under an older set unrepresentable.
        CheckConstraint("rule_id ~ '^[a-z][a-z0-9_]{0,63}$'", name="rule_id_shape"),
        CheckConstraint(
            "classifier_version ~ '^[a-z0-9][a-z0-9.-]{0,31}$'", name="classifier_version_shape"
        ),
        # The analyst queue reads open exceptions; also the claim path
        # (SELECT … FOR UPDATE SKIP LOCKED, §13.1). The PK does not order by status.
        Index("ix_exception_status_created_at", "status", "created_at"),
    )


class Evidence(Base):
    """An addressable evidence record attached to an exception (FR-5).

    The UUID primary key *is* the stable address §6.1 requires for ``evidence_refs``.
    """

    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = uuid_pk()

    # CASCADE here, unlike elsewhere: evidence is a component of its exception and has no
    # independent meaning. The exception itself is protected upstream by RESTRICT, so this
    # cascade cannot be reached by deleting a settlement line.
    exception_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[EvidenceKind] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: Where the evidence came from, e.g. a ticket id. Free text; no credentials.
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    exception: Mapped[ExceptionRecord] = relationship()

    __table_args__ = (
        CheckConstraint(_closed("kind", EvidenceKind), name="kind_valid"),
        # Assembling the evidence pack for one exception is the only read path.
        Index("ix_evidence_exception_id", "exception_id"),
    )


class TreatmentProposalEvidence(Base):
    """Association between a proposal and the evidence it cited (§6.1 ``evidence_refs``).

    A relational association table rather than a ``UUID[]`` column on the proposal. The array
    would keep the table count at ten, but it cannot be referentially checked, and this
    project's rule is to prefer a database-enforced invariant wherever the database can
    express one cleanly. A proposal citing an evidence id that does not exist would be a
    provenance record pointing at nothing — precisely the failure the audit trail exists to
    prevent. This is a realisation of a specified field, not a new business entity.
    """

    __tablename__ = "treatment_proposal_evidence"

    treatment_proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("treatment_proposal.id", ondelete="CASCADE"), primary_key=True
    )
    # RESTRICT: evidence a proposal relied on must not vanish while the proposal survives.
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True
    )


class TreatmentProposal(Base):
    """Model output for one exception — **and never a monetary amount**.

    §6.1 requires the response type to contain no numeric field of any kind, so the model
    cannot emit an amount even accidentally. That containment is mirrored here: this table
    has no ``Numeric``, no ``Integer``, no numeric column at all. Confidence is a closed
    band, not a score. A schema test walks the table and fails if a numeric column appears,
    and a second test fails on an amount-like column name.

    ``rationale`` is provenance for a human reader. No code may parse it, and it is never an
    input to the amount calculator (§6.2).
    """

    __tablename__ = "treatment_proposal"

    id: Mapped[uuid.UUID] = uuid_pk()

    # RESTRICT: a model decision is provenance and must survive.
    exception_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception.id", ondelete="RESTRICT"), nullable=False
    )

    treatment: Mapped[TreatmentCode] = mapped_column(String(16), nullable=False)
    confidence: Mapped[ConfidenceBand] = mapped_column(String(8), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    abstained: Mapped[bool] = mapped_column(nullable=False, default=False)

    #: Provenance for reproducing the call. Text, never numeric.
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cassette_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Where the model call was processed (§11 ``region_jurisdiction``).
    region_jurisdiction: Mapped[str] = mapped_column(String(64), nullable=False)

    proposed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = created_at_column()

    exception: Mapped[ExceptionRecord] = relationship()
    cited_evidence: Mapped[list[Evidence]] = relationship(secondary="treatment_proposal_evidence")

    __table_args__ = (
        CheckConstraint(_closed("treatment", TreatmentCode), name="treatment_valid"),
        CheckConstraint(_closed("confidence", ConfidenceBand), name="confidence_valid"),
        CheckConstraint(f"prompt_hash ~ '{SHA256_HEX}'", name="prompt_hash_is_sha256_hex"),
        # An abstention is not a treatment recommendation: it must escalate, never propose an
        # action the model declined to stand behind.
        CheckConstraint(
            "NOT abstained OR treatment = 'escalate'",
            name="abstention_escalates",
        ),
        Index("ix_treatment_proposal_exception_id", "exception_id"),
    )


class Approval(Base):
    """A human decision on an exception. No ledger write happens without one (FR-7)."""

    __tablename__ = "approval"

    id: Mapped[uuid.UUID] = uuid_pk()

    exception_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exception.id", ondelete="RESTRICT"), nullable=False
    )

    #: Increments when an approved resolution is superseded (§12.1). Part of the operation
    #: identifier, so a corrected resolution is a *different* operation.
    resolution_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Nullable: the model may have been unavailable or may have abstained, and a human can
    #: still decide. RESTRICT so the proposal that informed a decision survives it.
    treatment_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("treatment_proposal.id", ondelete="RESTRICT"), nullable=True
    )

    decision: Mapped[ApprovalDecision] = mapped_column(String(16), nullable=False)

    #: The treatment actually authorised. Absent on a rejection.
    approved_treatment: Mapped[TreatmentCode | None] = mapped_column(String(16), nullable=True)

    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = created_at_column()

    exception: Mapped[ExceptionRecord] = relationship()

    __table_args__ = (
        # §13.1: at most one approved resolution per (exception, resolution_version).
        UniqueConstraint(
            "exception_id", "resolution_version", name="uq_approval_exception_resolution_version"
        ),
        # Redundant on its own — ``id`` is already the primary key — and required anyway: a
        # foreign key must reference a uniquely-constrained column list, and ``adjustment``
        # references this triple so that the decision, the treatment it authorised and the
        # principal who took it are carried into the money path as *verified* values rather
        # than as copies. See the composite key on ``adjustment``.
        UniqueConstraint(
            "id", "approved_treatment", "principal", name="uq_approval_id_treatment_principal"
        ),
        CheckConstraint("resolution_version >= 1", name="resolution_version_positive"),
        CheckConstraint(_closed("decision", ApprovalDecision), name="decision_valid"),
        CheckConstraint(
            "approved_treatment IS NULL OR " + _closed("approved_treatment", TreatmentCode),
            name="approved_treatment_valid",
        ),
        # An approval that authorises nothing, or a rejection that authorises something, are
        # both incoherent records. The database refuses both.
        CheckConstraint(
            "(decision IN ('approved', 'edited')) = (approved_treatment IS NOT NULL)",
            name="approved_treatment_iff_authorising",
        ),
    )


class Adjustment(Base):
    """The computed ledger adjustment for an approved resolution.

    The amount here is **computed deterministically** from settlement and ledger data
    (§6.2); it never originates from model output. It uses the shared money contract, so an
    over-precise value is rejected rather than silently rounded.
    """

    __tablename__ = "adjustment"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: The authority for the write. The foreign key is declared at table level because it is
    #: composite — see ``fk_adjustment_approval``.
    approval_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    #: Carried from the approval, not chosen here. Because the composite foreign key below
    #: includes it, this cannot be set to anything the approval did not actually authorise —
    #: and because it is NOT NULL while ``approval.approved_treatment`` is NULL on a
    #: rejection, no row here can reference a rejection at all.
    approved_treatment: Mapped[TreatmentCode] = mapped_column(String(16), nullable=False)

    #: Also carried from the approval and verified by the same foreign key. It exists so that
    #: ``recovery_queue`` can enforce segregation of duties against a value the database has
    #: checked rather than one the application copied. See ADR-028.
    approving_principal: Mapped[str] = mapped_column(String(128), nullable=False)

    amount: Mapped[decimal.Decimal] = money_column(nullable=False)
    currency: Mapped[str] = currency_column(nullable=False)

    account_code: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Accounting period, ``YYYY-MM``.
    period: Mapped[str] = mapped_column(String(7), nullable=False)

    #: §12.1. Retry-independent, derived from exception id, resolution version and the
    #: instruction payload hash — never from an attempt counter, timestamp or approver.
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Covers everything determining the financial effect (§12.1), so a configuration change
    #: between attempts yields a different operation identifier.
    instruction_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Set only once the ledger confirms. Absent while pending or unknown.
    posting_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    approval: Mapped[Approval] = relationship()

    __table_args__ = (
        # FR-7: no ledger write without a human decision that actually authorised one.
        #
        # A plain foreign key to ``approval.id`` proves only that *an* approval row exists. It
        # does not prove the approval said yes. Referencing the triple instead makes the
        # authorisation itself the referential fact: a rejection carries
        # ``approved_treatment IS NULL``, and this side is NOT NULL, so a rejection can never
        # be matched. No trigger, no application check, no ordering assumption.
        ForeignKeyConstraint(
            ["approval_id", "approved_treatment", "approving_principal"],
            ["approval.id", "approval.approved_treatment", "approval.principal"],
            ondelete="RESTRICT",
            name="fk_adjustment_approval",
        ),
        # §12.2: the database is the guarantee, not application logic.
        UniqueConstraint("operation_id", name="uq_adjustment_operation_id"),
        # One adjustment per approved resolution.
        UniqueConstraint("approval_id", name="uq_adjustment_approval_id"),
        # Both exist to be referenced, and both are redundant as uniqueness claims because
        # ``id`` is the primary key. ``posting_attempt`` and ``recovery_queue`` reference them
        # so that the operation identifier on an attempt record, and the approving principal
        # on a recovery item, are checked rather than trusted.
        UniqueConstraint("id", "operation_id", name="uq_adjustment_id_operation_id"),
        UniqueConstraint("id", "approving_principal", name="uq_adjustment_id_approving_principal"),
        CheckConstraint(f"operation_id ~ '{SHA256_HEX}'", name="operation_id_is_sha256_hex"),
        CheckConstraint(
            f"instruction_payload_hash ~ '{SHA256_HEX}'",
            name="instruction_payload_hash_is_sha256_hex",
        ),
        CheckConstraint("period ~ '^[0-9]{4}-[0-9]{2}$'", name="period_is_year_month"),
        CheckConstraint(_closed("approved_treatment", TreatmentCode), name="treatment_valid"),
        # §6.2: escalation is the outcome *because* the case cannot be priced
        # deterministically. An adjustment for an escalated treatment is therefore a
        # contradiction — an amount computed for the case that was referred precisely because
        # no amount could be computed.
        CheckConstraint("approved_treatment <> 'escalate'", name="escalation_is_never_posted"),
        currency_format_constraint("currency", "currency_format"),
        money_scale_constraint("amount", "amount_scale"),
        money_magnitude_constraint("amount", "amount_magnitude"),
    )


class Outbox(Base):
    """Dispatch intent, written in the same transaction as the approval state change (§13.2).

    Deliberately **at-least-once**: this guarantees the intent is not lost, never that it is
    delivered once. Conflating those is the most common error in this pattern.
    """

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = uuid_pk()

    adjustment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("adjustment.id", ondelete="RESTRICT"), nullable=False
    )

    state: Mapped[DispatchState] = mapped_column(
        String(16), nullable=False, default=DispatchState.PENDING
    )

    #: Last adapter outcome. NULL until a first attempt resolves.
    last_outcome: Mapped[PostingOutcome | None] = mapped_column(String(24), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[dt.datetime] = created_at_column()

    adjustment: Mapped[Adjustment] = relationship()

    __table_args__ = (
        # One dispatch intent per adjustment; a second would be a duplicate write path.
        UniqueConstraint("adjustment_id", name="uq_outbox_adjustment_id"),
        CheckConstraint(_closed("state", DispatchState), name="state_valid"),
        CheckConstraint(
            "last_outcome IS NULL OR " + _closed("last_outcome", PostingOutcome),
            name="last_outcome_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        # A settled row must carry a terminal outcome. This is the constraint that stops
        # UNKNOWN being quietly filed as done: 'unknown', 'throttled' and
        # 'partially_applied' cannot appear on a settled row (§13.5).
        #
        # The IS NOT NULL is load-bearing, not defensive. ``NULL IN ('confirmed',
        # 'rejected')`` evaluates to NULL, and PostgreSQL treats a NULL check result as
        # *satisfied* — so without it, a row could be marked settled carrying no outcome at
        # all: a dispatch recorded as finished with no record of what the ledger said. That
        # is the same defect as filing an UNKNOWN as done, reached by a different route.
        # Found by a real-database test; the expression reads correctly and is wrong.
        CheckConstraint(
            "state <> 'settled' OR "
            "(last_outcome IS NOT NULL AND last_outcome IN ('confirmed', 'rejected'))",
            name="settled_requires_terminal_outcome",
        ),
        # The dispatcher polls due work. Partial index: settled and dead-lettered rows are
        # never scanned, and they will dominate the table over time.
        Index(
            "ix_outbox_pending_next_attempt_at",
            "next_attempt_at",
            postgresql_where=text("state = 'pending'"),
        ),
    )


class PostingAttempt(Base):
    """Write-ahead record of one dispatch attempt (§12.1.1).

    Committed in its own transaction **before** the socket write. On recovery, an
    ``in_flight`` attempt with no recorded response is ``UNKNOWN`` by definition and is never
    retryable. Without this row, a crash between the write and the response is
    indistinguishable from a crash before it, and the system would hold no evidence a send
    occurred at all.

    ``operation_id`` is duplicated here from ``adjustment`` on purpose: the attempt record's
    entire value is being self-contained evidence that survives a crash, and requiring a join
    to interpret it would undermine that.
    """

    __tablename__ = "posting_attempt"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: Composite foreign key at table level — see ``fk_posting_attempt_adjustment``.
    adjustment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)

    sent_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    state: Mapped[AttemptState] = mapped_column(
        String(16), nullable=False, default=AttemptState.IN_FLIGHT
    )
    outcome: Mapped[PostingOutcome | None] = mapped_column(String(24), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posting_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    adjustment: Mapped[Adjustment] = relationship()

    __table_args__ = (
        # RESTRICT: attempt history is the evidence base for recovery and must not be erased.
        #
        # Composite, so the duplicated ``operation_id`` is verified rather than merely copied.
        # An attempt record naming a different operation than the adjustment it belongs to
        # would be worse than no record: recovery reads this row to decide whether an
        # irreversible write may be repeated, and it would be deciding about the wrong
        # operation while looking entirely well-formed.
        ForeignKeyConstraint(
            ["adjustment_id", "operation_id"],
            ["adjustment.id", "adjustment.operation_id"],
            ondelete="RESTRICT",
            name="fk_posting_attempt_adjustment",
        ),
        # Two records for the same attempt would corrupt the recovery evidence.
        UniqueConstraint("adjustment_id", "attempt_no", name="uq_posting_attempt_adjustment_no"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(f"operation_id ~ '{SHA256_HEX}'", name="operation_id_is_sha256_hex"),
        CheckConstraint(_closed("state", AttemptState), name="state_valid"),
        CheckConstraint(
            "outcome IS NULL OR " + _closed("outcome", PostingOutcome), name="outcome_valid"
        ),
        # in_flight means "sent, nothing known": no outcome, no resolution time.
        # resolved means both are recorded. Neither half may drift from the other.
        CheckConstraint(
            "(state = 'resolved') = (outcome IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolved_iff_outcome_recorded",
        ),
        # A resolution cannot precede the send it resolves.
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= sent_at", name="resolved_after_sent"
        ),
        # A posting reference is meaningful only on an attempt the ledger actually applied.
        # Same three-valued trap as the outbox constraint above: with the outcome NULL the
        # IN evaluates to NULL and the check passes, so an in-flight attempt could carry a
        # posting reference for a response that has not arrived.
        CheckConstraint(
            "posting_ref IS NULL OR "
            "(outcome IS NOT NULL AND outcome IN ('confirmed', 'partially_applied'))",
            name="posting_ref_requires_applied_outcome",
        ),
        # Recovery scans unresolved attempts. Partial: resolved attempts are the vast
        # majority and are never the subject of this query.
        Index(
            "ix_posting_attempt_in_flight",
            "sent_at",
            postgresql_where=text("state = 'in_flight'"),
        ),
    )


class DeadLetter(Base):
    """A dispatch that exhausted its retry budget, with the envelope needed to replay it.

    ``envelope`` is JSONB holding **dispatch metadata only** — operation identifier,
    endpoint, adapter name, attempt number, redacted headers. It deliberately does *not*
    carry the monetary amount: storing money in JSONB would bypass the money constraints and
    create exactly the hidden numeric escape hatch the schema forbids elsewhere. The amount
    is reconstructed from ``adjustment`` on replay. A check constraint rejects amount-like
    top-level keys so this cannot erode.
    """

    __tablename__ = "dlq"

    id: Mapped[uuid.UUID] = uuid_pk()

    # RESTRICT: a dead letter is the record of a failure and must outlive tidy-up.
    outbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outbox.id", ondelete="RESTRICT"), nullable=False
    )

    envelope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)

    replay_state: Mapped[ReplayState] = mapped_column(
        String(16), nullable=False, default=ReplayState.PENDING
    )
    replayed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    outbox: Mapped[Outbox] = relationship()

    __table_args__ = (
        # An outbox row dead-letters at most once.
        UniqueConstraint("outbox_id", name="uq_dlq_outbox_id"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(_closed("replay_state", ReplayState), name="replay_state_valid"),
        CheckConstraint(
            "(replay_state = 'replayed') = (replayed_at IS NOT NULL)",
            name="replayed_at_iff_replayed",
        ),
        # No monetary value may hide in the envelope. Keys checked against the same
        # amount-like list §6.1 applies to the model response schema.
        CheckConstraint(
            "NOT (envelope ?| array['amount','value','total','sum','qty','quantity',"
            "'rate','pct','percent','balance','delta','fee','price','cost'])",
            name="envelope_carries_no_monetary_key",
        ),
        # The operator works the pending queue.
        Index(
            "ix_dlq_pending_created_at",
            "created_at",
            postgresql_where=text("replay_state = 'pending'"),
        ),
    )


class RecoveryItem(Base):
    """An ``UNKNOWN`` outcome awaiting reconciliation or an operator decision (§13.5).

    A queue is not a control on its own, so the row carries what the operator must actually
    do: the ``evidence_procedure`` naming which downstream artefact to inspect, an SLA so a
    stale item is alertable, and one of three permitted resolutions — including
    ``resolved_unverified``, which records explicitly that no evidence was obtainable and a
    judgement was made anyway, so it is visible to an auditor rather than indistinguishable
    from a verified resolution.
    """

    __tablename__ = "recovery_queue"

    id: Mapped[uuid.UUID] = uuid_pk()

    #: Composite foreign key at table level — see ``fk_recovery_queue_adjustment``.
    adjustment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    state: Mapped[RecoveryState] = mapped_column(
        String(16), nullable=False, default=RecoveryState.OPEN
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    #: Which downstream artefact must be inspected, and what counts as sufficient evidence.
    evidence_procedure: Mapped[str] = mapped_column(Text, nullable=False)

    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sla_due_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: The principal who approved the adjustment. Present here so that segregation of duties
    #: is expressible as a check constraint — a CHECK cannot reference another table.
    #:
    #: It is not a free copy. The composite foreign key below ties it to
    #: ``adjustment.approving_principal``, which is itself tied to ``approval.principal``, so
    #: the value cannot be set to anything other than the real approver. Without that chain
    #: the check would compare against whatever the writer supplied, and the control would
    #: rest on the same application discipline it exists to replace.
    approving_principal: Mapped[str] = mapped_column(String(128), nullable=False)

    resolution: Mapped[RecoveryResolution | None] = mapped_column(String(32), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = created_at_column()

    adjustment: Mapped[Adjustment] = relationship()

    __table_args__ = (
        # RESTRICT, and composite so the approving principal is verified — see the column note.
        ForeignKeyConstraint(
            ["adjustment_id", "approving_principal"],
            ["adjustment.id", "adjustment.approving_principal"],
            ondelete="RESTRICT",
            name="fk_recovery_queue_adjustment",
        ),
        CheckConstraint(_closed("state", RecoveryState), name="state_valid"),
        CheckConstraint(
            "resolution IS NULL OR " + _closed("resolution", RecoveryResolution),
            name="resolution_valid",
        ),
        # A resolved item records all three of what, who and when — or it is not resolved.
        CheckConstraint(
            "(state = 'resolved') = "
            "(resolution IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolved_iff_fully_recorded",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= opened_at", name="resolved_after_opened"
        ),
        CheckConstraint("sla_due_at > opened_at", name="sla_due_after_opened"),
        # §13.5: the principal resolving an UNKNOWN may not be the one who approved it.
        CheckConstraint(
            "resolved_by IS NULL OR resolved_by <> approving_principal",
            name="segregation_of_duties",
        ),
        # At most one *open* recovery item per adjustment. A partial unique index, because
        # an adjustment may legitimately return to recovery after an earlier item is closed.
        Index(
            "uq_recovery_queue_open_adjustment",
            "adjustment_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
        # Operators work by SLA urgency.
        Index(
            "ix_recovery_queue_open_sla_due_at",
            "sla_due_at",
            postgresql_where=text("state = 'open'"),
        ),
    )


class AuditEvent(Base):
    """Append-only audit record, contract v1 (§11).

    Immutability is enforced by a database trigger, not by convention: an ``UPDATE`` or
    ``DELETE`` raises regardless of which role attempts it. A grant-based control protects
    only roles you remembered to restrict; a trigger protects the table.
    """

    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = uuid_pk()

    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool: Mapped[AuditTool] = mapped_column(String(24), nullable=False)
    scope_granted: Mapped[str] = mapped_column(String(256), nullable=False)
    approval_decision: Mapped[AuditApprovalDecision] = mapped_column(
        String(16), nullable=False, default=AuditApprovalDecision.NOT_APPLICABLE
    )
    approver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    region_jurisdiction: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[AuditOutcome] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[dt.datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(_closed("tool", AuditTool), name="tool_valid"),
        CheckConstraint(
            _closed("approval_decision", AuditApprovalDecision), name="approval_decision_valid"
        ),
        CheckConstraint(_closed("outcome", AuditOutcome), name="outcome_valid"),
        # Reconstructing the history of one request is the audit trail's whole purpose.
        Index("ix_audit_event_correlation_id_occurred_at", "correlation_id", "occurred_at"),
    )
