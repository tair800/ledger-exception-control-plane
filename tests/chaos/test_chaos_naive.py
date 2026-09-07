"""§19's scenarios against `naive/` — the RED half of the gate (increment 4.5).

`PROJECT_SPEC.md` §19: *"`naive/` — a deliberately unsafe baseline … **It must double-post.**"* and
*"A suite that passes on both branches proves nothing and is a defect."*

**Every test in this module asserts a failure.** That is not a stylistic inversion — it is the gate.
If these tests started passing in the sense of "the baseline behaved well", the chaos suite would
have stopped being able to tell a safe implementation from an unsafe one, and the flagship claim
would rest on nothing.

**The same world as `main`.** Same PostgreSQL, same three capability configurations, same
fault-injection port, same amount, same instant. The only difference between this module and
``test_chaos_main.py`` is the code being driven. Anything else and the comparison would be
measuring the harness.

**Counted at the ledger, across identifiers.** §19's column is *adjustments posted* — financial
effects — and every one of the baseline's five duplicates posts twice under two *different*
identifiers: two
residuals from one payload, two approvals from one replayed token. A per-identifier count would
record each of those as "applied once" while the money moved twice, which is precisely the kind of
number that makes a green suite meaningless.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from naive.pipeline import (
    NaiveResidual,
    approve,
    claim_open_residuals,
    dispatch,
    dispatch_with_a_stable_key,
    record_adjustment,
)

from ledger_exception_control_plane.ledger import Fault, FaultInjectingLedger
from tests.chaos.conftest import DSN, naive_work, one_day_later
from tests.chaos.results import observe
from tests.chaos.scenarios import (
    CONFIGURATIONS,
    Capability,
    Scenario,
    adapter_for,
    expectation,
)

pytestmark = pytest.mark.integration


def _expect(scenario: Scenario) -> int:
    return expectation(scenario=scenario, capability=Capability.NONE, branch="naive").applied


async def _one_claimed(
    connection: asyncpg.Connection, *, marker: str
) -> tuple[NaiveResidual, uuid.UUID]:
    """One piece of baseline work, claimed and approved. Returns ``(residual, approval_id)``."""
    await naive_work(connection, marker=marker)
    (residual,) = await claim_open_residuals(connection, worker="worker-1")
    approval_id = await approve(
        connection,
        residual_id=residual.id,
        principal="controller-a",
        token=uuid.uuid4().hex,
        decided_at=one_day_later(),
    )
    return residual, approval_id


# ======================================================================================
# §19.1 — the required scenario, and the one the gate turns on
# ======================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_the_baseline_double_posts_when_a_response_is_lost(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """**The RED result the flagship claim depends on.**

    The identical fault `main` survives in ``test_chaos_main.py``: the ledger commits the posting
    and the response is lost. The baseline reads the ambiguity as a failure — because ``Unknown`` is
    not a shape it knows — and retries. The retry lands, and the same money is now on the books
    twice.

    **It fails in all three configurations, including under an enforcing ledger**, and that is
    §12.3 read backwards: sending an identifier is a *request* for idempotent treatment, and the
    baseline sends a different one each attempt. A provider that would have suppressed the duplicate
    is never given the chance to.
    """
    inner = adapter_for(capability)
    adapter = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    residual, approval_id = await _one_claimed(connection, marker=f"lost{capability.value}")

    adjustment_id = await record_adjustment(connection, residual=residual, approval_id=approval_id)
    await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)

    assert adapter.injections == 1, "the same fault `main` was given"
    observe(
        scenario=Scenario.LOST_RESPONSE_AFTER_COMMIT,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.LOST_RESPONSE_AFTER_COMMIT), (
        "the baseline must double-post here; if it does not, the suite is theatre"
    )
    assert adapter.total_applied == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_stable_key_does_not_save_the_baseline_where_the_ledger_does_not_enforce_one(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """**The obvious objection to the row above, measured instead of argued.**

    The objection: the duplicate is an artefact of the baseline sending a fresh key per attempt, and
    a luckier naive implementation passing a stable row id would not double-post. It is half right,
    and the half it gets right is worth pinning down.

    Against ``ENFORCES_KEY`` this variant **is** suppressed and applies once — the provider doing
    the work the client did not, which is exactly what §13.5 means by a *conditional* guarantee.
    Against the other two it double-posts identically, because a stable key buys nothing where the
    provider neither enforces one nor can be asked.

    So the baseline's failure survives the most favourable reading of "no idempotency key" in two
    of the three configurations, and in the third the protection is the ledger's rather than its
    own.
    """
    inner = adapter_for(capability)
    adapter = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    residual, approval_id = await _one_claimed(connection, marker=f"stable{capability.value}")

    adjustment_id = await record_adjustment(connection, residual=residual, approval_id=approval_id)
    await dispatch_with_a_stable_key(
        connection, adapter, residual=residual, adjustment_id=adjustment_id
    )

    if capability is Capability.ENFORCES_KEY:
        assert adapter.total_applied == 1, (
            "suppressed by the ledger, not by the baseline — and only because the key happened "
            "to be stable"
        )
    else:
        assert adapter.total_applied == 2, (
            "a stable key is worth nothing where the provider does not enforce one"
        )


# ======================================================================================
# The other six scenarios
# ======================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_the_baseline_reposts_after_a_crash_before_it_recorded_the_posting(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """No transaction spans the send and the record, so a crash between them loses the effect.

    The crash is modelled as what it is: the posting reference never reaching the row. The baseline
    has no write-ahead record and nothing that says "a send happened", so the re-run finds work that
    looks untouched and sends again.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    residual, approval_id = await _one_claimed(connection, marker=f"crash{capability.value}")
    adjustment_id = await record_adjustment(connection, residual=residual, approval_id=approval_id)

    await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)
    # The crash: the process dies before the reference and the state reach the database.
    await connection.execute(
        "UPDATE naive_adjustment SET posting_ref = NULL WHERE id = $1", adjustment_id
    )
    await connection.execute(
        "UPDATE naive_residual SET state = 'claimed' WHERE id = $1", residual.id
    )

    # The re-run. Nothing on the row says a send ever happened.
    await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)

    observe(
        scenario=Scenario.CRASH_BEFORE_COMMIT,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.CRASH_BEFORE_COMMIT)
    assert adapter.total_applied == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_redelivered_payload_becomes_a_second_unit_of_work(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """No content-hash uniqueness, so one delivery arriving twice is two pieces of work.

    Both are posted, under two different identifiers, and each is applied once. The money has moved
    twice — which is why the count that matters is financial effects rather than identifiers.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))

    await naive_work(connection, marker=f"dup{capability.value}")
    await naive_work(connection, marker=f"dup{capability.value}")

    assert len(await connection.fetch("SELECT id FROM naive_batch")) == 2, (
        "the identical payload was stored twice"
    )

    claimed = await claim_open_residuals(connection, worker="worker-1")
    assert len(claimed) == 2

    for residual in claimed:
        approval_id = await approve(
            connection,
            residual_id=residual.id,
            principal="controller-a",
            token=uuid.uuid4().hex,
            decided_at=one_day_later(),
        )
        adjustment_id = await record_adjustment(
            connection, residual=residual, approval_id=approval_id
        )
        await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)

    observe(
        scenario=Scenario.DUPLICATE_WEBHOOK,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.DUPLICATE_WEBHOOK)
    assert adapter.total_applied == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_worker_killed_mid_batch_strands_the_baselines_work(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """**A different failure, recorded as the different number it is.**

    The baseline claims the whole batch up front and commits the claim, so a killed worker leaves
    the work marked ``claimed`` with nobody working it. It is not re-claimable: the next worker's
    read looks for ``state = 'open'`` and finds nothing.

    That is a *lost* financial effect rather than a duplicated one, and writing this row as a
    double-post would have been the suite deciding what it wanted to see. `main`'s claim is
    transaction-scoped and comes back by itself.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    await naive_work(connection, marker=f"killed{capability.value}")

    claimed = await claim_open_residuals(connection, worker="worker-1")
    assert len(claimed) == 1, "the claim is committed, not held in a transaction"

    # The worker dies here, having claimed and posted nothing.
    again = await claim_open_residuals(connection, worker="worker-2")
    assert again == [], "the work is stranded: claimed, unposted, and invisible to the next worker"

    observe(
        scenario=Scenario.WORKER_KILLED_MID_BATCH,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.WORKER_KILLED_MID_BATCH)
    assert adapter.total_applied == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_two_baseline_workers_both_claim_and_both_post(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """Read, then update, with no lock — so two workers get the same row and both post it.

    Forced with the same handshake ``test_chaos_main.py`` uses, on two genuine connections, so the
    overlap is real rather than hoped for. What differs is only the claim: `main` takes ``SELECT …
    FOR UPDATE SKIP LOCKED`` in one statement and the second worker gets nothing.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    await naive_work(connection, marker=f"race{capability.value}")

    first_has_read = asyncio.Event()
    second_has_read = asyncio.Event()

    async def worker(name: str, *, first: bool) -> list[asyncpg.Record]:
        own = await asyncpg.connect(DSN)
        try:
            rows = await own.fetch(
                "SELECT id, reference, amount, currency, account_code, period"
                " FROM naive_residual WHERE state = 'open'"
            )
            if first:
                first_has_read.set()
                await asyncio.wait_for(second_has_read.wait(), timeout=20)
            else:
                await asyncio.wait_for(first_has_read.wait(), timeout=20)
                second_has_read.set()
            if rows:
                await own.execute(
                    "UPDATE naive_residual SET state = 'claimed', claimed_by = $1 WHERE id = $2",
                    name,
                    rows[0]["id"],
                )
            return list(rows)
        finally:
            await own.close()

    one, two = await asyncio.gather(worker("worker-1", first=True), worker("worker-2", first=False))

    assert len(one) == 1 and len(two) == 1, (
        "both workers read the same open row: nothing prevented it"
    )

    for row in (one[0], two[0]):
        residual = NaiveResidual(
            id=row["id"],
            reference=row["reference"],
            amount=row["amount"],
            currency=row["currency"],
            account_code=row["account_code"],
            period=row["period"],
        )
        approval_id = await approve(
            connection,
            residual_id=residual.id,
            principal="controller-a",
            token=uuid.uuid4().hex,
            decided_at=one_day_later(),
        )
        adjustment_id = await record_adjustment(
            connection, residual=residual, approval_id=approval_id
        )
        await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)

    observe(
        scenario=Scenario.TWO_WORKERS_ONE_RESIDUAL,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.TWO_WORKERS_ONE_RESIDUAL)
    assert adapter.total_applied == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_a_replayed_token_authorises_a_second_baseline_posting(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """The token is stored because it arrived, not because anything checked it was unused.

    So the replay records a second approval, and a second approval authorises a second posting of
    the same work. `main` loses the replay to ``uq_approval_token`` — a unique index rather than an
    application check a refactor could skip.
    """
    adapter = FaultInjectingLedger(adapter_for(capability))
    await naive_work(connection, marker=f"token{capability.value}")
    (residual,) = await claim_open_residuals(connection, worker="worker-1")
    token = uuid.uuid4().hex

    for _ in range(2):
        approval_id = await approve(
            connection,
            residual_id=residual.id,
            principal="controller-a",
            token=token,
            decided_at=one_day_later(),
        )
        adjustment_id = await record_adjustment(
            connection, residual=residual, approval_id=approval_id
        )
        await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)

    approvals = await connection.fetch("SELECT id FROM naive_approval WHERE token = $1", token)
    assert len(approvals) == 2, "one token, two approvals"

    observe(
        scenario=Scenario.REPLAYED_APPROVAL_TOKEN,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.REPLAYED_APPROVAL_TOKEN)
    assert adapter.total_applied == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", CONFIGURATIONS)
async def test_an_ambiguous_5xx_leaves_the_baseline_correct_by_luck(
    connection: asyncpg.Connection, capability: Capability
) -> None:
    """**The row where the baseline does not fail, recorded honestly.**

    An ambiguous 5xx applied nothing, so the baseline's retry completes the work exactly once. It is
    right, and it is right for no reason it can take credit for: the identical inference — *"not a
    confirmation, so try again"* — is the one that double-posts in §19.1. The difference is entirely
    in what the ledger happened to have done, which the baseline cannot see.

    Writing this row as a duplicate would have been the suite deciding what it wanted to see, and a
    RED baseline that fails every row is as uninformative as one that fails none.
    """
    inner = adapter_for(capability)
    adapter = FaultInjectingLedger(inner, fault=Fault.AMBIGUOUS_5XX)
    residual, approval_id = await _one_claimed(connection, marker=f"fivexx{capability.value}")

    adjustment_id = await record_adjustment(connection, residual=residual, approval_id=approval_id)
    await dispatch(connection, adapter, residual=residual, adjustment_id=adjustment_id)

    assert adapter.injections == 1
    observe(
        scenario=Scenario.AMBIGUOUS_5XX,
        capability=capability,
        branch="naive",
        applied=adapter.total_applied,
    )
    assert adapter.total_applied == _expect(Scenario.AMBIGUOUS_5XX)
    assert adapter.total_applied == 1
