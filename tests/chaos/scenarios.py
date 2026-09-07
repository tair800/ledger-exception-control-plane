"""The §19 scenario vocabulary and the three capability configurations (increment 4.5).

Defined once, in one place, and pointed at both branches. §19 requires *"each scenario … against
three adapter configurations"* and a results table of *"scenario, branch, adjustments posted,
expected, observed"*, and a table is only checkable if the rows are values rather than prose.

**The expectation is declared per scenario and per configuration, before the run.** That ordering
is the point of the gate: the exit criterion is *"`naive/` double-posts, `main` does not"*, and a
suite that decided what to expect after seeing the result would be unfalsifiable. Every expectation
below is written down here; the runners read them and assert against them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
from typing import Final

from ledger_exception_control_plane.ledger import (
    Fault,
    IdempotencyMode,
    LedgerAdapterCapabilities,
    Linearizable,
    NonIdempotentLedger,
    PostingQueryMode,
    QueryableNonIdempotentLedger,
    SimulatedLedger,
)
from ledger_exception_control_plane.ledger.port import LedgerAdapter

__all__ = [
    "AMOUNT",
    "CONFIGURATIONS",
    "CONFIGURATION_LABELS",
    "EPOCH",
    "INFLIGHT",
    "NAIVE_DOUBLE_POSTS",
    "SCENARIOS",
    "SCENARIO_FAULTS",
    "SCENARIO_TITLES",
    "Capability",
    "Expectation",
    "Scenario",
    "adapter_for",
    "expectation",
]

#: The instant every scenario is driven at. Fixed, because a chaos suite whose ordering depends on
#: wall-clock resolution fails on a fast machine for a reason that has nothing to do with chaos.
EPOCH: Final = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

#: The amount every scenario posts. One value, so a duplicate is visible as a count rather than as
#: arithmetic — §19's table counts *adjustments posted*, not money.
AMOUNT: Final = decimal.Decimal("2799.97")

#: Long enough that no scenario accidentally satisfies §13.5's windows.
INFLIGHT: Final = dt.timedelta(seconds=30)


class Capability(enum.StrEnum):
    """§19's three adapter configurations, named exactly as the specification names them."""

    #: Enforces the operation key. Deliberately **not** queryable: `main` prefers a query wherever
    #: one is available, so a configuration that offered both would exercise the query branch and
    #: leave the bounded re-send — the branch this configuration exists to test — unreached.
    ENFORCES_KEY = "enforces_key"

    #: Queryable by operation identifier and enforcing nothing. Backed by an adapter that genuinely
    #: double-books, so the label is not stronger than the behaviour.
    BY_OPERATION_ID = "by_operation_id"

    #: Neither. §13.5's *"otherwise route to manual recovery … the automatic path stops"*.
    NONE = "none"


class Scenario(enum.StrEnum):
    """The seven failures §19 names, verbatim in meaning if not in wording."""

    CRASH_BEFORE_COMMIT = "crash_before_commit"
    DUPLICATE_WEBHOOK = "duplicate_webhook"
    WORKER_KILLED_MID_BATCH = "worker_killed_mid_batch"
    TWO_WORKERS_ONE_RESIDUAL = "two_workers_one_residual"
    REPLAYED_APPROVAL_TOKEN = "replayed_approval_token"
    LOST_RESPONSE_AFTER_COMMIT = "lost_response_after_commit"
    AMBIGUOUS_5XX = "ambiguous_5xx"


#: The English titles the results table renders, so the table and the code cannot drift.
SCENARIO_TITLES: Final[dict[Scenario, str]] = {
    Scenario.CRASH_BEFORE_COMMIT: "Crash before commit",
    Scenario.DUPLICATE_WEBHOOK: "Duplicate webhook delivery",
    Scenario.WORKER_KILLED_MID_BATCH: "Worker killed mid-batch",
    Scenario.TWO_WORKERS_ONE_RESIDUAL: "Two workers claim one residual",
    Scenario.REPLAYED_APPROVAL_TOKEN: "Replay of a consumed approval token",
    Scenario.LOST_RESPONSE_AFTER_COMMIT: "Lost response after a committed ledger write (§19.1)",
    Scenario.AMBIGUOUS_5XX: "Ledger returns an ambiguous 5xx",
}

#: Which fault each scenario injects at the ledger boundary.
#:
#: Five scenarios inject nothing there — they are faults of *process and delivery*, not of the
#: ledger — and ``Fault.NONE`` says so rather than leaving a reader to infer it. Injecting a ledger
#: fault into, say, the duplicate-webhook scenario would confound two failures in one row.
SCENARIO_FAULTS: Final[dict[Scenario, Fault]] = {
    Scenario.CRASH_BEFORE_COMMIT: Fault.NONE,
    Scenario.DUPLICATE_WEBHOOK: Fault.NONE,
    Scenario.WORKER_KILLED_MID_BATCH: Fault.NONE,
    Scenario.TWO_WORKERS_ONE_RESIDUAL: Fault.NONE,
    Scenario.REPLAYED_APPROVAL_TOKEN: Fault.NONE,
    Scenario.LOST_RESPONSE_AFTER_COMMIT: Fault.COMMIT_THEN_LOSE_RESPONSE,
    Scenario.AMBIGUOUS_5XX: Fault.AMBIGUOUS_5XX,
}

SCENARIOS: Final[tuple[Scenario, ...]] = tuple(Scenario)
CONFIGURATIONS: Final[tuple[Capability, ...]] = tuple(Capability)


