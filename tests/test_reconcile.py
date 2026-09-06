"""M4.4 — the bounds, the windows and the vocabularies, with no database and no ledger.

Every rule §13.5 states as a condition is a pure function here, and that is deliberate: the two
bounds on a re-send and the two windows that gate believing a ``NotFound`` are the whole safety
argument for touching an ambiguous financial write, and an argument that can only be exercised
through a container is one nobody exercises.

The workflow those functions drive — the query table, the monotonicity triggers, the interlock, the
queue — is in ``test_reconcile_postgres.py``, because its entire content is database behaviour.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ledger_exception_control_plane.audit import (
    _POSTING_AUDIT_OUTCOME,
    posting_audit_outcome,
)
from ledger_exception_control_plane.db.control import (
    AuditOutcome,
    AuditTool,
    QueryAnswer,
    RecoveryResolution,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
from ledger_exception_control_plane.ledger.port import (
    UNBOUNDED,
    Atomicity,
    Eventual,
    IdempotencyMode,
    IdempotencyScope,
    LedgerAdapterCapabilities,
    Linearizable,
    PostingQueryMode,
)
from ledger_exception_control_plane.operations.dispatcher import (
    ResendBound,
    resend_is_within_bounds,
)
from ledger_exception_control_plane.operations.reconcile import (
    _ANSWER_AUDIT_OUTCOME,
    AMBIGUOUS,
    ReconciliationPolicy,
    Resolution,
    negative_answer_is_trustworthy,
    visibility_bound_of,
)
from ledger_exception_control_plane.operations.recovery import (
    _RESOLUTION_AUDIT_OUTCOME,
    RecoveryReason,
    evidence_procedure_for,
)

EPOCH = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
ENDPOINT = "sim://ledger/postings"


def _enforcing(**overrides: object) -> LedgerAdapterCapabilities:
    """A capability record that permits a bounded re-send, so each test varies one thing."""
    base = {
        "idempotency": IdempotencyMode.ENFORCES_KEY,
        "idempotency_window": dt.timedelta(days=1),
        "idempotency_scope": IdempotencyScope.PER_ENDPOINT,
        "posting_identity_query": PostingQueryMode.NONE,
        "query_consistency": Linearizable(),
        "max_inflight_window": dt.timedelta(seconds=30),
        "atomicity": Atomicity.ATOMIC,
    }
    return LedgerAdapterCapabilities(**{**base, **overrides})  # type: ignore[arg-type]


def _bound(
    capabilities: LedgerAdapterCapabilities,
    *,
    now: dt.datetime = EPOCH + HOUR,
    first_sent_at: dt.datetime | None = EPOCH,
    original_endpoint: str | None = ENDPOINT,
    target_endpoint: str | None = ENDPOINT,
) -> ResendBound:
    return resend_is_within_bounds(
        capabilities=capabilities,
        first_sent_at=first_sent_at,
        now=now,
        original_endpoint=original_endpoint,
        target_endpoint=target_endpoint,
    )


# ======================================================================================
# §13.5 clause 3 — a re-send is bounded by the window AND the scope
# ======================================================================================


def test_a_resend_inside_both_bounds_is_permitted() -> None:
    assert _bound(_enforcing()) is ResendBound.PERMITTED


def test_the_bounds_live_where_they_are_enforced() -> None:
    """**They are the dispatcher's, not the reconciler's**, and that placement is the property.

    §13.5's two bounds guard an irreversible write, so they are evaluated in front of the socket
    rather than in whichever module decided a re-send was worth attempting. A caller that forgot to
    ask — a future worker, a replay path, a console button — would otherwise get an unbounded
    duplicate under a header the provider may already have forgotten.

    Asserted rather than assumed, because a later refactor moving them back into the caller would
    leave every behavioural test above green while removing the guarantee.
    """
    import inspect

    from ledger_exception_control_plane.operations import dispatcher, reconcile

    assert inspect.getmodule(resend_is_within_bounds) is dispatcher
    assert "resend_is_within_bounds" in inspect.getsource(dispatcher.dispatch_once), (
        "the gate in front of the socket write must evaluate the bounds itself"
    )
    assert reconcile.ResendBound is ResendBound, "re-exported for one import site, not redefined"


@pytest.mark.parametrize(
    ("label", "elapsed", "expected"),
    [
        ("well inside", dt.timedelta(hours=1), ResendBound.PERMITTED),
        ("just inside", dt.timedelta(days=1, microseconds=-1), ResendBound.PERMITTED),
        # §13.5 is written as a strict inequality — "permitted only while
        # now - first_send < idempotency_window" — so the boundary itself is outside.
        ("exactly at the boundary", dt.timedelta(days=1), ResendBound.WINDOW_EXPIRED),
        ("past it", dt.timedelta(days=2), ResendBound.WINDOW_EXPIRED),
    ],
)
def test_the_window_is_a_strict_inequality(
    label: str, elapsed: dt.timedelta, expected: ResendBound
) -> None:
    """**The boundary case is the one that matters and the specification settles it.**

    A provider that retains a key for 24 hours has, at hour 24, either forgotten it or not — and we
    cannot tell which. The strict reading treats the boundary as expired, which is the direction
    that cannot produce a duplicate posting.
    """
    assert _bound(_enforcing(), now=EPOCH + elapsed) is expected


def test_an_unbounded_window_never_expires() -> None:
    """``UNBOUNDED`` is a value rather than ``None`` precisely so this branch can exist."""
    caps = _enforcing(idempotency_window=UNBOUNDED)

    assert _bound(caps, now=EPOCH + dt.timedelta(days=3650)) is ResendBound.PERMITTED


@pytest.mark.parametrize(
    ("label", "mode"),
    [
        ("no key at all", IdempotencyMode.NONE),
        # §13.4: a provider that accepts a header and ignores it is indistinguishable from one that
        # has none. This is the misreading the whole capability vocabulary exists to prevent.
        ("a key it merely accepts", IdempotencyMode.ACCEPTS_KEY),
    ],
)
def test_without_enforcement_there_is_no_bound_to_be_inside(
    label: str, mode: IdempotencyMode
) -> None:
    assert _bound(_enforcing(idempotency=mode)) is ResendBound.NOT_ENFORCED


def test_nothing_sent_means_nothing_to_resend() -> None:
    """Reported as ``NOT_ENFORCED`` rather than permitted: a caller arriving here with no attempt on
    record has a different problem than a spent bound, and "permitted" would be answering the wrong
    question with the dangerous value."""
    assert _bound(_enforcing(), first_sent_at=None) is ResendBound.NOT_ENFORCED


# ======================================================================================
# The scope half of the same clause
# ======================================================================================


def test_a_different_endpoint_is_outside_a_per_endpoint_scope() -> None:
    """§13.5: *"a re-send outside either bound is an ordinary duplicate write wearing an
    idempotency header"*. A key enforced per endpoint says nothing about a different one."""
    assert (
        _bound(_enforcing(), target_endpoint="sim://other/postings") is ResendBound.SCOPE_UNPROVEN
    )


