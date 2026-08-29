"""Deterministic schema tests — model metadata only, no database required.

These run in the ordinary unit suite. They check properties that are true of the *declared*
schema and can therefore be checked without PostgreSQL: that money is never floating point,
that every amount has a currency, that timestamps are timezone-aware, that the model output
table cannot carry a number, and that this increment did not quietly grow M2+ tables.

Behaviour only a real database can prove — that a constraint actually rejects a bad row,
that the migration applies and reverses — lives in ``test_schema_postgres.py`` behind the
``integration`` marker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    SmallInteger,
    Table,
    UniqueConstraint,
)
from sqlalchemy.types import TypeDecorator

from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_MAX_SCALE,
    Base,
    Money,
)
from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    ApprovalDecision,
    AttemptState,
    AuditEvent,
    ConfidenceBand,
    DeadLetter,
    DispatchState,
    Evidence,
    EvidenceKind,
    ExceptionClassification,
    ExceptionRecord,
    ExceptionStatus,
    Outbox,
    PostingAttempt,
    PostingOutcome,
    RecoveryItem,
    RecoveryResolution,
    RecoveryState,
    ReplayState,
    TreatmentCode,
    TreatmentProposal,
    TreatmentProposalEvidence,
)
from ledger_exception_control_plane.db.models import (
    BatchStatus,
    LedgerEntry,
    MatchResult,
    MatchState,
    SettlementBatch,
    SettlementLine,
)

#: The tables M1.1 defined.
M1_1_TABLES = {"settlement_batch", "settlement_line", "ledger_entry", "match_result"}

#: The tables M1.2 adds. Ten come straight from ``IMPLEMENTATION_PLAN.md`` §1.2;
#: ``treatment_proposal_evidence`` realises the ``evidence_refs`` field required by
#: ``PROJECT_SPEC.md`` §6.1 — see ``ASSOCIATION_TABLES``.
M1_2_TABLES = {
    "exception",
    "evidence",
    "treatment_proposal",
    "treatment_proposal_evidence",
    "approval",
    "adjustment",
    "outbox",
    "posting_attempt",
    "dlq",
    "recovery_queue",
    "audit_event",
}

#: The complete set of tables the schema is allowed to define at M1.2.
EXPECTED_TABLES = M1_1_TABLES | M1_2_TABLES

#: Pure link tables. They legitimately have neither a surrogate primary key nor a creation
#: timestamp of their own, so the two "every table" invariants below exempt them. The
#: exemption is not a hole: ``test_association_table_shape_is_pinned`` fixes their shape
#: exactly, so nothing can hide here.
ASSOCIATION_TABLES = {"treatment_proposal_evidence"}

#: A sample of tables belonging to increments after M1.2. None may exist yet. Names are
#: indicative rather than specified — the plan does not name M2+ tables — so this guard is a
#: smoke alarm for scope creep, not a contract.
LATER_TABLES = {
    "cassette",
    "golden_case",
    "eval_run",
    "approval_token",
    "tolerance_band",
    "adapter_capability",
}

#: Columns holding a monetary value, and the currency column each must be paired with.
MONEY_COLUMNS = {
    ("settlement_line", "amount"): "currency",
    ("ledger_entry", "amount"): "currency",
    ("match_result", "tolerance_applied"): "tolerance_currency",
    ("adjustment", "amount"): "currency",
}

#: Every SQLAlchemy type that can hold a number. Used by the containment test below; listed
#: explicitly rather than inferred, so a new numeric type cannot slip past by not being
#: recognised.
NUMERIC_TYPES = (Numeric, Float, Integer, SmallInteger, BigInteger)


def _tables() -> dict[str, Table]:
    return dict(Base.metadata.tables)


# --------------------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------------------


def test_metadata_builds_and_models_import() -> None:
    assert set(_tables()) == EXPECTED_TABLES


def test_every_m1_2_table_exists() -> None:
    """The ten tables ``IMPLEMENTATION_PLAN.md`` §1.2 assigns, plus the link table."""
    missing = M1_2_TABLES - set(_tables())
    assert not missing, f"M1.2 tables not defined: {sorted(missing)}"


def test_no_later_increment_tables_were_introduced() -> None:
    """M1.2 must not pre-build tables belonging to a later increment."""
    leaked = LATER_TABLES & set(_tables())
    assert not leaked, f"post-M1.2 tables defined during M1.2: {sorted(leaked)}"


def test_association_table_shape_is_pinned() -> None:
    """The exemption granted to link tables is narrow and cannot widen unnoticed."""
    for name in ASSOCIATION_TABLES:
        table = _tables()[name]
        assert {c.name for c in table.columns} == {"treatment_proposal_id", "evidence_id"}
        assert {c.name for c in table.primary_key.columns} == {
            "treatment_proposal_id",
            "evidence_id",
        }, "the pair itself must be the primary key, or a proposal could cite one evidence twice"


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
def test_money_columns_are_exact_numeric_returning_decimal(
    table_name: str, column_name: str
) -> None:
    column_type = _tables()[table_name].columns[column_name].type
    assert isinstance(column_type, Money), f"{table_name}.{column_name} is not the Money type"

    impl = column_type.impl_instance
    assert isinstance(impl, Numeric), f"{table_name}.{column_name} is not NUMERIC"
    assert impl.asdecimal is True
    assert impl.python_type is Decimal


@pytest.mark.parametrize(("table_name", "column_name"), list(MONEY_COLUMNS))
def test_money_columns_carry_no_fixed_scale_typmod(table_name: str, column_name: str) -> None:
    """**Regression guard.** A fixed-scale typmod silently rounds and must never return.

    ``NUMERIC(20, 4)`` does not reject an over-precise value — it rounds it before any check
    constraint can see the original. Measured: ``Decimal("1.23456")`` was stored as
    ``1.2346`` with no error. Declaring precision or scale on a monetary column reintroduces
    that defect, so this test fails the moment anyone does.
    """
    column_type = _tables()[table_name].columns[column_name].type
    if isinstance(column_type, TypeDecorator):
        column_type = column_type.impl_instance
    assert isinstance(column_type, Numeric)

    assert column_type.precision is None, (
        f"{table_name}.{column_name} declares precision "
        f"{column_type.precision!r}; a fixed typmod silently rounds"
    )
    assert column_type.scale is None, (
        f"{table_name}.{column_name} declares scale {column_type.scale!r}; "
        f"a fixed typmod silently rounds"
    )


@pytest.mark.parametrize(("table_name", "column_name"), list(MONEY_COLUMNS))
def test_every_money_column_has_a_precision_check_constraint(
    table_name: str, column_name: str
) -> None:
    """Removing the typmod is only safe because a constraint replaces it."""
    conditions = [
        str(constraint.sqltext)
        for constraint in _tables()[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    ]
    scale_checks = [c for c in conditions if f"trunc({column_name}" in c]
    magnitude_checks = [c for c in conditions if f"abs({column_name})" in c]
    assert scale_checks, f"{table_name}.{column_name} has no scale check constraint"
    assert magnitude_checks, f"{table_name}.{column_name} has no magnitude check constraint"

    assert f"trunc({column_name}, {MONEY_MAX_SCALE}) = {column_name}" in scale_checks[0]
    # NaN is excluded explicitly: trunc('NaN',4) = 'NaN' is TRUE, so the scale rule alone
    # would admit it.
    assert f"{column_name} <> 'NaN'::numeric" in scale_checks[0]
    assert f"abs({column_name}) < {MONEY_MAGNITUDE_EXCLUSIVE_BOUND}" in magnitude_checks[0]


def test_precision_check_uses_trunc_not_scale() -> None:
    """``scale()`` is representation-based and would reject valid values.

    ``scale(1.230000)`` is 6, so a scale-based rule rejects a value numerically identical to
    ``1.2300`` that loses nothing when stored. ``trunc(v, 4) = v`` is value-based.
    """
    conditions = " ".join(
        str(constraint.sqltext)
        for table in _tables().values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "trunc(" in conditions
    assert "scale(" not in conditions, "scale() would reject valid trailing-zero values"


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
    assert MONEY_MAX_SCALE >= 4


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
        if table.name in ASSOCIATION_TABLES:
            continue
        column = table.columns["created_at"]
        assert column.server_default is not None, f"{table.name}.created_at has no server default"
        assert column.nullable is False


def test_every_table_has_a_uuid_primary_key() -> None:
    for table in _tables().values():
        if table.name in ASSOCIATION_TABLES:
            continue
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
        # M1.2. The rule is RESTRICT everywhere on the decision and money path: each of these
        # rows is evidence that a financial decision was taken, and a cascade would delete the
        # record of a decision as a side effect of tidying up something upstream.
        ("exception", "settlement_line_id", "settlement_line", "RESTRICT"),
        ("treatment_proposal", "exception_id", "exception", "RESTRICT"),
        ("approval", "exception_id", "exception", "RESTRICT"),
        ("approval", "treatment_proposal_id", "treatment_proposal", "RESTRICT"),
        ("outbox", "adjustment_id", "adjustment", "RESTRICT"),
        ("dlq", "outbox_id", "outbox", "RESTRICT"),
        # Three composite keys. Each carries a value from the row it references so the value
        # is *verified* rather than copied — see the authorisation and segregation tests.
        # SQLAlchemy reports one entry per column, so a composite key appears once per column.
        ("adjustment", "approval_id", "approval", "RESTRICT"),
        ("adjustment", "approved_treatment", "approval", "RESTRICT"),
        ("adjustment", "approving_principal", "approval", "RESTRICT"),
        ("posting_attempt", "adjustment_id", "adjustment", "RESTRICT"),
        ("posting_attempt", "operation_id", "adjustment", "RESTRICT"),
        ("recovery_queue", "adjustment_id", "adjustment", "RESTRICT"),
        ("recovery_queue", "approving_principal", "adjustment", "RESTRICT"),
        ("treatment_proposal_evidence", "evidence_id", "evidence", "RESTRICT"),
        # The two exceptions, both components rather than decisions: evidence has no meaning
        # apart from its exception, and a citation has none apart from its proposal. Neither
        # cascade is reachable from a settlement line, because the RESTRICT above blocks it.
        ("evidence", "exception_id", "exception", "CASCADE"),
        (
            "treatment_proposal_evidence",
            "treatment_proposal_id",
            "treatment_proposal",
            "CASCADE",
        ),
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
        # M1.2.
        ("exception", ("settlement_line_id",)),
        ("approval", ("exception_id", "resolution_version")),
        # Redundant as uniqueness claims — `id` is already the primary key — and required,
        # because a foreign key must reference a uniquely-constrained column list.
        ("approval", ("id", "approved_treatment", "principal")),
        ("adjustment", ("id", "operation_id")),
        ("adjustment", ("id", "approving_principal")),
        # 12.2: duplicate suppression is a database guarantee, not application logic.
        ("adjustment", ("operation_id",)),
        ("adjustment", ("approval_id",)),
        ("outbox", ("adjustment_id",)),
        ("posting_attempt", ("adjustment_id", "attempt_no")),
        ("dlq", ("outbox_id",)),
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
        "ck_settlement_line_amount_scale",
        "ck_settlement_line_amount_magnitude",
        "ck_ledger_entry_amount_scale",
        "ck_ledger_entry_amount_magnitude",
        "ck_match_result_tolerance_scale",
        "ck_match_result_tolerance_magnitude",
        # M1.2 - closed vocabularies.
        "ck_exception_classification_valid",
        "ck_exception_status_valid",
        "ck_evidence_kind_valid",
        "ck_treatment_proposal_treatment_valid",
        "ck_treatment_proposal_confidence_valid",
        "ck_approval_decision_valid",
        "ck_approval_approved_treatment_valid",
        "ck_outbox_state_valid",
        "ck_outbox_last_outcome_valid",
        "ck_posting_attempt_state_valid",
        "ck_posting_attempt_outcome_valid",
        "ck_dlq_replay_state_valid",
        "ck_recovery_queue_state_valid",
        "ck_recovery_queue_resolution_valid",
        "ck_audit_event_tool_valid",
        "ck_audit_event_approval_decision_valid",
        "ck_audit_event_outcome_valid",
        # M1.2 - hashes and formats.
        "ck_treatment_proposal_prompt_hash_is_sha256_hex",
        "ck_adjustment_operation_id_is_sha256_hex",
        "ck_adjustment_instruction_payload_hash_is_sha256_hex",
        "ck_adjustment_period_is_year_month",
        "ck_adjustment_currency_format",
        "ck_adjustment_treatment_valid",
        "ck_adjustment_escalation_is_never_posted",
        "ck_posting_attempt_operation_id_is_sha256_hex",
        # M1.2 - money on the adjustment, under the same rules as M1.1.
        "ck_adjustment_amount_scale",
        "ck_adjustment_amount_magnitude",
        # M1.2 - state coherence. These are the constraints that make an ambiguous outcome
        # impossible to file as a finished one, and a resolution impossible to record halfway.
        "ck_treatment_proposal_abstention_escalates",
        "ck_approval_approved_treatment_iff_authorising",
        "ck_outbox_settled_requires_terminal_outcome",
        "ck_posting_attempt_resolved_iff_outcome_recorded",
        "ck_posting_attempt_resolved_after_sent",
        "ck_posting_attempt_posting_ref_requires_applied_outcome",
        "ck_dlq_replayed_at_iff_replayed",
        "ck_dlq_envelope_carries_no_monetary_key",
        "ck_recovery_queue_resolved_iff_fully_recorded",
        "ck_recovery_queue_resolved_after_opened",
        "ck_recovery_queue_sla_due_after_opened",
        "ck_recovery_queue_segregation_of_duties",
        # M1.2 - bounds.
        "ck_approval_resolution_version_positive",
        "ck_outbox_attempt_count_non_negative",
        "ck_posting_attempt_attempt_no_positive",
        "ck_dlq_attempts_non_negative",
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
        "exception": ["settlement_line_id", "classification", "status", "correlation_id"],
        "evidence": ["exception_id", "kind", "content"],
        "treatment_proposal": [
            "exception_id",
            "treatment",
            "confidence",
            "rationale",
            "abstained",
            "model_id",
            "model_version",
            "prompt_hash",
            "region_jurisdiction",
            "proposed_at",
        ],
        "approval": ["exception_id", "resolution_version", "decision", "principal", "decided_at"],
        "adjustment": [
            "approval_id",
            # Both NOT NULL is what makes the composite key bite: PostgreSQL's default
            # MATCH SIMPLE would skip the check entirely if either were NULL.
            "approved_treatment",
            "approving_principal",
            "amount",
            "currency",
            "account_code",
            "period",
            "operation_id",
            "instruction_payload_hash",
        ],
        "outbox": ["adjustment_id", "state", "attempt_count"],
        # sent_at NOT NULL is what makes the write-ahead record evidence: a row with no send
        # time could not distinguish "about to send" from "sent".
        "posting_attempt": ["adjustment_id", "operation_id", "attempt_no", "sent_at", "state"],
        "dlq": ["outbox_id", "envelope", "reason", "attempts", "replay_state"],
        "recovery_queue": [
            "adjustment_id",
            "state",
            "reason",
            "evidence_procedure",
            "opened_at",
            "sla_due_at",
            "approving_principal",
        ],
        "audit_event": [
            "occurred_at",
            "principal",
            "tool",
            "scope_granted",
            "approval_decision",
            "outcome",
            "correlation_id",
        ],
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
        # M1.2. Each backs a query the system actually performs: the analyst queue, evidence
        # assembly, the dispatcher poll, the recovery scan, the operator queues, audit replay.
        ("exception", ("status", "created_at")),
        ("evidence", ("exception_id",)),
        ("treatment_proposal", ("exception_id",)),
        ("outbox", ("next_attempt_at",)),
        ("posting_attempt", ("sent_at",)),
        ("dlq", ("created_at",)),
        ("recovery_queue", ("sla_due_at",)),
        ("recovery_queue", ("adjustment_id",)),
        ("audit_event", ("correlation_id", "occurred_at")),
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
    for model in (
        SettlementBatch,
        SettlementLine,
        LedgerEntry,
        MatchResult,
        ExceptionRecord,
        Evidence,
        TreatmentProposal,
        TreatmentProposalEvidence,
        Approval,
        Adjustment,
        Outbox,
        PostingAttempt,
        DeadLetter,
        RecoveryItem,
        AuditEvent,
    ):
        assert model.__tablename__ in EXPECTED_TABLES


# --------------------------------------------------------------------------------------
# Containment: the model must never touch money
#
# CLAUDE.md rule 1 and PROJECT_SPEC.md 6.1. The response schema guard arrives with the
# provider port in 3.2; these are the persistence half of the same rule, and they are here
# rather than there because a numeric column added to this table would defeat the guard
# regardless of how well the schema itself is policed.
# --------------------------------------------------------------------------------------


def test_treatment_proposal_has_no_numeric_column_of_any_kind() -> None:
    """The model output table cannot hold a number.

    Not "no amount column" - no number at all. A score, a count, a percentage or a ratio is
    a channel from the model into the money path just as an amount is, and the whole point of
    the closed treatment enum is that the channel does not exist. Confidence is a band.
    """
    numeric = [
        f"{column.name}: {column.type!r}"
        for column in _tables()["treatment_proposal"].columns
        if isinstance(column.type, NUMERIC_TYPES)
        or (
            isinstance(column.type, TypeDecorator)
            and isinstance(column.type.impl_instance, NUMERIC_TYPES)
        )
    ]
    assert not numeric, f"treatment_proposal must carry no number: {numeric}"


def test_treatment_proposal_has_no_amount_like_column_name() -> None:
    """A text column named ``amount`` would be a channel dressed as provenance."""
    forbidden = {
        "amount",
        "value",
        "total",
        "sum",
        "qty",
        "quantity",
        "rate",
        "pct",
        "percent",
        "balance",
        "delta",
        "fee",
        "price",
        "cost",
        "score",
        "currency",
    }
    names = {c.name for c in _tables()["treatment_proposal"].columns}
    assert not (names & forbidden), f"amount-like columns: {sorted(names & forbidden)}"


def test_confidence_is_a_closed_band_not_a_score() -> None:
    assert {band.value for band in ConfidenceBand} == {"low", "medium", "high"}


def test_treatment_set_is_the_closed_four() -> None:
    """The only channel from the model into the money path (6.1)."""
    assert {code.value for code in TreatmentCode} == {
        "rebook",
        "accrue",
        "write_off",
        "escalate",
    }


def test_no_monetary_column_exists_outside_the_declared_set() -> None:
    """Money may only appear where this file says it appears.

    Catches an amount added to a control table - an outbox row carrying its own copy of the
    amount, say - which would create a second source of truth for the figure that gets posted.
    """
    declared = set(MONEY_COLUMNS)
    actual = {
        (table.name, column.name)
        for table in _tables().values()
        for column in table.columns
        if isinstance(column.type, Money)
    }
    assert actual == declared, f"undeclared monetary columns: {sorted(actual - declared)}"


# --------------------------------------------------------------------------------------
# Ambiguous outcomes must stay representable
#
# 13.5. These assertions look trivial. They are here because the failure they guard against
# is a later increment quietly reducing an outcome to a boolean, which is the single defect
# this project exists to demonstrate the absence of.
# --------------------------------------------------------------------------------------


def test_posting_outcome_can_express_ambiguity() -> None:
    values = {outcome.value for outcome in PostingOutcome}
    assert "unknown" in values, "an outcome vocabulary without UNKNOWN cannot be honest"
    assert "partially_applied" in values
    assert "throttled" in values, "throttling is not rejection and must not share its path"
    assert values == {"confirmed", "rejected", "throttled", "unknown", "partially_applied"}


def test_an_unknown_outcome_cannot_be_filed_as_settled() -> None:
    """Declared as a check constraint; PostgreSQL enforcement is proven in the schema tests."""
    constraint = next(
        c
        for c in _tables()["outbox"].constraints
        if isinstance(c, CheckConstraint)
        and str(c.name) == "ck_outbox_settled_requires_terminal_outcome"
    )
    text = str(constraint.sqltext)
    assert "settled" in text
    for ambiguous in ("unknown", "throttled", "partially_applied"):
        assert ambiguous not in text, f"{ambiguous} must not be an acceptable settled outcome"


def test_attempt_state_distinguishes_in_flight_from_resolved() -> None:
    """12.1.1: the write-ahead record is only evidence if "sent, nothing known" exists."""
    assert {state.value for state in AttemptState} == {"in_flight", "resolved"}


def test_recovery_resolution_records_an_unverified_judgement_distinctly() -> None:
    """13.5 clause 5: a judgement made without evidence must not look like a verified one."""
    assert {r.value for r in RecoveryResolution} == {
        "confirmed_by_evidence",
        "rejected_by_evidence",
        "resolved_unverified",
    }


def test_remaining_control_enums_are_closed() -> None:
    assert {v.value for v in ExceptionClassification} == {
        "partial_capture",
        "fee_split",
        "chargeback_reversal",
        "fx_rounding",
        "cross_period_refund",
        "unclassified",
    }
    assert {v.value for v in ExceptionStatus} == {"open", "resolved"}
    assert {v.value for v in EvidenceKind} == {
        "dispute_reason",
        "merchant_memo",
        "support_ticket_note",
        "remittance_reference",
        "candidate_ledger_entry",
    }
    assert {v.value for v in ApprovalDecision} == {"approved", "rejected", "edited"}
    assert {v.value for v in DispatchState} == {"pending", "settled", "dead_lettered"}
    assert {v.value for v in ReplayState} == {"pending", "replayed", "abandoned"}
    assert {v.value for v in RecoveryState} == {"open", "resolved"}


# --------------------------------------------------------------------------------------
# Authorisation and segregation of duties are referential, not copied
#
# Added after an adversarial review found that both controls rested on a value the
# application supplies. A foreign key to `approval.id` proves an approval exists; it does not
# prove the approval said yes. A check comparing against a copied principal is only as good as
# the copy. Both are now closed by composite foreign keys (ADR-028, ADR-030).
# --------------------------------------------------------------------------------------


def _composite_fk(table_name: str) -> dict[str, tuple[str, ...]]:
    """Map referenced table -> referencing columns, for composite keys only."""
    return {
        constraint.referred_table.name: tuple(e.parent.name for e in constraint.elements)
        for constraint in _tables()[table_name].foreign_key_constraints
        if len(constraint.elements) > 1
    }


def test_an_adjustment_is_bound_to_what_its_approval_authorised() -> None:
    """FR-7. The authorisation must be the referential fact, not merely a row that exists."""
    assert _composite_fk("adjustment")["approval"] == (
        "approval_id",
        "approved_treatment",
        "approving_principal",
    )
    adjustment = _tables()["adjustment"]
    # NOT NULL on this side is what makes a rejection unreferenceable: a rejected approval
    # carries approved_treatment NULL, and NULL matches nothing.
    assert adjustment.columns["approved_treatment"].nullable is False
    assert adjustment.columns["approving_principal"].nullable is False


def test_an_escalated_treatment_cannot_be_posted() -> None:
    """§6.2: escalation happens *because* the case cannot be priced deterministically."""
    names = {
        str(c.name) for c in _tables()["adjustment"].constraints if isinstance(c, CheckConstraint)
    }
    assert "ck_adjustment_escalation_is_never_posted" in names


def test_an_attempt_record_is_bound_to_its_adjustments_operation_id() -> None:
    """§12.1.1: recovery reads this row to decide whether a write may be repeated."""
    assert _composite_fk("posting_attempt")["adjustment"] == ("adjustment_id", "operation_id")


def test_segregation_of_duties_compares_against_a_verified_principal() -> None:
    """§13.5 clause 5.

    The check constraint is only half the control. Without this key the principal it compares
    against is whatever the writer supplied, so the discipline would have moved from the
    comparison to the copy rather than being removed.
    """
    assert _composite_fk("recovery_queue")["adjustment"] == (
        "adjustment_id",
        "approving_principal",
    )
    constraint = next(
        c
        for c in _tables()["recovery_queue"].constraints
        if isinstance(c, CheckConstraint)
        and str(c.name) == "ck_recovery_queue_segregation_of_duties"
    )
    assert "approving_principal" in str(constraint.sqltext)


# --------------------------------------------------------------------------------------
# Audit-event contract v1
# --------------------------------------------------------------------------------------


def test_audit_event_carries_every_contract_v1_field() -> None:
    """11. Seven later projects copy this shape, so the field set is a commitment."""
    required = {
        "principal",
        "agent_identity",
        "tool",
        "scope_granted",
        "approval_decision",
        "approver",
        "model",
        "region_jurisdiction",
        "outcome",
        "correlation_id",
        "occurred_at",
    }
    actual = {c.name for c in _tables()["audit_event"].columns}
    assert required <= actual, f"contract v1 fields missing: {sorted(required - actual)}"


# --------------------------------------------------------------------------------------
# Partial indexes
# --------------------------------------------------------------------------------------


def test_partial_indexes_declare_their_predicate() -> None:
    """A partial index that lost its predicate is a different, much larger index.

    Autogenerate does emit ``postgresql_where``, but silently dropping it would still produce
    a migration that applies cleanly - so the predicate is asserted rather than assumed.
    """
    expected = {
        "ix_outbox_pending_next_attempt_at": "state = 'pending'",
        "ix_posting_attempt_in_flight": "state = 'in_flight'",
        "ix_dlq_pending_created_at": "replay_state = 'pending'",
        "ix_recovery_queue_open_sla_due_at": "state = 'open'",
        "uq_recovery_queue_open_adjustment": "state = 'open'",
    }
    actual = {
        str(index.name): str(index.dialect_options["postgresql"]["where"])
        for table in _tables().values()
        for index in table.indexes
        if index.dialect_options["postgresql"]["where"] is not None
    }
    assert actual == expected


def test_at_most_one_open_recovery_item_per_adjustment_is_a_unique_index() -> None:
    """Uniqueness must hold only while the item is open, so it cannot be a table constraint."""
    index = next(
        i
        for table in _tables().values()
        for i in table.indexes
        if str(i.name) == "uq_recovery_queue_open_adjustment"
    )
    assert index.unique is True
    assert [c.name for c in index.columns] == ["adjustment_id"]


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