CONFIGURATION_LABELS: Final[dict[Capability, str]] = {
    Capability.ENFORCES_KEY: "`ENFORCES_KEY`",
    Capability.BY_OPERATION_ID: "`BY_OPERATION_ID` only",
    Capability.NONE: "`NONE`/`NONE`",
}


def adapter_for(capability: Capability) -> LedgerAdapter:
    """A fresh adapter in one of §19's three configurations.

    Fresh per call, deliberately: the applied-count is the measurement every §19.1 assertion reads,
    and a shared ledger would carry one scenario's postings into the next.
    """
    if capability is Capability.ENFORCES_KEY:
        return SimulatedLedger(
            capabilities=LedgerAdapterCapabilities(
                idempotency=IdempotencyMode.ENFORCES_KEY,
                idempotency_window=dt.timedelta(days=1),
                posting_identity_query=PostingQueryMode.NONE,
                query_consistency=Linearizable(),
                max_inflight_window=INFLIGHT,
            )
        )
    if capability is Capability.BY_OPERATION_ID:
        return QueryableNonIdempotentLedger()
    if capability is Capability.NONE:
        return NonIdempotentLedger()
    raise ValueError(f"{capability!r} is not one of §19's configurations")  # pragma: no cover


@dataclasses.dataclass(frozen=True, slots=True)
class Expectation:
    """What a branch must do in one scenario under one configuration.

    ``applied`` is the number §19's table calls *adjustments posted*: financial effects committed at
    the ledger for one economic unit of work, counted across identifiers. Counting per identifier
    would miss every one of the baseline's five duplicates — two residuals from one payload, or
    two approvals from
    one replayed token, post twice under two *different* identifiers, and each is applied once while
    the money has moved twice.
    """

    applied: int
    note: str


#: **Every expectation, declared before any run.** Read by the runners, decided by neither.
#:
#: `main` applies **at most once, everywhere**, and that is the exit criterion rather than an
#: optimistic guess. Where it applies *zero* times the work did not complete automatically, and that
#: is the correct outcome rather than a shortfall: under a capability that can neither suppress a
#: duplicate nor answer a query, §13.5 requires the automatic path to stop and a human to decide.
#: A row of `1` under `NONE`/`NONE` for an ambiguity would mean the system had guessed.
_MAIN: Final[dict[tuple[Scenario, Capability], Expectation]] = {}
for _scenario in Scenario:
    for _capability in Capability:
        if _scenario is Scenario.AMBIGUOUS_5XX and _capability is not Capability.ENFORCES_KEY:
            _MAIN[(_scenario, _capability)] = Expectation(
                applied=0,
                note="nothing was applied and nothing may be assumed; routed to a human",
            )
        else:
            _MAIN[(_scenario, _capability)] = Expectation(
                applied=1, note="exactly one financial effect"
            )

#: `naive/`, and the shape of each failure is not the same shape.
#:
#: Five scenarios produce a **duplicate** financial effect. One produces a **lost** one — the
#: baseline claims a whole batch up front, so work in a killed worker's batch is stranded rather
#: than re-claimable, which is a different defect and is recorded as the different number it is. One
#: produces neither: an ambiguous 5xx applied nothing, so the baseline's retry lands the work once
#: and is *correct by luck*. Writing that row as a duplicate would have been the suite deciding what
#: it wanted to see.
_NAIVE: Final[dict[Scenario, Expectation]] = {
    Scenario.CRASH_BEFORE_COMMIT: Expectation(
        applied=2,
        note="posted, then crashed before recording it; the re-run posts again",
    ),
    Scenario.DUPLICATE_WEBHOOK: Expectation(
        applied=2, note="no content-hash uniqueness: one payload, two units of work"
    ),
    Scenario.WORKER_KILLED_MID_BATCH: Expectation(
        applied=0, note="the batch was claimed up front, so the work is stranded, not re-claimable"
    ),
    Scenario.TWO_WORKERS_ONE_RESIDUAL: Expectation(
        applied=2, note="no claim lock: both workers took the same residual and both posted"
    ),
    Scenario.REPLAYED_APPROVAL_TOKEN: Expectation(
        applied=2, note="the token is not single-use, so the replay authorised a second posting"
    ),
    Scenario.LOST_RESPONSE_AFTER_COMMIT: Expectation(
        applied=2,
        note="the ambiguity was read as a failure and retried under a fresh key",
    ),
    Scenario.AMBIGUOUS_5XX: Expectation(
        applied=1,
        note="nothing had been applied, so the retry completed the work — correct by luck",
    ),
}

#: The scenarios in which the baseline commits the **same financial effect twice**.
#:
#: This set is the gate. §19: *"It must double-post."* If it were empty the suite would be theatre,
#: and a test asserts it is not — separately from asserting each row, so that a future edit that
#: quietly turned every naive expectation into `1` would fail on the claim rather than pass on the
#: rows.
NAIVE_DOUBLE_POSTS: Final[frozenset[Scenario]] = frozenset(
    scenario for scenario, expected in _NAIVE.items() if expected.applied > 1
)


def expectation(*, scenario: Scenario, capability: Capability, branch: str) -> Expectation:
    """What this branch must do here. Reads the tables above; decides nothing itself."""
    if branch == "main":
        return _MAIN[(scenario, capability)]
    if branch == "naive":
        return _NAIVE[scenario]
    raise ValueError(f"{branch!r} is not a branch of this comparison")  # pragma: no cover