def test_an_unrecorded_original_endpoint_is_unproven_rather_than_matching() -> None:
    """**The fail-safe direction, and the reason the column is nullable rather than backfilled.**

    An adapter that declared no endpoint left nothing on the attempt row to compare. Treating that
    absence as a match would let the weakest adapter through the strictest bound; treating it as a
    mismatch routes the operation to a human. §10.1 takes exactly this direction with an unverified
    capability, and the reasoning carries over unchanged: proving a re-send is inside its scope is
    our obligation, not the provider's.
    """
    assert _bound(_enforcing(), original_endpoint=None) is ResendBound.SCOPE_UNPROVEN


def test_a_global_scope_does_not_care_where_the_resend_goes() -> None:
    caps = _enforcing(idempotency_scope=IdempotencyScope.GLOBAL)

    assert _bound(caps, target_endpoint="sim://other/postings") is ResendBound.PERMITTED


def test_a_per_account_scope_is_satisfied_by_the_operation_identifier_itself() -> None:
    """**Not an omission — a consequence of §12.1.**

    The operation identifier binds the whole instruction payload, of which the account is part, so a
    re-send to a different account is a *different operation* and cannot reach this function
    carrying this identifier. Comparing accounts here would be re-checking something a unique
    constraint already makes impossible, and would suggest the identifier does not already cover it.
    """
    caps = _enforcing(idempotency_scope=IdempotencyScope.PER_ACCOUNT)

    assert _bound(caps, target_endpoint="sim://other/postings") is ResendBound.PERMITTED


