"""Claiming a residual so that no two workers hold it at once (increment 4.1).

`PROJECT_SPEC.md` §13.1, Guarantee 1 — unconditional, and ours: *work is claimed with
``SELECT … FOR UPDATE SKIP LOCKED``; two workers cannot claim one residual.* This module is that
sentence, and it is an **at-most-one** guarantee rather than an exactly-one: for the duration of a
transaction the holder is alone, and when that transaction ends the residual is free again. 4.1
writes no status, so nothing marks a residual as handled — that transition belongs to the
dispatcher (4.2).

**The claim is the transaction, and there is no claim column anywhere.** A row lock lives exactly as
long as the transaction that took it, so a worker that dies loses its connection, PostgreSQL rolls
its transaction back, and the residual is claimable again. A ``claimed_by``/``claimed_at`` column
would have been the obvious design and would have been worse: it survives the process that wrote it,
so it needs an expiry policy, and an expiry policy that fires early hands one residual to two
workers — the exact thing being prevented.

**What that does and does not cover, stated precisely.** §14 asks that *"claimed work times out and
returns"*, and a transaction-scoped lock delivers it whenever the server learns the client is gone:
a crash, a kill, a closed socket. It does **not** cover a host that is frozen or partitioned, where
no FIN ever arrives — there the lock is held until TCP keepalive or a server-side timeout expires,
and this project configures neither. That is a real bound and it is written here rather than
implied away, because "needs no mechanism beyond that" was the first draft and it was an
overstatement. Choosing ``idle_in_transaction_session_timeout`` is a deployment decision that
belongs with the dispatcher that will hold these claims for real work (4.2), not with the claim
itself.

**Why ``SKIP LOCKED`` rather than the plain ``FOR UPDATE`` this repository already uses.** ADR-041
claims a settlement batch with a blocking lock, deliberately: two deliveries of one payload are the
*same* work, so the loser should wait and then observe a finished batch. Residuals are not that. Two
workers pulling from a queue want *different* rows, and a blocking lock would serialise them behind
each other for no reason — the second worker would sleep until the first finished, then wake to find
the row it waited for already handled. Skipping is what makes the queue concurrent. The rationale
does not transfer, so this module carries its own.

**Claiming decides nothing.** No status is written, no adjustment is created, nothing is dispatched.
Selecting *which* claimed residual is ready to post, and doing anything about it, belongs to the
dispatcher (4.2).
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.db.control import (
    ExceptionClassification,
    ExceptionRecord,
    ExceptionStatus,
)

__all__ = ["Claim", "ClaimedResidual", "claim_residuals"]


@dataclasses.dataclass(frozen=True, slots=True)
class ClaimedResidual:
    """One residual this worker holds for the life of its transaction.

    Deliberately not the ORM row. What a caller needs is the identity and enough provenance to
    correlate; handing back an attached instance would let a caller mutate exception state through
    an object this module returned, and nothing at 4.1 may change an exception's status.
    """

    exception_id: uuid.UUID

    #: A real enum member, coerced on the way out of the database rather than trusted from this
    #: annotation. ``exception.classification`` is a ``String(32)`` with a check constraint and no
    #: type decorator, so what the driver hands back is a bare ``str`` — which compares *and hashes*
    #: equal to a member, so a consumer's ``in`` test and ``==`` keep working while ``is`` silently
    #: fails and ``.value`` raises. That asymmetry is the M3.1 lesson, and the sibling module in
    #: this same package had to learn it again one layer down. Coercing also re-checks the stored
    #: text against the closed vocabulary.
    classification: ExceptionClassification
    correlation_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class Claim:
    """What one claim attempt obtained.

    ``requested`` is carried alongside so a caller — and a concurrency test — can tell "there was no
    work" from "the work was already held by somebody else". Both return an empty tuple, and they
    are different events: the first is an idle queue, the second is a busy one.
    """

    residuals: tuple[ClaimedResidual, ...]
    requested: int

    @property
    def claimed(self) -> int:
        return len(self.residuals)

    @property
    def exception_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(residual.exception_id for residual in self.residuals)


async def claim_residuals(session: AsyncSession, *, limit: int) -> Claim:
    """Take an exclusive claim on up to ``limit`` open residuals, skipping any already held.

    **The lock is released when the caller's transaction ends**, so the claim is only meaningful
    inside it, and a caller that commits or rolls back has released every residual here.

    A caller that never called ``session.begin()`` has still claimed them: SQLAlchemy autobegins on
    the first ``execute``, so the lock is taken and held until the session is committed, rolled back
    or closed. The first draft of this paragraph said the opposite — that such a caller "has claimed
    nothing at all" — which would have made a leaked session look harmless while it silently held
    every residual it had read.

    The ordering is total — ``created_at`` then ``id`` — so two workers walking the queue see the
    same sequence and contend at the front of it rather than at arbitrary points. Oldest first,
    because a residual that has waited longest is the one closest to breaching any SLA a later
    increment attaches to it.

    ``status = 'open'`` is the whole predicate. It is tempting to also exclude residuals that
    already have an adjustment, and that would be wrong here: deciding which claimed work is ready
    to dispatch is the dispatcher's judgement (4.2), and folding it into the claim would mean this
    module quietly owned a policy nobody wrote down.
    """
    if limit < 1:
        raise ValueError(f"a claim must ask for at least one residual, not {limit}")

    rows = (
        await session.execute(
            select(
                ExceptionRecord.id,
                ExceptionRecord.classification,
                ExceptionRecord.correlation_id,
            )
            .where(ExceptionRecord.status == ExceptionStatus.OPEN)
            .order_by(ExceptionRecord.created_at, ExceptionRecord.id)
            .limit(limit)
            # The two halves of Guarantee 1. `FOR UPDATE` makes the read exclusive; `SKIP LOCKED`
            # makes a contended row invisible to the second worker instead of making it wait.
            .with_for_update(skip_locked=True)
        )
    ).all()

    return Claim(
        residuals=tuple(
            ClaimedResidual(
                exception_id=row.id,
                classification=ExceptionClassification(row.classification),
                correlation_id=row.correlation_id,
            )
            for row in rows
        ),
        requested=limit,
    )
