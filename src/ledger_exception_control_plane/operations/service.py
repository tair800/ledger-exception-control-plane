"""Persisting the operation identifier before anything can dispatch it (increment 4.1).

`PROJECT_SPEC.md` §12.1.1: *the identifier is persisted before the external call, never derived at
call time.* This module is the only place in the package that writes an ``adjustment`` row, and a
guard test enforces that — the fence that used to say *nothing* writes one has been narrowed to
*exactly one module does*, which is a checkable claim rather than a weaker version of the old one.

**§12.1.1 is split between two increments, and the split is not arbitrary.** The subsection
contains two rules: the write-ahead attempt record before every socket write, which is 4.2's
because there is no socket here to write ahead of; and the sentence above, which is 4.1's because
it constrains *when* the identifier comes into existence rather than what happens to it
afterwards. An earlier version of this docstring attributed that sentence to §12.1 while the
package docstring assigned §12.1.1 wholesale to 4.2 — two statements that could not both be right.

**Why derived-at-call-time would be a defect rather than an inefficiency.** An identifier computed
at the moment of sending is computed from whatever the configuration happens to be at that moment.
A re-send after an account mapping changed would then send a *different* key for what the system
believes is the same operation — and under ``ENFORCES_KEY`` the provider would treat it as new work
and apply it twice. Persisting first means the key that goes on the wire is the key the decision was
recorded under, whatever has changed since.

**What this module refuses, and why the database cannot refuse it instead.** ``adjustment`` reaches
its exception only through ``approval``; there is no ``exception_id`` column to constrain. So the
composite foreign key proves the approval exists, that it authorised *some* treatment and that the
principal is real — and it cannot prove the priced instruction belongs to that exception, nor that
the amount was computed for the treatment the human actually authorised. Those two checks live here,
and both are refusals rather than reconciliations: pricing exception A and posting it under
exception B's authorisation is a financial defect no amount of downstream care recovers from.

**Two entry points, and the split is the point.** :func:`record_operation` records the identifier
and nothing else — it is 4.1's primitive and writes no dispatch intent, which a scope test still
pins. :func:`enqueue_posting` is 4.2's: it does the same work *and* writes the outbox row, in the
caller's single transaction, so §13.2's guarantee holds by construction rather than by discipline.

**Still nothing here sends.** No attempt record, no adapter call, no socket. §12.3 is explicit that
an operation identifier is a *request* for idempotent treatment and nothing more; what happens to it
belongs to the dispatcher, and is conditional on a declared adapter capability (§13.5).

Three properties a caller has to know, because none is visible from the signature:

- **The caller owns the transaction, and a flush is not a commit.** ``record_operation`` flushes so
  the row acquires its identity and the unique constraints do their work; it never commits. A
  caller that closes or rolls back has un-persisted an identifier it was told was ``created``. §12.1
  asks for the identifier to be persisted *before the external call*, and that obligation belongs
  to whoever owns the transaction the dispatch will run in.
- **Race-freedom here rests on READ COMMITTED**, PostgreSQL's default and this project's setting.
  The approval row lock serialises two callers; the second's *next* statement then takes a fresh
  snapshot and sees the first's committed row. Under REPEATABLE READ the second would keep its
  original snapshot, miss the row, and take a unique-constraint violation instead of a clean no-op.
- **Lock ordering is the caller's** when several approvals are recorded in one transaction. This
  function locks exactly one approval row. Two callers recording ``{A, B}`` and ``{B, A}`` in one
  transaction each will deadlock, and PostgreSQL will abort one of them. ADR-044's discipline —
  take row locks in a consistent order — applies to the loop, not to this function.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.db.control import (
    Adjustment,
    Approval,
    ApprovalDecision,
    Outbox,
    TreatmentCode,
)
from ledger_exception_control_plane.money import (
    AdjustmentInstruction,
    is_account_code,
    is_currency,
    is_period,
)
from ledger_exception_control_plane.operations.identity import (
    AmountNotStorableError,
    OperationIdentity,
    canonical_amount,
    derive_identity,
)

__all__ = [
    "IdentifierContradictionError",
    "OperationRecord",
    "RecordingRefusedError",
    "enqueue_posting",
    "record_operation",
]

#: The decisions that authorise a ledger write. A rejection authorises nothing, and the database
#: agrees: ``approved_treatment`` is NULL on a rejection and the ``adjustment`` column is NOT NULL.
_AUTHORISING: frozenset[ApprovalDecision] = frozenset(
    {ApprovalDecision.APPROVED, ApprovalDecision.EDITED}
)


class RecordingRefusedError(Exception):
    """The instruction and the approval do not describe the same authorised operation."""


class IdentifierContradictionError(Exception):
    """A stored identifier disagrees with the one the current inputs derive.

    Not a race and not a retry: the same approval has been asked to carry two different postings.
    Something that determines the financial effect changed after the identifier was recorded —
    account mapping, period configuration, the amount itself.

    Overwriting would be the worst available option. The stored identifier may already be on the
    wire, so replacing it would leave the system unable to recognise its own operation. Returning
    the stored one silently would hand back an identifier for a posting nobody computed. The
    specification's answer is that a genuinely different instruction is a *different operation*,
    reached by superseding the resolution — which increments ``resolution_version`` and is
    interlocked (4.4). Until that exists, this refuses and says so.
    """


def _treatment_of(instruction: AdjustmentInstruction) -> TreatmentCode:
    """The instruction's treatment as a real enum member.

    ``AdjustmentInstruction`` is a plain frozen dataclass with no runtime type check, so its
    ``treatment`` can hold a bare ``str`` — which is exactly what a caller gets when it rebuilds an
    instruction from persisted values, since ``adjustment.approved_treatment`` is a ``String(16)``
    with no type decorator.

    The first version compared with ``is not``, chosen to avoid ``StrEnum``'s equal-to-its-value
    behaviour, and a bare string therefore took the *refusal* branch and then raised
    ``AttributeError`` on ``.value`` — a crash on the path whose whole job is to refuse cleanly.
    Coercing first fixes both halves: an out-of-vocabulary value is refused here, and everything
    downstream has a member rather than something that merely compares like one.
    """
    try:
        return TreatmentCode(instruction.treatment)
    except ValueError as exc:
        raise RecordingRefusedError(
            f"the instruction names {instruction.treatment!r}, which is not a treatment"
        ) from exc


def _assert_instruction_is_postable(
    instruction: AdjustmentInstruction, treatment: TreatmentCode
) -> None:
    """Everything about the instruction that the database will not catch.

    ``record_operation`` takes an ``AdjustmentInstruction``, and that type is a plain dataclass: it
    validates nothing, so "it came from the calculator" is a convention rather than a fact. An
    adversarial review walked a 48-character account code carrying SQL punctuation straight through
    to a persisted, identified financial instruction.

    Each check below is here because the database cannot make it, or cannot make it *cheaply*:

    - **The account code has no constraint at all.** ``adjustment.account_code`` is a bare
      ``String(64)``. The money policy's rule — four digits — is the only definition there is, and
      nothing was consulting it on this path.
    - **A zero amount** is refused by the calculator (§7: an instruction to do nothing, carrying
      the full weight of an approved financial instruction) and by no constraint. Without this an
      instruction the calculator would never emit is storable.
    - **Escalation** has a constraint, and reaching it costs the caller their transaction: a failed
      flush deactivates the session, so a refusal that arrives as an ``IntegrityError`` takes the
      whole unit of work with it. §6.2 makes this a contradiction rather than an edge case — an
      amount computed for the case that was referred *because* no amount could be computed.
    - **Period and currency** have constraints, refused here for the same transaction-cost reason.
    - **The amount's storability** is re-checked by deriving its canonical form, which is what the
      identifier binds anyway.
    """
    if treatment is TreatmentCode.ESCALATE:
        raise RecordingRefusedError(
            "an escalated treatment has no priceable amount (§6.2), so it can never be posted"
        )
    if not is_account_code(instruction.account_code):
        raise RecordingRefusedError(
            f"{instruction.account_code!r} is not a ledger account code; the column would store it "
            "and nothing else would notice"
        )
    if not is_period(instruction.period):
        raise RecordingRefusedError(f"{instruction.period!r} is not a YYYY-MM accounting period")
    if not is_currency(instruction.currency):
        raise RecordingRefusedError(f"{instruction.currency!r} is not an ISO 4217 code")

    try:
        canonical = canonical_amount(instruction.amount)
    except AmountNotStorableError as exc:
        raise RecordingRefusedError(f"the amount cannot be stored exactly: {exc}") from exc
    if instruction.amount == 0:
        raise RecordingRefusedError(
            f"an adjustment of {canonical} instructs the ledger to do nothing while carrying the "
            "full weight of an approved financial instruction (§7)"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class OperationRecord:
    """The persisted identity, and whether this call is what created it."""

    adjustment_id: uuid.UUID
    identity: OperationIdentity
    created: bool


async def record_operation(
    session: AsyncSession, *, approval_id: uuid.UUID, instruction: AdjustmentInstruction
) -> OperationRecord:
    """Derive the operation identifier for an approved resolution and store it.

    Idempotent by re-derivation rather than by a flag. Called twice for one approval it returns the
    same record and writes nothing the second time — and if the second derivation disagrees with
    what is stored it raises, because that is the case where silence would be dangerous.

    The approval is **read back under a row lock** rather than trusted from the caller's arguments.
    Two callers racing to record the same approval would otherwise both derive, both insert, and one
    would take an integrity error from ``uq_adjustment_approval_id`` — the constraint doing its job,
    but reported as a crash rather than as the no-op it actually is. The lock makes the second
    caller wait and then observe the finished work, which is ADR-041's reasoning applied to a
    different table.
    """
    approval = (
        await session.execute(
            select(Approval)
            .where(Approval.id == approval_id)
            # `populate_existing` is not decoration. Without it, an `Approval` already in the
            # session's identity map keeps its in-memory column values: the SELECT takes the lock
            # and fetches the current row, and the ORM hands back the stale instance anyway. The
            # docstring above says the approval is *read back* under the lock, and this is what
            # makes that true. Latent at 4.1, because no caller here loads an approval first —
            # 4.2's dispatcher, which must read one to build the instruction, is exactly the caller
            # that makes it live. `resolution_version` is the value at risk: it feeds the
            # identifier and, unlike the treatment and the principal, no column on `adjustment`
            # re-checks it.
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if approval is None:
        raise RecordingRefusedError(f"no approval {approval_id}; nothing authorises this write")

    # Re-typed at the boundary rather than trusted from the annotation. These columns are
    # ``String(16)`` with a check constraint and no type decorator, so what comes back is a plain
    # `str` however the model is annotated — a `StrEnum` compares *and hashes* equal to its own
    # value, so membership tests kept working while ``.value`` raised ``AttributeError``. That is
    # the same asymmetry the M3.1 gate was written for, met again one layer down. Converting also
    # re-checks the stored text against the closed vocabulary, which costs nothing and means an
    # out-of-vocabulary value fails here rather than downstream.
    decision = ApprovalDecision(approval.decision)
    if decision not in _AUTHORISING:
        raise RecordingRefusedError(
            f"approval {approval_id} is {decision.value} and authorises no ledger write"
        )
    if approval.approved_treatment is None:  # pragma: no cover - the database forbids this pairing
        raise RecordingRefusedError(f"approval {approval_id} authorises no treatment")
    authorised = TreatmentCode(approval.approved_treatment)

    if instruction.exception_id != approval.exception_id:
        raise RecordingRefusedError(
            f"the instruction prices exception {instruction.exception_id} but approval "
            f"{approval_id} authorises exception {approval.exception_id}"
        )

    priced = _treatment_of(instruction)
    if priced is not authorised:
        raise RecordingRefusedError(
            f"the instruction prices {priced.value} but approval {approval_id} "
            f"authorised {authorised.value}"
        )

    _assert_instruction_is_postable(instruction, priced)

    identity = derive_identity(
        instruction,
        exception_id=approval.exception_id,
        resolution_version=approval.resolution_version,
    )

    existing = (
        await session.execute(select(Adjustment).where(Adjustment.approval_id == approval_id))
    ).scalar_one_or_none()

    if existing is not None:
        # Both digests, not just the identifier. They are stored in two independent columns with
        # nothing tying them together beyond a hex-shape check, so a row whose identifier is right
        # and whose payload hash is not would otherwise be returned as agreement — and the payload
        # hash is the one that says *what* was priced. ADR-030's rule for exactly this shape is
        # that a duplicated value is verified rather than copied.
        if (
            existing.operation_id != identity.operation_id
            or existing.instruction_payload_hash != identity.instruction_payload_hash
        ):
            raise IdentifierContradictionError(
                f"approval {approval_id} already carries operation "
                f"{existing.operation_id[:12]}… but these inputs derive "
                f"{identity.operation_id[:12]}…; a changed instruction is a different "
                "operation and needs a superseding resolution, not an overwrite"
            )
        return OperationRecord(
            adjustment_id=existing.id,
            identity=OperationIdentity(
                operation_id=existing.operation_id,
                instruction_payload_hash=existing.instruction_payload_hash,
            ),
            created=False,
        )

    adjustment = Adjustment(
        approval_id=approval.id,
        # Carried from the approval, never from the caller. The composite foreign key re-checks both
        # against the approval row, so a value invented here cannot be stored.
        approved_treatment=authorised,
        approving_principal=approval.principal,
        amount=instruction.amount,
        currency=instruction.currency,
        account_code=instruction.account_code,
        period=instruction.period,
        operation_id=identity.operation_id,
        instruction_payload_hash=identity.instruction_payload_hash,
    )
    session.add(adjustment)
    await session.flush()

    return OperationRecord(adjustment_id=adjustment.id, identity=identity, created=True)


async def enqueue_posting(
    session: AsyncSession, *, approval_id: uuid.UUID, instruction: AdjustmentInstruction
) -> OperationRecord:
    """Record the operation **and** its dispatch intent, in one transaction (§13.2, Guarantee 2).

    *"The state change and the dispatch intent are written in a single database transaction. There
    is no committed approval without an outbox row, and no outbox row without a committed
    approval."* Both halves are the caller's single transaction here: this function flushes and
    never commits, so if the caller rolls back, neither row exists — and if the caller commits, both
    do. There is no window in which one is durable and the other is not, because there is no second
    transaction for a crash to fall between.

    **Deliberately at-least-once, and that is not a shortfall.** The outbox guarantees the intent is
    not *lost*. It does not, and cannot, guarantee the intent is delivered only once — conflating
    those two is, in the specification's words, the most common error in this pattern. Delivery is
    the dispatcher's problem and duplicate *effect* is the adapter capability's.

    Idempotent for the same reason :func:`record_operation` is: called twice for one approval it
    returns the same record and adds no second intent. ``uq_outbox_adjustment_id`` is the guarantee
    behind that, and this only spares the caller an integrity error where a no-op is the truth.
    """
    record = await record_operation(session, approval_id=approval_id, instruction=instruction)

    existing = (
        await session.execute(select(Outbox).where(Outbox.adjustment_id == record.adjustment_id))
    ).scalar_one_or_none()
    if existing is None:
        # `state` and `attempt_count` take their column defaults — pending, zero. `next_attempt_at`
        # stays NULL: scheduling is 4.3's, and a value written here would be this increment quietly
        # deciding a retry policy nobody has specified.
        session.add(Outbox(adjustment_id=record.adjustment_id))
        await session.flush()

    return record
