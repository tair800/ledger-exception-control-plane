"""M5.2 — audit-event contract v1, everything provable without a database.

The three tests `IMPLEMENTATION_PLAN.md` §5.2 names all need a real server, so they live in
``test_audit_contract_postgres.py``. What is here is the contract's *shape*: the vocabularies, the
mappings that must be total, the fields that must never carry certain things, and the guards that
keep a portfolio contract from drifting once six later repositories have copied it.

**Why the shape gets its own suite.** §11 is copied, not imported. A later repository takes this
field set and these vocabularies and re-implements them, so what matters most is that they are
*decided* — one scope vocabulary rather than nine string literals, one model-identity format rather
than three, one total outcome mapping rather than a default. A drift here is a drift across the
portfolio, and none of it needs a container to check.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ledger_exception_control_plane.audit import (
    NO_AUTHORITY,
    SYSTEM_PRINCIPAL,
    UNRECORDED_CORRELATION_ID,
    Scope,
    approval_scope,
    correlation_id_for,
    is_known_scope,
    model_identity,
    scope_for,
)
from ledger_exception_control_plane.db.control import (
    AuditApprovalDecision,
    AuditEvent,
    AuditOutcome,
    AuditTool,
)

PACKAGE_ROOT = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"
)


def _package_sources() -> dict[str, str]:
    return {
        str(path.relative_to(PACKAGE_ROOT)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    }


# ======================================================================================
# The field set §11 fixes
# ======================================================================================


def test_the_table_carries_every_field_section_eleven_names() -> None:
    """The ten rows of §11's table, by name.

    Ten, not eleven. ``occurred_at`` is an implementation column M1.2 added and the specification
    never mentions — a distinction worth keeping, because a later repository copying "contract v1"
    should be told which fields are the contract and which are this schema's own bookkeeping.
    """
    contract_v1 = {
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
    }
    columns = {column.name for column in AuditEvent.__table__.columns}

    assert contract_v1 <= columns
    assert columns - contract_v1 == {"id", "occurred_at", "created_at"}, (
        "the table may carry bookkeeping the contract does not name, but every such column should "
        "be a deliberate addition rather than a field somebody thought §11 asked for"
    )


def test_the_outcome_vocabulary_is_exactly_the_four_the_specification_names() -> None:
    assert {outcome.value for outcome in AuditOutcome} == {
        "success",
        "failure",
        "abstained",
        "quarantined",
    }


def test_the_approval_decision_vocabulary_is_exactly_the_four_named() -> None:
    assert {decision.value for decision in AuditApprovalDecision} == {
        "approved",
        "rejected",
        "edited",
        "n_a",
    }


def test_the_tool_vocabulary_is_the_eight_named_plus_the_two_that_needed_a_clause() -> None:
    """**The widening rule, asserted rather than described.**

    §11 glosses eight verbs. 4.4 added ``reconcile`` and ``recover`` because §13.5 clause 6 requires
    an event for *"every reconciliation query and result, and every manual decision"* and no
    existing verb names those acts.

    That is the project's rule and this test is where it bites: **a verb is added only when a
    specification clause requires an event for an action the existing verbs cannot name.** An
    eleventh member appearing without a clause behind it fails here — which is the point, because
    the vocabulary is copied by later repositories and a verb invented in one of them is a contract
    that has quietly forked. ADR-058 records it.
    """
    section_eleven = {
        "match",
        "propose_treatment",
        "approve",
        "compute_amount",
        "post",
        "retry",
        "dlq",
        "replay",
    }
    required_by_section_thirteen_five = {"reconcile", "recover"}

    assert {tool.value for tool in AuditTool} == (
        section_eleven | required_by_section_thirteen_five
    )


# ======================================================================================
# The scope vocabulary
# ======================================================================================


def test_every_deterministic_tool_has_a_scope() -> None:
    """Total over the enum bar one, and the exception is the point.

    ``approve`` has no fixed scope because it is the only action a *person* takes: its authority is
    the approver's role, which is a runtime value. Every other verb runs under a named internal
    capability, and a verb with no authority would be an action nobody could say the authorisation
    for — which is the one question §11 provides this field to answer.
    """
    for tool in AuditTool:
        if tool is AuditTool.APPROVE:
            with pytest.raises(ValueError, match="approve"):
                scope_for(tool)
            continue
        assert isinstance(scope_for(tool), Scope)


def test_the_scopes_are_distinct_and_name_capabilities_not_people() -> None:
    values = [scope.value for scope in Scope]

    assert len(values) == len(set(values))
    for value in values:
        assert ":" in value, f"{value} does not name a capability in a namespace"
        assert not value.startswith("approval:"), (
            "a fixed scope must never be an approval; that authority belongs to a role"
        )


@pytest.mark.parametrize(
    ("label", "scope", "known"),
    [
        ("a declared capability", Scope.POST.value, True),
        ("an approval with a role", "approval:controller", True),
        ("no authority at all", NO_AUTHORITY, True),
        ("an approval with no role", "approval:", False),
        ("free text", "whatever the caller felt like", False),
        ("an empty string", "", False),
        ("a near miss", "ledger:posts", False),
    ],
)
def test_only_three_shapes_of_scope_are_admitted(label: str, scope: str, known: bool) -> None:
    """§11's authorisation field is queryable only if its values come from one vocabulary.

    An auditor asking "what ran under the model's authority" is filtering, and a filter over strings
    each call site invented is a filter over spelling.
    """
    assert is_known_scope(scope) is known


def test_a_refused_action_can_record_that_no_authority_was_held() -> None:
    """**The value that exists because the first version recorded a lie.**

    4.4's refusal path stamped ``approval:<role>`` on every refused approval — including one refused
    precisely because that role may not approve. An operator's blocked attempt wrote a permanent,
    undeletable row asserting an authorisation §16 does not grant, in the one field provided for
    answering that question.
    """
    assert is_known_scope(NO_AUTHORITY)
    assert approval_scope("operator") != NO_AUTHORITY
    assert not NO_AUTHORITY.startswith("approval")


def test_the_refused_scope_is_decided_by_the_verb_that_was_attempted() -> None:
    """**The same lie, reintroduced by ADR-061 and caught by a reviewer rather than by a test.**

    The route decided whether a refused decision had *any* authority with ``may_record_decision()``,
    which was the whole right until 5.1's correction split it in two. An analyst passes that check,
    so a refused **approve** was about to record ``approval:analyst`` — asserting the one
    authorisation ADR-056 specifically denies them, permanently, in the field an auditor reads to
    check exactly that.

    Asserted verb by verb rather than role by role, because the property is that the audit row asks
    the same question the gate asked. A table keyed on roles would pass while the two drifted apart
    again, which is how this happened both times.
    """
    from ledger_exception_control_plane.db.control import ApprovalDecision
    from ledger_exception_control_plane.routes import _holds_the_authority_for
    from ledger_exception_control_plane.security import Principal, Role

    analyst = Principal(id="analyst-a", role=Role.ANALYST)
    controller = Principal(id="controller-a", role=Role.CONTROLLER)
    operator = Principal(id="operator-a", role=Role.OPERATOR)

    # An analyst holds the right to reject and no more. The first two are the regression.
    assert not _holds_the_authority_for(analyst, ApprovalDecision.APPROVED)
    assert not _holds_the_authority_for(analyst, ApprovalDecision.EDITED)
    assert _holds_the_authority_for(analyst, ApprovalDecision.REJECTED)

    # A controller holds all three, or the gate itself would be wrong.
    for decision in ApprovalDecision:
        assert _holds_the_authority_for(controller, decision), decision

    # An operator works the failure queues and decides nothing.
    for decision in ApprovalDecision:
        assert not _holds_the_authority_for(operator, decision), decision


# ======================================================================================
# What must never reach a row
# ======================================================================================


def test_no_call_site_puts_money_evidence_or_a_secret_into_an_event() -> None:
    """**The field set is about who did what, not about what it was worth.**

    §11 carries no amount and no evidence, deliberately: the financial facts live in ``adjustment``
    and the reasoning in ``treatment_proposal``, both of which the correlation id joins to. An audit
    row duplicating the amount would be a second copy of a number with exactly one owner. §16 adds
    that merchant identifiers must not appear in telemetry, and §17 that no secret may.

    Walked as syntax over every ``emit`` call in the package, so a future call site is watched by
    default rather than when somebody remembers to add it here.
    """
    forbidden = {
        "amount",
        "currency",
        "rationale",
        "evidence",
        "psp_reference",
        "merchant_reference",
        "token",
        "dsn",
        "password",
        "secret",
        "posting_ref",
    }
    inspected = 0
    for name, source in _package_sources().items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            callee = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            if callee != "emit":
                continue
            inspected += 1
            for keyword in node.keywords:
                # `tool` and `scope_granted` carry vocabulary members, not data, and one of the
                # verbs is literally named `compute_amount` — scanning those for the substring
                # "amount" flags the contract's own vocabulary. This fence is about what *values*
                # cross into a row, so it looks at the fields that carry values.
                if keyword.arg in {"tool", "scope_granted", "outcome", "approval_decision"}:
                    continue
                rendered = ast.unparse(keyword.value).lower()
                for banned in forbidden:
                    assert banned not in rendered, (
                        f"{name} passes {banned!r} into an audit event via {keyword.arg}"
                    )

    assert inspected >= 8, "the scan is not seeing the emit call sites"


def test_the_emitter_is_the_only_thing_that_builds_an_event() -> None:
    """One shape means one constructor, and this is what keeps that true.

    A companion to the entity-writer fence in ``test_treatment_closure.py``: that one asserts which
    *module* may write each guarded entity, this one asserts the audit row is never built anywhere
    but inside the emitter itself — including inside ``audit.py``, where a second helper that
    assembled its own row would be the same drift one file closer.
    """
    constructing = set()
    for name, source in _package_sources().items():
        for node in ast.walk(ast.parse(source)):
            # A *call*, so the class statement in `db/control.py` — a ClassDef with a base, not a
            # Call — is not mistaken for a construction of the thing it defines.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AuditEvent"
            ):
                constructing.add(name)

    assert constructing == {"audit.py"}


# ======================================================================================
# The correlation id
# ======================================================================================


def test_the_correlation_id_is_derived_from_the_ingested_artefact() -> None:
    """§18: *"generated at ingestion, propagated through every layer"*.

    Derived rather than threaded, which is what makes the span a property of the data: any stage can
    compute it, so no layer has to remember to pass it on and no layer can mint a fresh one.
    """
    digest = "a" * 64

    assert correlation_id_for(digest, 7) == f"lecp:{digest}:000007"
    assert correlation_id_for(digest, 7) == correlation_id_for(digest, 7)
    assert correlation_id_for(digest, 7) != correlation_id_for(digest, 8)
    assert correlation_id_for(digest, 8) != correlation_id_for("b" * 64, 8)
    assert len(correlation_id_for(digest, 999999)) <= 128, "must fit exception.correlation_id"


def test_the_derivation_has_exactly_one_definition() -> None:
    """It moved to ``audit`` at 5.2 and is re-exported from where it used to live.

    Two definitions would be two spans, and the failure would look like an audit trail that simply
    had no other members — the worst kind, because it reads as an absence of activity.
    """
    from ledger_exception_control_plane.audit import correlation_id_for as canonical
    from ledger_exception_control_plane.classification import correlation_id_for as re_exported

    assert canonical is re_exported

    definitions = {
        name for name, source in _package_sources().items() if "def correlation_id_for(" in source
    }
    assert definitions == {"audit.py"}


def test_an_unwalkable_chain_is_recorded_as_such_rather_than_invented() -> None:
    """§11 makes the field NOT NULL, so there is no "leave it out"; a fresh id would be worse.

    An invented identifier looks like a trace that simply has no other members. A fixed, obviously
    synthetic value says what happened and greps.
    """
    assert UNRECORDED_CORRELATION_ID.startswith("lecp")
    assert "unrecorded" in UNRECORDED_CORRELATION_ID
    assert correlation_id_for("a" * 64, 1) != UNRECORDED_CORRELATION_ID


# ======================================================================================
# Model identity
# ======================================================================================


def test_the_model_field_carries_the_id_and_the_version_together() -> None:
    """§11: *"Model id and version"*. Both, because two versions of one model can differ."""
    assert model_identity("claude-opus-5", "2026-09-01") == "claude-opus-5@2026-09-01"


def test_the_model_identity_has_one_format() -> None:
    """One helper, so the pair is never joined two different ways across the portfolio."""
    joiners = {
        name
        for name, source in _package_sources().items()
        if "model_id" in source and "model_version" in source and "def model_identity" not in source
    }
    for name in joiners:
        source = _package_sources()[name]
        assert 'f"{proposer.model_id}' not in source, f"{name} builds the pair by hand"
        assert "model_id + " not in source, f"{name} builds the pair by hand"


def test_no_call_site_spells_the_system_principal_itself() -> None:
    """§11: *"Authenticated human, or `system`"*. One literal, so the trail stays filterable by the
    distinction it exists to draw rather than by spelling.

    Scoped to ``emit`` call sites rather than to the word anywhere in the package: the provider
    adapters use ``"system"`` for a prompt role, which is an unrelated meaning of the same string,
    and flagging it would be the sort of noise that gets a fence deleted.
    """
    assert SYSTEM_PRINCIPAL == "system"

    for name, source in _package_sources().items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            callee = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            if callee != "emit":
                continue
            for keyword in node.keywords:
                if keyword.arg != "principal":
                    continue
                assert not isinstance(keyword.value, ast.Constant), (
                    f"{name} passes a literal principal; use SYSTEM_PRINCIPAL or a real one"
                )