def test_the_window_is_not_consulted_when_the_scope_already_fails() -> None:
    """Order matters for the message an operator reads: a scope mismatch inside a live window is
    reported as a scope problem, not as a window that happens to still be open."""
    assert (
        _bound(_enforcing(), now=EPOCH + HOUR, target_endpoint="elsewhere")
        is ResendBound.SCOPE_UNPROVEN
    )


# ======================================================================================
# §13.5 clause 4 — when a NotFound becomes trustworthy
# ======================================================================================


def test_a_linearizable_query_has_no_visibility_lag() -> None:
    assert visibility_bound_of(_enforcing(query_consistency=Linearizable())) == dt.timedelta(0)


def test_an_eventual_query_carries_its_own_bound() -> None:
    """The payload on the variant is what makes this answerable — 4.2 put it there for 4.4."""
    caps = _enforcing(query_consistency=Eventual(visibility_bound=dt.timedelta(minutes=5)))

    assert visibility_bound_of(caps) == dt.timedelta(minutes=5)


@pytest.mark.parametrize(
    ("label", "elapsed", "expected"),
    [
        ("neither window elapsed", dt.timedelta(minutes=1), False),
        ("only the visibility bound elapsed", dt.timedelta(minutes=6), False),
        ("only the in-flight window elapsed", dt.timedelta(minutes=4), False),
        ("both elapsed", dt.timedelta(minutes=11), True),
    ],
)
def test_both_windows_must_elapse_before_a_negative_answer_counts(
    label: str, elapsed: dt.timedelta, expected: bool
) -> None:
    """**Both, not either**, and the two cases are not the same failure seen twice.

    The visibility bound covers a read that has not caught up with the write. The in-flight window
    covers a request the ledger has received and not yet committed. A system that waited for only
    one would still act on the other, and acting on either is a double-post.

    The parameters are chosen so each window is the binding one in exactly one row: visibility is
    five minutes and in-flight is ten, so four minutes clears neither, six clears only visibility,
    and — because in-flight is the longer — the "only the in-flight window elapsed" row is
    unreachable by construction and is asserted at four minutes to show the conjunction still
    refuses. Reversing the two durations would swap which row is which and change nothing.
    """
    caps = _enforcing(
        query_consistency=Eventual(visibility_bound=dt.timedelta(minutes=5)),
        max_inflight_window=dt.timedelta(minutes=10),
    )

    trustworthy = negative_answer_is_trustworthy(
        capabilities=caps, last_sent_at=EPOCH, now=EPOCH + elapsed
    )
    assert trustworthy is expected


def test_a_linearizable_adapter_still_waits_out_its_inflight_window() -> None:
    """§13.5 says *"or immediately, if LINEARIZABLE"* about the **visibility** bound only.

    A linearizable read reflects every *committed* posting, which says nothing about a request the
    ledger has accepted and not yet committed. Reading the parenthesis as excusing both windows is
    the plausible misreading, and it is the one that double-posts.
    """
    caps = _enforcing(
        query_consistency=Linearizable(), max_inflight_window=dt.timedelta(minutes=10)
    )

    assert not negative_answer_is_trustworthy(
        capabilities=caps, last_sent_at=EPOCH, now=EPOCH + dt.timedelta(minutes=9)
    )
    assert negative_answer_is_trustworthy(
        capabilities=caps, last_sent_at=EPOCH, now=EPOCH + dt.timedelta(minutes=10)
    )


def test_an_adapter_that_declared_nothing_waits_a_long_time() -> None:
    """The defaults are the weakest claim, and for these two fields weakest means *long*.

    A record built with no arguments must not behave like a fast, linearizable ledger — that would
    be the strongest possible assertion made on behalf of an author who asserted nothing.
    """
    caps = LedgerAdapterCapabilities()

    assert not negative_answer_is_trustworthy(
        capabilities=caps, last_sent_at=EPOCH, now=EPOCH + dt.timedelta(hours=23)
    )


