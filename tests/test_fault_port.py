"""The fault-injection port itself — every member of the vocabulary, exercised (increment 4.5).

`PROJECT_SPEC.md` §19 requires injection *"via explicit, testable seams (a fault-injection port),
not by patching internals"*. A port is only a seam if what it injects is checkable, so this module
drives **every member of :class:`~.faults.Fault`** and asserts two things per member: what the
client is told, and what happened to the books. §14's whole subject is that those two are
independent.

**Why every member and not only the three §19's scenarios use.** Three faults — a connection
refused before the first byte, a partial application, a throttle — are not injected by any scenario
in `tests/chaos/`, because §19's seven rows do not call for them and inventing an eighth row would
be this increment widening a specification on its own authority. But an enum member that nothing
ever constructs is untested code sitting in the flagship module, and the next increment to reach for
one would be the first to find out whether it works. They are exercised here instead, at the port,
which is the level they belong to.

The paths those three feed into are already proven where they live: 4.3's transport classifier
decides ``NOT_SENT`` from an exception type against an enumerated allowlist
(``tests/test_retry.py``), and 4.4 sends ``PartiallyApplied`` straight to an operator
(``tests/test_reconcile_postgres.py``). What is asserted here is only that the port can *produce*
each of them faithfully.

**No database.** The port sits at the ledger boundary and all three reference adapters are
in-process.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Final

import pytest

from ledger_exception_control_plane.ledger import (
    AMBIGUOUS_FAULTS,
    Confirmed,
    Fault,
    FaultInjectingLedger,
    Found,
    LedgerAdapterCapabilities,
    LedgerUnreachableError,
    NonIdempotentLedger,
    PartiallyApplied,
    PostingInstruction,
    PostingOutcome,
    QueryableNonIdempotentLedger,
    SimulatedLedger,
    Throttled,
    Unknown,
)
from ledger_exception_control_plane.ledger.port import (
    EndpointDeclaringAdapter,
    QueryableLedgerAdapter,
    declared_endpoint,
)
from ledger_exception_control_plane.ledger.transport import (
    TransportClass,
    classify_transport_failure,
)

OPERATION: Final = uuid.uuid5(uuid.NAMESPACE_OID, "fault-port").hex * 2
OTHER: Final = uuid.uuid5(uuid.NAMESPACE_OID, "fault-port-2").hex * 2

INSTRUCTION: Final = PostingInstruction(
    adjustment_id=uuid.UUID("55555555-5555-5555-8555-555555555555"),
    amount=decimal.Decimal("2799.9700"),
    currency="EUR",
    account_code="4100",
    period="2026-06",
)


# ======================================================================================
# Every fault, and the two independent things it decides
# ======================================================================================


@pytest.mark.asyncio
async def test_no_fault_is_a_transparent_pass_through() -> None:
    """The control arm of every scenario: the adapter behaves as itself and nothing is counted."""
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner)

    outcome = await adapter.post(OPERATION, INSTRUCTION)

    assert isinstance(outcome, Confirmed)
    assert adapter.injections == 0, "nothing was injected, and the counter must say so"
    assert inner.applied_count(OPERATION) == 1


@pytest.mark.asyncio
async def test_a_lost_response_commits_the_posting_and_tells_the_client_nothing() -> None:
    """**§19.1.** The books moved; the caller cannot know it. The ordering is the contract.

    Any other ordering makes §19.1 unreachable: a fault that short-circuits before delegating is an
    ambiguity with *nothing* applied, which is a different row of §19's table.
    """
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)

    outcome = await adapter.post(OPERATION, INSTRUCTION)

    assert isinstance(outcome, Unknown)
    assert inner.applied_count(OPERATION) == 1, "the ledger committed it"
    assert adapter.injections == 1


@pytest.mark.asyncio
async def test_an_ambiguous_5xx_applies_nothing_and_is_indistinguishable_from_the_above() -> None:
    """The other half of §14's point: same client-visible answer, opposite state of the books.

    The suite can tell them apart only because the *fault* says which is which. The caller cannot,
    by construction — which is why §13.5 forbids inferring one from the other.
    """
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner, fault=Fault.AMBIGUOUS_5XX)

    outcome = await adapter.post(OPERATION, INSTRUCTION)

    assert isinstance(outcome, Unknown)
    assert inner.applied_count(OPERATION) == 0, "nothing reached the books"
    assert inner.posts_received == 0, "and the inner ledger was never called"


def test_exactly_the_two_client_indistinguishable_faults_are_named_ambiguous() -> None:
    """``AMBIGUOUS_FAULTS`` is the set whose members a caller cannot tell apart.

    Asserted as an exact set rather than by membership: a third fault added to it would be a claim
    that some *other* failure is also unknowable, and a fault removed from it would be a claim the
    caller can now tell. Both are decisions, not edits.
    """
    assert set(AMBIGUOUS_FAULTS) == {Fault.COMMIT_THEN_LOSE_RESPONSE, Fault.AMBIGUOUS_5XX}


@pytest.mark.asyncio
async def test_an_unreachable_ledger_raises_what_the_classifier_can_allowlist() -> None:
    """§14's ``NOT_SENT`` class: the transport failed before the request left the client.

    The exception type matters and is the whole reason this class exists. 4.3's classifier decides
    ``NOT_SENT`` from an enumerated allowlist of exception types, so a bespoke error class would be
    classified ``UNKNOWN`` — correctly, since the classifier refuses to guess — and the scenario
    would then be testing the classifier's caution instead of the path it was written for.
    """
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner, fault=Fault.UNREACHABLE_BEFORE_FIRST_BYTE)

    with pytest.raises(LedgerUnreachableError) as raised:
        await adapter.post(OPERATION, INSTRUCTION)

    assert inner.applied_count(OPERATION) == 0
    assert inner.posts_received == 0, "nothing was sent, which is what NOT_SENT means"
    verdict = classify_transport_failure(raised.value)
    assert verdict.classification is TransportClass.NOT_SENT, (
        "the fault must produce an exception 4.3 already knows how to allowlist"
    )
    assert verdict.cause is not None, (
        "a NOT_SENT verdict carries why; an UNKNOWN one has no cause by construction"
    )


@pytest.mark.asyncio
async def test_a_partial_application_reports_the_legs_it_committed() -> None:
    """§14 sends this straight to manual recovery and never retries it.

    The payload is not decoration: the leg count and the references are what an operator reconciles
    against, and a variant that lost them would be an ambiguity with the evidence discarded.
    """
    adapter = FaultInjectingLedger(SimulatedLedger(), fault=Fault.PARTIALLY_APPLIED)

    outcome = await adapter.post(OPERATION, INSTRUCTION)

    assert isinstance(outcome, PartiallyApplied)
    assert outcome.applied_legs == 1
    assert outcome.posting_refs == ("CHAOS-LEG-1",)


@pytest.mark.asyncio
async def test_a_throttle_carries_a_delay_and_applied_nothing() -> None:
    """A scheduling signal, not a declination — and the delay is what 4.3 schedules on."""
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner, fault=Fault.THROTTLED)

    outcome = await adapter.post(OPERATION, INSTRUCTION)

    assert isinstance(outcome, Throttled)
    assert outcome.retry_after == dt.timedelta(seconds=30)
    assert inner.applied_count(OPERATION) == 0


def test_every_fault_in_the_enum_is_exercised_by_this_module() -> None:
    """**The reason this module exists, asserted rather than trusted.**

    An enum member nothing constructs is untested code in the flagship module. This fails when a
    fault is added without a test, which is the only moment the omission is cheap to fix.
    """
    exercised = {
        Fault.NONE,
        Fault.COMMIT_THEN_LOSE_RESPONSE,
        Fault.AMBIGUOUS_5XX,
        Fault.UNREACHABLE_BEFORE_FIRST_BYTE,
        Fault.PARTIALLY_APPLIED,
        Fault.THROTTLED,
    }
    assert exercised == set(Fault), f"unexercised fault(s): {sorted(set(Fault) - exercised)}"


# ======================================================================================
# The bound on firing, and what it exists to prevent
# ======================================================================================


@pytest.mark.asyncio
async def test_the_fault_fires_a_bounded_number_of_times_and_then_stops() -> None:
    """**A permanent fault would make most of §19 unreachable.**

    The failures §19 names are transient — a lost response, a 5xx, a connection refused — and each
    asks what the system does *afterwards*. A fault that fired on every attempt would mean nothing
    ever landed, and every branch would then look identically safe: an implementation that never
    posts never double-posts. The interesting arithmetic exists only when the first send is faulted
    and the second is not.
    """
    inner = NonIdempotentLedger()
    adapter = FaultInjectingLedger(inner, fault=Fault.AMBIGUOUS_5XX, fires=2)

    first = await adapter.post(OPERATION, INSTRUCTION)
    second = await adapter.post(OPERATION, INSTRUCTION)
    third = await adapter.post(OPERATION, INSTRUCTION)

    assert isinstance(first, Unknown) and isinstance(second, Unknown)
    assert isinstance(third, Confirmed), "the third send is past the bound and behaves normally"
    assert adapter.injections == 2
    assert inner.applied_count(OPERATION) == 1


# ======================================================================================
# It forwards, and it manufactures nothing
# ======================================================================================


@pytest.mark.asyncio
async def test_the_wrapper_forwards_the_query_and_never_answers_for_a_ledger_that_cannot() -> None:
    """The fault is at the *posting* boundary. A fault that also broke the query would be two
    faults in one row of §19's table.

    And the ``Indeterminate`` branch is a refusal, not an answer: a wrapper that could be queried
    while its inner ledger could not would be manufacturing the very capability §10.1 requires to
    be proven.
    """
    queryable = QueryableNonIdempotentLedger()
    await queryable.post(OPERATION, INSTRUCTION)
    through = FaultInjectingLedger(queryable, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)
    # Narrowed before the call, which is how every real caller reaches this method: the capability
    # is checked, then the query is made. A test that called it unconditionally would be exercising
    # a path the dispatcher cannot take.
    assert isinstance(through, QueryableLedgerAdapter)
    assert isinstance(await through.get_by_operation_id(OPERATION), Found)

    # **The negative direction, which is the one that matters and the one that was missing.**
    # The first version of this test asserted only the positive case, so it did not notice that the
    # wrapper satisfied the protocol around a ledger that cannot be queried at all — a reviewer
    # found the mismatch between the code and its own docstring. `hasattr` as well as `isinstance`,
    # because the protocol check resolves attributes statically and the two can disagree.
    opaque = FaultInjectingLedger(NonIdempotentLedger())
    assert not isinstance(opaque, QueryableLedgerAdapter), (
        "the wrapper must be invisible to the question, not merely unable to answer it"
    )
    assert not hasattr(opaque, "get_by_operation_id")


@pytest.mark.asyncio
async def test_the_wrapper_forwards_the_declared_endpoint_and_its_absence() -> None:
    """4.4 bounds a re-send by the endpoint the original send recorded.

    A wrapper that swallowed the declaration made every send through it record none, and the bound
    then refused a re-send the adapter's *verified* ``ENFORCES_KEY`` permits — the same defect
    ``AttributedAdapter`` had, in a new place. Both directions are asserted, because preserving the
    *absence* is the half an implementation gets wrong: Python resolves runtime protocol checks with
    ``inspect.getattr_static``, so a property raising ``AttributeError`` looks present anyway.
    """
    declaring = FaultInjectingLedger(SimulatedLedger(endpoint="sim://somewhere/postings"))
    assert declared_endpoint(declaring) == "sim://somewhere/postings"
    assert isinstance(declaring, EndpointDeclaringAdapter)

    class _DeclaresNoEndpoint:
        """Both reference adapters declare one, so the absent case needs a stand-in."""

        name = "silent"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(
            self, operation_id: str, instruction: PostingInstruction
        ) -> PostingOutcome:  # pragma: no cover - never called
            return Confirmed(posting_ref="unused")

    silent = FaultInjectingLedger(_DeclaresNoEndpoint())
    assert declared_endpoint(silent) is None
    assert not isinstance(silent, EndpointDeclaringAdapter)
    assert not hasattr(silent, "endpoint")


@pytest.mark.asyncio
async def test_the_counts_are_the_inner_ledgers_and_are_kept_apart() -> None:
    """Three numbers, three different questions, none of them the wrapper's opinion.

    ``applied_count`` is per identifier; ``total_applied`` is across them, which is what §19's
    *adjustments posted* column needs; ``posts_received`` counts requests that reached the ledger. A
    suite that only counted applications could not tell suppression from the request never
    arriving — the difference between the strong configuration and the weak one.
    """
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner)

    await adapter.post(OPERATION, INSTRUCTION)
    await adapter.post(OPERATION, INSTRUCTION)
    await adapter.post(OTHER, INSTRUCTION)

    assert adapter.applied_count(OPERATION) == 1, "suppressed at the books"
    assert adapter.posts_received == 3, "and received three times"
    assert adapter.total_applied == 2, "two identifiers, one application each"

    assert adapter.applied_count(OPERATION) == inner.applied_count(OPERATION)
    assert adapter.total_applied == inner.total_applied
    assert adapter.posts_received == inner.posts_received


@pytest.mark.asyncio
async def test_a_delegated_post_carries_the_identifier_it_was_given() -> None:
    """**The property that makes the conformance unwrap sound, asserted at the wrapper.**

    ``implementation_of`` unwraps this class so the inner ledger's conformance record applies, and
    that is only defensible because a fault changes what the client is *told* and never the
    identifier a delegated post carries. A wrapper that re-keyed the request would be manufacturing
    a duplicate the record says cannot happen.
    """
    inner = SimulatedLedger()
    adapter = FaultInjectingLedger(inner, fault=Fault.COMMIT_THEN_LOSE_RESPONSE)

    await adapter.post(OPERATION, INSTRUCTION)

    assert inner.applied_count(OPERATION) == 1
    assert inner.total_applied == 1, "one posting, under the identifier the caller supplied"
    assert inner.applied_count(OTHER) == 0
