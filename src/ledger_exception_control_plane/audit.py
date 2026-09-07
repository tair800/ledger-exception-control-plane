"""Audit-event contract v1 — the portfolio's canonical audit shape (increments 4.4 and 5.2).

`PROJECT_SPEC.md` §11 fixes the field set; M1.2 built the table and the trigger that makes it
append-only. This module is the one place that writes to it, and 5.2 is the increment that makes
that sentence load-bearing across the whole pipeline rather than across one increment's transitions.

**Why an emitter rather than an ORM call at each site.** Contract v1 is a *portfolio* contract —
seven later repositories copy it — so what matters is that every event has the same shape and that
the shape is decided once. Eleven fields, four of which are only meaningful when a model was
involved, is exactly the sort of structure that drifts when each call site fills it in by hand.

**And it keeps every entity-writer fence intact.** This project asserts, per guarded entity, that
exactly one module may construct it. 5.2 adds emission to six more modules across matching, the
model layer, the money path and the operations package — and widens *no* fence, because none of
those modules constructs an :class:`AuditEvent`. They call :func:`emit`. A trail that is orthogonal
to the write discipline can be added everywhere without eroding it anywhere.

**The vocabulary is closed, and widening it needs a clause.** §11 lists eight tools; 4.4 added
``reconcile`` and ``recover`` because §13.5 clause 6 requires an event for *"every reconciliation
query and result, and every manual decision"* and no existing verb names those acts. That is the
rule, stated once here: **a verb is added only when a specification clause requires an event for an
action the existing verbs cannot name.** Ingestion, quarantine, classification and evidence assembly
are therefore *not* audited — no clause requires it, and none of them is ledger-affecting. Their
provenance lives in their own tables, which the correlation id joins to. ADR-058 records this.

**What never reaches an audit row.** No amount, no evidence text, no rationale, no provider message,
no token, no DSN, no merchant identifier. §11's field set is deliberately about *who did what under
which authority with what outcome* — the financial facts live in `adjustment` and the reasoning in
`treatment_proposal`, both of which the correlation id already joins to. An audit trail that
duplicated the amount would be a second copy of a number with exactly one owner, and §16 forbids
merchant identifiers in telemetry outright. A guard test walks every call site and enforces it.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    AuditApprovalDecision,
    AuditEvent,
    AuditOutcome,
    AuditTool,
    ExceptionRecord,
)
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode

__all__ = [
    "NO_AUTHORITY",
    "SYSTEM_PRINCIPAL",
    "UNRECORDED_CORRELATION_ID",
    "Scope",
    "approval_scope",
    "correlation_for_adjustment",
    "correlation_for_exception",
    "correlation_id_for",
    "emit",
    "is_known_scope",
    "model_identity",
    "posting_audit_outcome",
    "scope_for",
]

#: What an event records when the correlation chain cannot be walked.
#:
#: §11 makes ``correlation_id`` NOT NULL, so there is no "leave it out" option, and inventing a
#: fresh identifier would be worse than admitting the gap: it would look like a trace that simply
#: has no other members. A fixed, obviously-synthetic value is greppable and says what happened.
#: In practice this is unreachable for a dispatched adjustment — the chain adjustment → approval →
#: exception is all NOT NULL foreign keys — and a test asserts the real path is taken.
UNRECORDED_CORRELATION_ID = "lecp-correlation-unrecorded"

#: The correlation id prefix. Short, fixed, and greppable across logs, spans and audit rows.
CORRELATION_PREFIX: Final = "lecp"


def correlation_id_for(content_hash: str, line_number: int) -> str:
    """The correlation id every record about one settlement line carries (§11, §18).

    **Derived from the ingested artefact, never taken from an ambient request context**, and that is
    what makes §18's *"generated at ingestion, propagated through every layer"* true rather than
    aspirational. The span is a property of the data: any stage can compute the id from the content
    hash of the file the line arrived in and its position within that file, so no layer has to
    remember to thread a value through, and no layer can mint a fresh one and silently break the
    chain.

    It is also *stable*. Re-running a stage after a crash produces the same id for the same economic
    event, where a request-scoped id would produce a new one — and a trail whose identifier changes
    on retry is a trail that cannot be followed across exactly the failures this project is about.

    Content hash rather than batch id because the hash is the identity of the payload (FR-1), so a
    re-delivery of the same file yields the same id for the same line.

    **This lives here rather than in `classification` as of 5.2.** §11 owns the correlation id, and
    by 5.2 three stages need it — matching, classification and the emitter. Leaving the canonical
    derivation in the module that happened to need it first would have made matching import
    classification, which is the pipeline running backwards. ``classification`` re-exports it, so
    every existing import still resolves and a test asserts the two names are the same object.
    """
    return f"{CORRELATION_PREFIX}:{content_hash}:{line_number:06d}"


#: The principal recorded for a deterministic step nobody authorised individually.
#:
#: §11: *"Authenticated human, or `system`"*. Writing the literal in one place stops it drifting
#: into "SYSTEM", "internal" and "svc" across call sites, which would make the trail unfilterable by
#: exactly the distinction it exists to draw.
SYSTEM_PRINCIPAL = "system"


async def emit(
    session: AsyncSession,
    *,
    tool: AuditTool,
    outcome: AuditOutcome,
    correlation_id: str,
    occurred_at: dt.datetime,
    scope_granted: str,
    principal: str = SYSTEM_PRINCIPAL,
    approval_decision: AuditApprovalDecision = AuditApprovalDecision.NOT_APPLICABLE,
    approver: str | None = None,
    agent_identity: str | None = None,
    model: str | None = None,
    region_jurisdiction: str | None = None,
) -> None:
    """Append one event. Written into the caller's transaction, deliberately.

    The event and the state change it describes commit together or not at all. The alternative — a
    separate transaction — produces exactly two failure modes, both bad: an event for a transition
    that rolled back, or a transition with no event. §11 requires *"every ledger-affecting action
    has at least one event"*, and the only way to mean that is to make the event part of the action.

    ``occurred_at`` is a parameter for the same reason ``sent_at`` is one on the dispatcher: a clock
    reading taken inside this function would make the trail depend on when the code ran rather than
    on when the thing happened.

    ``scope_granted`` is §11's *"authorisation under which the action ran"* — the role for a human
    action, or the named internal capability for a deterministic one. It is required rather than
    defaulted, because an event that cannot say under what authority it happened is not an audit
    record, and a default would let a call site skip the one field that carries the authorisation
    question.
    """
    if not is_known_scope(scope_granted):
        raise ValueError(
            f"{scope_granted!r} is not a scope this contract admits. §11's authorisation field is "
            "queryable only if its values come from one vocabulary; see Scope, approval_scope and "
            "NO_AUTHORITY."
        )

    session.add(
        AuditEvent(
            occurred_at=occurred_at,
            principal=principal,
            agent_identity=agent_identity,
            tool=tool,
            scope_granted=scope_granted,
            approval_decision=approval_decision,
            approver=approver,
            model=model,
            region_jurisdiction=region_jurisdiction,
            outcome=outcome,
            correlation_id=correlation_id,
        )
    )


#: How a persisted posting outcome reads in the audit trail.
#:
#: Four audit outcomes and six posting outcomes, so the mapping is lossy by construction and the
#: trail is not where the detail lives — ``posting_attempt`` holds that, and the correlation id
#: joins the two. What this mapping must get right is the **three-way** distinction §13.5 rests on,
#: and it is the reason ``QUARANTINED`` is used rather than folding ambiguity into failure:
#:
#: - applied → ``SUCCESS``;
#: - definitely not applied → ``FAILURE``, which covers a declination, a throttle that turned the
#:   request away before it could be considered, and a transport failure with no byte written;
#: - **undetermined → ``QUARANTINED``**, meaning held aside for a decision rather than decided.
#:
#: Recording an ``UNKNOWN`` as ``FAILURE`` would put the exact coercion this project exists to
#: prevent into the one record an auditor reads to check it did not happen.
_POSTING_AUDIT_OUTCOME: dict[OutcomeCode, AuditOutcome] = {
    OutcomeCode.CONFIRMED: AuditOutcome.SUCCESS,
    OutcomeCode.REJECTED: AuditOutcome.FAILURE,
    OutcomeCode.THROTTLED: AuditOutcome.FAILURE,
    OutcomeCode.NOT_SENT: AuditOutcome.FAILURE,
    OutcomeCode.UNKNOWN: AuditOutcome.QUARANTINED,
    OutcomeCode.PARTIALLY_APPLIED: AuditOutcome.QUARANTINED,
}


def posting_audit_outcome(code: OutcomeCode) -> AuditOutcome:
    """The audit reading of a posting outcome. Total over the enum, and a test proves it.

    Total rather than defaulted: a new posting outcome must be classified deliberately, and a
    ``dict.get(code, FAILURE)`` would classify the next ambiguous variant as a failure by
    accident — silently, and in the direction that invites a re-send.
    """
    try:
        return _POSTING_AUDIT_OUTCOME[code]
    except KeyError:  # pragma: no cover - the test above makes this unreachable
        raise ValueError(
            f"{code!r} has no audit reading; classify it rather than defaulting, because the "
            "default direction for an unclassified outcome is the unsafe one"
        ) from None


async def correlation_for_adjustment(session: AsyncSession, adjustment_id: uuid.UUID) -> str:
    """The correlation id spanning ingestion to posting, for one adjustment.

    §11: *"Every record carries a correlation id that survives the full path from ingestion to
    ledger posting."* The chain is ``adjustment → approval → exception``, and every link is a NOT
    NULL foreign key — so this is a lookup rather than a search, and a miss means the adjustment
    itself does not exist.
    """
    found = (
        await session.execute(
            select(ExceptionRecord.correlation_id)
            .join(Approval, Approval.exception_id == ExceptionRecord.id)
            .join(Adjustment, Adjustment.approval_id == Approval.id)
            .where(Adjustment.id == adjustment_id)
        )
    ).scalar_one_or_none()
    return found or UNRECORDED_CORRELATION_ID


class Scope(enum.StrEnum):
    """§11's *"authorisation under which the action ran"*, as a closed vocabulary.

    **Free text here would make the field unqueryable, which defeats the point of having it.** An
    auditor asking "what ran under the model's authority" needs to filter, and a filter over strings
    each call site invented is a filter over spelling. 4.4 wrote three of these as module constants
    in three modules; 5.2 needed six more, and nine literals scattered across nine files is the
    drift this contract exists to prevent.

    Every member names an *internal capability*, never a person. The one authority that belongs to a
    human is an approval, and that one is role-parameterised — see :func:`approval_scope`.

    A test asserts every :class:`~.db.control.AuditTool` maps to a scope here, and that no call site
    passes a ``scope_granted`` outside this vocabulary or the approval form.
    """

    #: Deterministic matching against ledger entries.
    MATCH = "matching:auto"

    #: A model call proposing a treatment code. The only scope under which a model acts at all.
    PROPOSE = "llm:propose"

    #: The deterministic amount calculation, run under an approval that already authorised it.
    COMPUTE = "money:compute"

    #: One posting attempt against a ledger adapter.
    POST = "ledger:post"

    #: Scheduling a further attempt after an allowlisted transport failure.
    RETRY = "ledger:retry"

    #: Asking a ledger what happened to an operation whose outcome is undetermined.
    RECONCILE = "ledger:reconcile"

    #: Writing a dead letter after a bounded retry ran out.
    DEAD_LETTER = "operations:dead_letter"

    #: An operator replaying a dead letter.
    REPLAY = "operations:replay"

    #: An operator judging what happened to an ambiguous posting.
    RECOVER = "operations:recover"


#: The scope each tool runs under. Total over :class:`~.db.control.AuditTool` by construction, and a
#: test proves it — a tool with no scope would be an action nobody could say the authority for.
#:
#: ``APPROVE`` is absent on purpose and is the only absence: it is the one action a *person* takes,
#: so its scope carries the role rather than a capability name. See :func:`approval_scope`.
_TOOL_SCOPE: Final[dict[AuditTool, Scope]] = {
    AuditTool.MATCH: Scope.MATCH,
    AuditTool.PROPOSE_TREATMENT: Scope.PROPOSE,
    AuditTool.COMPUTE_AMOUNT: Scope.COMPUTE,
    AuditTool.POST: Scope.POST,
    AuditTool.RETRY: Scope.RETRY,
    AuditTool.RECONCILE: Scope.RECONCILE,
    AuditTool.DLQ: Scope.DEAD_LETTER,
    AuditTool.REPLAY: Scope.REPLAY,
    AuditTool.RECOVER: Scope.RECOVER,
}

#: How an approval's scope is spelled. The role, because §16's whole control is role separation and
#: an audit trail that recorded "approval" without saying *which kind of principal* could not show
#: that an operator never approved anything.
APPROVAL_SCOPE_PREFIX: Final = "approval"


#: The scope of an action refused because the principal held no authority to take it.
#:
#: **This value exists because the first version recorded a lie.** 4.4's refusal path stamped
#: ``approval:<role>`` on every refused approval, including one refused *precisely because that role
#: may not approve* — so an operator's blocked attempt wrote a permanent, undeletable row asserting
#: an authorisation that does not exist under §16's role separation. The one field §11 provides for
#: answering "under what authority did this happen" said the opposite of the truth, in the one table
#: that cannot be corrected afterwards.
#:
#: §11 asks for *"the authorisation under which the action ran"*. Where the answer is "none", the
#: honest record says none. A refusal for some *other* reason — a replayed token, a supersession
#: interlock — keeps the real scope, because there the authority was genuinely held and the refusal
#: was about something else.
NO_AUTHORITY: Final = "none"


def approval_scope(role: str) -> str:
    """The scope for a human decision, carrying the role the token resolved to.

    Takes the role as a string rather than the :class:`~.security.Role` enum, so this module does
    not import the security layer — the emitter is copied into seven later repositories that will
    have their own principal model, and a dependency on this project's would travel with it.
    """
    return f"{APPROVAL_SCOPE_PREFIX}:{role}"


def is_known_scope(scope: str) -> bool:
    """Whether a scope is one this contract admits.

    Three shapes and no others: a member of :class:`Scope`, the approval form carrying a non-empty
    role, or :data:`NO_AUTHORITY`. Free text would make §11's authorisation field unqueryable, which
    is most of the reason for having it — an auditor asking "what ran under the model's authority"
    would be filtering on spelling.
    """
    if scope == NO_AUTHORITY or scope in {member.value for member in Scope}:
        return True
    prefix = f"{APPROVAL_SCOPE_PREFIX}:"
    return scope.startswith(prefix) and len(scope) > len(prefix)


def scope_for(tool: AuditTool) -> Scope:
    """The scope a deterministic tool runs under. Raises for ``APPROVE``, which is not one.

    Total rather than defaulted, for the reason every mapping in this project is: a new tool must be
    given an authority deliberately, and a default would let one be recorded under whichever scope
    happened to be first in the dictionary.
    """
    try:
        return _TOOL_SCOPE[tool]
    except KeyError:
        raise ValueError(
            f"{tool!r} has no fixed scope. `approve` is the only tool that should reach here, and "
            "its authority is the approver's role — use approval_scope(role)."
        ) from None


def model_identity(model_id: str, model_version: str) -> str:
    """§11's ``model`` field: *"Model id and version, where a model was involved"*, as one value.

    One helper rather than an f-string at each site, so the id and the version are never recorded
    apart and never joined two different ways. The pair is what makes the field answerable — a bare
    model id cannot distinguish two versions that behave differently, which is the whole reason §11
    asks for both.

    **What this value does and does not assert.** It says the proposal is attributed to that model
    under that version: it is the model the deployment configured, the contract the answer was
    validated against, and the pair persisted on ``treatment_proposal``. It does *not* assert that
    bytes crossed a network — every cassette committed to this repository is marked ``synthesised``
    rather than captured, and a test enforces that marking. An auditor asking "which model is this
    decision attributed to" gets a true answer here; one asking "did a call happen" reads the
    cassette's own origin, which is where that fact lives. ADR-058 records the split.
    """
    return f"{model_id}@{model_version}"


async def correlation_for_exception(session: AsyncSession, exception_id: uuid.UUID) -> str:
    """The correlation id for one exception. A column read, not a search.

    ``exception.correlation_id`` is NOT NULL and was written at classification from
    :func:`correlation_id_for`, so this is the same value every other stage computes or joins to.
    """
    found = (
        await session.execute(
            select(ExceptionRecord.correlation_id).where(ExceptionRecord.id == exception_id)
        )
    ).scalar_one_or_none()
    return found or UNRECORDED_CORRELATION_ID
