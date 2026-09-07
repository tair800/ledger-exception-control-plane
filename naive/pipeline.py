"""The naive baseline — what you write before you have thought about duplicate side effects.

`PROJECT_SPEC.md` §19: *"a deliberately unsafe baseline: no idempotency key, no outbox, no claim
locking. **It must double-post.**"*

**Read this as ordinary code, because it is.** Nothing here is sabotage. Work is claimed by reading
the open rows and updating them. The adjustment is inserted and then the posting is sent, each in
its own transaction, because those are the two statements the job needs. A failed send is retried,
because retrying a failed request is ordinary good practice. Every one of those is a decision a
competent engineer makes on a Tuesday, and every one of them is wrong in a way that only shows up
under the faults in §19.

The defects, named so nobody has to reverse-engineer them:

1. **No idempotency key.** :func:`dispatch` sends a fresh request identifier per attempt. That is
   what an identifier is *for* when nobody has asked the provider to deduplicate on it — a value to
   correlate logs by. §19 defines the baseline as having no idempotency key, and this is what that
   means at the call site. :func:`dispatch_with_a_stable_key` exists so the demonstration does not
   rest on this choice; see its docstring.
2. **The ambiguity is read as a failure.** ``Unknown`` is not a shape this code knows, so anything
   that is not a confirmation is retried. This is the single defect the whole reliability layer
   exists to prevent, and it is also the most natural code in the file: ``if not ok: retry``.
3. **No write-ahead record.** Nothing is committed before the socket write, so a crash mid-send
   leaves no evidence a send happened.
4. **No transaction spanning the state change and the send.** The row and the effect can diverge in
   either direction.
5. **No claim locking.** Two workers reading the same open row both get it.
6. **No single-use approval token.** A replayed token is a second approval.
7. **No content-hash uniqueness.** A redelivered payload is a second batch and a second residual.

It imports the ledger port, the reference adapters and the fault vocabulary from ``src`` — the same
world the real implementation faces, which is what makes the comparison worth anything — and it
imports nothing else from there. It shares no reliability code, no schema, and no transaction
discipline. A guard test asserts ``src`` never imports this.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import uuid
from typing import Final

import asyncpg

from ledger_exception_control_plane.ledger.port import (
    Confirmed,
    LedgerAdapter,
    PostingInstruction,
)

__all__ = [
    "NAIVE_RETRIES",
    "NaiveResidual",
    "approve",
    "claim_open_residuals",
    "dispatch",
    "dispatch_with_a_stable_key",
    "ingest",
    "record_adjustment",
]

#: How many times a failed send is retried.
#:
#: Three, which is the number everybody picks. It is not the defect — ``src`` retries too, under two
#: independent bounds. The defect is *what* gets retried: see :func:`dispatch`.
NAIVE_RETRIES: Final = 3


@dataclasses.dataclass(frozen=True, slots=True)
class NaiveResidual:
    """One piece of work to post."""

    id: uuid.UUID
    reference: str
    amount: decimal.Decimal
    currency: str
    account_code: str
    period: str


async def ingest(
    connection: asyncpg.Connection,
    *,
    content_hash: str,
    residuals: list[tuple[str, str]],
    received_at: dt.datetime,
) -> uuid.UUID:
    """Store a delivered batch and its residual lines.

    **No uniqueness on ``content_hash``.** Nothing here checks whether this payload has been seen
    before, because nothing here has been asked to think about redelivery — a webhook that fires
    twice produces two batches and two sets of work.
    """
    batch_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO naive_batch (id, content_hash, received_at) VALUES ($1, $2, $3)",
        batch_id,
        content_hash,
        received_at,
    )
    for reference, amount in residuals:
        await connection.execute(
            "INSERT INTO naive_residual"
            " (id, batch_id, reference, amount, currency, account_code, period)"
            " VALUES ($1, $2, $3, $4, 'EUR', '4100', '2026-06')",
            uuid.uuid4(),
            batch_id,
            reference,
            decimal.Decimal(amount),
        )
    return batch_id


async def claim_open_residuals(
    connection: asyncpg.Connection, *, worker: str, limit: int = 10
) -> list[NaiveResidual]:
    """Take the open work and mark it claimed.

    **Read, then update — no lock.** The obvious two statements, and between them another
    worker can run the same read and get the same rows. ``src`` takes ``SELECT … FOR UPDATE
    SKIP LOCKED`` in one statement precisely so that window does not exist.
    """
    rows = await connection.fetch(
        "SELECT id, reference, amount, currency, account_code, period"
        " FROM naive_residual WHERE state = 'open' ORDER BY reference LIMIT $1",
        limit,
    )
    claimed = [
        NaiveResidual(
            id=row["id"],
            reference=row["reference"],
            amount=row["amount"],
            currency=row["currency"],
            account_code=row["account_code"],
            period=row["period"],
        )
        for row in rows
    ]
    if claimed:
        await connection.execute(
            "UPDATE naive_residual SET state = 'claimed', claimed_by = $1"
            " WHERE id = ANY($2::uuid[])",
            worker,
            [residual.id for residual in claimed],
        )
    return claimed


async def approve(
    connection: asyncpg.Connection,
    *,
    residual_id: uuid.UUID,
    principal: str,
    token: str,
    decided_at: dt.datetime,
) -> uuid.UUID:
    """Record an approval.

    **No uniqueness on the token.** The token is stored because it arrived, not because anything
    checks it has never been used — so presenting a consumed one again records a second approval,
    and a second approval authorises a second posting.
    """
    approval_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO naive_approval (id, residual_id, principal, token, decided_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        approval_id,
        residual_id,
        principal,
        token,
        decided_at,
    )
    return approval_id


async def record_adjustment(
    connection: asyncpg.Connection, *, residual: NaiveResidual, approval_id: uuid.UUID
) -> uuid.UUID:
    """Write the adjustment row.

    **No operation identifier and no uniqueness.** The row records what to post; nothing about it
    says *which posting this is*, so nothing can notice a second row for the same work — and nothing
    can tie a send to the row it came from.
    """
    adjustment_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO naive_adjustment"
        " (id, residual_id, approval_id, amount, currency, account_code, period)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)",
        adjustment_id,
        residual.id,
        approval_id,
        residual.amount,
        residual.currency,
        residual.account_code,
        residual.period,
    )
    return adjustment_id


def _instruction(residual: NaiveResidual, adjustment_id: uuid.UUID) -> PostingInstruction:
    return PostingInstruction(
        adjustment_id=adjustment_id,
        amount=residual.amount,
        currency=residual.currency,
        account_code=residual.account_code,
        period=residual.period,
    )


async def dispatch(
    connection: asyncpg.Connection,
    adapter: LedgerAdapter,
    *,
    residual: NaiveResidual,
    adjustment_id: uuid.UUID,
    retries: int = NAIVE_RETRIES,
) -> str | None:
    """Send the posting, retrying a failure. Returns the reference, or ``None`` if it never landed.

    **The two defects that matter are both here, and both look like good practice.**

    *A fresh request identifier per attempt.* §19 defines this baseline as having no idempotency
    key, and this is what that looks like at a call site: the parameter exists, so it gets a value,
    and the value that makes sense for a log line is a new one each time. Nothing has asked the
    provider to deduplicate on it, so nothing keeps it stable. An enforcing ledger given a different
    key each attempt suppresses nothing — which is §12.3's point read backwards: sending an
    identifier is a *request* for idempotent treatment, and a request nobody makes twice the same
    way is no request at all.

    *Anything that is not a confirmation is retried.* ``Unknown`` is not a shape this code knows.
    The five-valued outcome union is `src`'s; here the answer is either the reference or it is not,
    and "not" means try again. This is the exact inference `PROJECT_SPEC.md` §13.5 forbids —
    *"retrying an ambiguous financial write on the assumption it failed"* — and it is also the most
    ordinary line in the file.

    Nothing is committed before the send and nothing spans the two, so a crash between them leaves
    no evidence a send occurred.
    """
    instruction = _instruction(residual, adjustment_id)

    for _ in range(retries):
        # A fresh request identifier per attempt. See above.
        request_id = uuid.uuid4().hex + uuid.uuid4().hex
        try:
            outcome = await adapter.post(request_id, instruction)
        except Exception:
            # The transport raised. Retry — which is right when nothing was sent, and is the
            # double-post when the request landed and only the response was lost. Nothing here can
            # tell those apart, and nothing here tries.
            continue

        if isinstance(outcome, Confirmed):
            await connection.execute(
                "UPDATE naive_adjustment SET posting_ref = $1 WHERE id = $2",
                outcome.posting_ref,
                adjustment_id,
            )
            await connection.execute(
                "UPDATE naive_residual SET state = 'posted' WHERE id = $1", residual.id
            )
            return outcome.posting_ref

        # Not a confirmation. Try again.
        continue

    return None


async def dispatch_with_a_stable_key(
    connection: asyncpg.Connection,
    adapter: LedgerAdapter,
    *,
    residual: NaiveResidual,
    adjustment_id: uuid.UUID,
    retries: int = NAIVE_RETRIES,
) -> str | None:
    """The same dispatch, sending the **adjustment id** as the key on every attempt.

    **This exists so the demonstration does not rest on one debatable choice.** The obvious
    objection to :func:`dispatch` is that its fresh-key-per-attempt is what causes the duplicate,
    and that a slightly luckier naive implementation — one that happened to pass a stable row id —
    would not double-post. That objection is half right, and the half it gets right is worth
    measuring rather than arguing about.

    Against an ``ENFORCES_KEY`` ledger this variant is suppressed and applies once: the provider
    does the work the client did not. Against the other two configurations it double-posts exactly
    as the first variant does, because a stable key is worth nothing where the provider does not
    enforce one and cannot be queried.

    So the baseline's failure is not an artefact of the key: it survives the most favourable
    reading of "no idempotency key" in two of the three configurations, and the one configuration
    where it does not is the one where the *ledger* — not the baseline — is doing the protecting.
    §12.3 says exactly that, and the suite asserts it as its own row rather than leaving it to a
    reviewer's imagination.
    """
    instruction = _instruction(residual, adjustment_id)

    for _ in range(retries):
        try:
            outcome = await adapter.post(adjustment_id.hex * 2, instruction)
        except Exception:
            continue

        if isinstance(outcome, Confirmed):
            await connection.execute(
                "UPDATE naive_adjustment SET posting_ref = $1 WHERE id = $2",
                outcome.posting_ref,
                adjustment_id,
            )
            await connection.execute(
                "UPDATE naive_residual SET state = 'posted' WHERE id = $1", residual.id
            )
            return outcome.posting_ref

        continue

    return None