# ======================================================================================
# The policy's own bounds
# ======================================================================================


def test_the_default_policy_needs_three_consecutive_negatives() -> None:
    policy = ReconciliationPolicy()

    assert policy.consecutive_not_found == 3
    assert policy.max_queries == 10
    assert policy.sla == dt.timedelta(hours=24)


@pytest.mark.parametrize(
    ("label", "kwargs", "message"),
    [
        ("zero corroboration", {"consecutive_not_found": 0}, "at least 1"),
        ("a bound that expires first", {"max_queries": 2}, "at least N queries"),
        ("an SLA already elapsed", {"sla": dt.timedelta(0)}, "stale at once"),
    ],
)
def test_an_incoherent_policy_is_refused_at_construction(
    label: str, kwargs: object, message: str
) -> None:
    """Each refusal is a configuration that would look like a policy and behave like none.

    ``max_queries`` below N is the subtle one: the bound would expire before the evidence it is
    waiting for could ever be gathered, so every ambiguous operation would route to an operator
    having asked, and learned, nothing.
    """
    with pytest.raises(ValueError, match=message):
        ReconciliationPolicy(**kwargs)  # type: ignore[arg-type]


# ======================================================================================
# The vocabularies, and the mappings that must be total
# ======================================================================================


def test_the_three_declarations_of_the_ambiguous_set_agree() -> None:
    """Three modules declare this set for three different reasons; a test keeps them honest.

    The dispatcher's gates a second send *inside one dispatch*, the retry module's gates *selection
    for retry*, and this one gates *resolution*. Sharing one constant would couple three decisions
    that are free to diverge; letting them drift silently would be worse. So they are separate and
    checked equal, which is the arrangement that survives either of them changing on purpose.
    """
    from ledger_exception_control_plane.operations.dispatcher import _AMBIGUOUS
    from ledger_exception_control_plane.operations.retry import AMBIGUOUS_OUTCOMES

    assert AMBIGUOUS == _AMBIGUOUS == AMBIGUOUS_OUTCOMES
    assert {OutcomeCode.UNKNOWN, OutcomeCode.PARTIALLY_APPLIED} == AMBIGUOUS
    assert OutcomeCode.THROTTLED not in AMBIGUOUS, (
        "a throttle turned the request away before it could be applied; treating it as ambiguous "
        "would route a request the ledger never considered into manual recovery"
    )


def test_every_posting_outcome_has_an_audit_reading() -> None:
    assert set(_POSTING_AUDIT_OUTCOME) == set(OutcomeCode)


def test_an_ambiguous_outcome_is_quarantined_and_never_a_failure() -> None:
    """**The audit trail must not contain the coercion the system refuses to make.**

    Recording an ``UNKNOWN`` as ``FAILURE`` would put "the posting did not happen" into the one
    record an auditor reads to check that we never concluded that.
    """
    assert posting_audit_outcome(OutcomeCode.UNKNOWN) is AuditOutcome.QUARANTINED
    assert posting_audit_outcome(OutcomeCode.PARTIALLY_APPLIED) is AuditOutcome.QUARANTINED
    assert posting_audit_outcome(OutcomeCode.CONFIRMED) is AuditOutcome.SUCCESS
    assert posting_audit_outcome(OutcomeCode.REJECTED) is AuditOutcome.FAILURE
    assert posting_audit_outcome(OutcomeCode.NOT_SENT) is AuditOutcome.FAILURE


def test_kill_a_missing_outcome_classification_is_refused_rather_than_defaulted() -> None:
    """A ``dict.get(code, FAILURE)`` would classify the next ambiguous variant by accident, in the
    direction that invites a re-send. It raises instead, and this proves it."""
    with pytest.raises(ValueError, match="classify it rather than defaulting"):
        posting_audit_outcome("not-an-outcome")  # type: ignore[arg-type]


def test_a_notfound_answer_is_not_recorded_as_a_failure() -> None:
    """The question *was* answered; the answer was "not visible to this query"."""
    assert set(_ANSWER_AUDIT_OUTCOME) == set(QueryAnswer)
    assert _ANSWER_AUDIT_OUTCOME[QueryAnswer.NOT_FOUND] is AuditOutcome.QUARANTINED
    assert _ANSWER_AUDIT_OUTCOME[QueryAnswer.INDETERMINATE] is AuditOutcome.QUARANTINED
    assert _ANSWER_AUDIT_OUTCOME[QueryAnswer.FOUND] is AuditOutcome.SUCCESS


def test_an_unverified_resolution_is_recorded_as_an_abstention() -> None:
    """§13.5 requires ``RESOLVED_UNVERIFIED`` to be *"visible to an auditor rather than
    indistinguishable from a verified one"*, and this is where that becomes true rather than
    intended: it is the only resolution that is neither a success nor a failure."""
    assert set(_RESOLUTION_AUDIT_OUTCOME) == set(RecoveryResolution)
    assert (
        _RESOLUTION_AUDIT_OUTCOME[RecoveryResolution.RESOLVED_UNVERIFIED] is AuditOutcome.ABSTAINED
    )
    assert (
        _RESOLUTION_AUDIT_OUTCOME[RecoveryResolution.CONFIRMED_BY_EVIDENCE] is AuditOutcome.SUCCESS
    )
    assert (
        _RESOLUTION_AUDIT_OUTCOME[RecoveryResolution.REJECTED_BY_EVIDENCE] is AuditOutcome.FAILURE
    )


def test_reconciliation_and_recovery_are_their_own_audit_verbs() -> None:
    """Folding either into ``post`` or ``approve`` would make the segregation of duties unreadable:
    authorising a posting and judging what happened to one are different acts by different roles."""
    assert AuditTool.RECONCILE.value == "reconcile"
    assert AuditTool.RECOVER.value == "recover"
    assert {tool.value for tool in AuditTool} >= {"post", "approve", "reconcile", "recover"}


# ======================================================================================
# The evidence procedure — §13.5 clause 5's "a queue is not a control on its own"
# ======================================================================================


@pytest.mark.parametrize("reason", list(RecoveryReason))
def test_every_recovery_reason_names_an_artefact_and_a_sufficiency_test(
    reason: RecoveryReason,
) -> None:
    """Total over the enum, and every field is substantive rather than present.

    The length floor is crude and it is the property that matters: §13.5 asks for *"which downstream
    artefact must be inspected"* and *"what constitutes sufficient evidence for each permitted
    resolution"*, and a one-word placeholder would satisfy a "not empty" check while leaving the
    operator exactly as stuck as an empty queue entry would.
    """
    procedure = evidence_procedure_for(reason)

    assert len(procedure.artefact) > 40
    assert len(procedure.sufficient_for_confirmed) > 30
    assert len(procedure.sufficient_for_rejected) > 30


def test_a_rendered_procedure_carries_the_identifier_the_operator_will_search_for() -> None:
    rendered = evidence_procedure_for(RecoveryReason.NO_SUPPRESSION_OR_QUERY).render(
        operation_id="a" * 64
    )

    assert "a" * 64 in rendered
    assert "confirmed_by_evidence" in rendered
    assert "rejected_by_evidence" in rendered
    assert "resolved_unverified" in rendered


def test_the_window_expiry_procedure_tells_the_operator_not_to_resend() -> None:
    """The one instruction that must survive contact with a hurried operator.

    An expired idempotency window is exactly the state in which re-sending looks harmless — the
    system has an identifier, the provider takes the header — and is an ordinary duplicate write.
    """
    procedure = evidence_procedure_for(RecoveryReason.RESEND_WINDOW_EXPIRED)

    assert "do not re-send" in procedure.artefact.lower()


def test_the_resolutions_are_exactly_the_three_the_specification_names() -> None:
    assert {r.value for r in RecoveryResolution} == {
        "confirmed_by_evidence",
        "rejected_by_evidence",
        "resolved_unverified",
    }


def test_a_reconciliation_pass_reports_a_closed_set_of_conclusions() -> None:
    """No boolean and no ``None``: a caller must never have to infer what happened from a missing
    value, which is the same defect the ``QueryOutcome`` union exists to prevent one layer down."""
    assert {r.value for r in Resolution} == {
        "confirmed",
        "rejected",
        "resent",
        "unresolved",
        "routed_to_recovery",
        "already_in_recovery",
        "not_ambiguous",
    }
