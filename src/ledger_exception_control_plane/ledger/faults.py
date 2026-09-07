"""The fault-injection port — §19's *"explicit, testable seams"* (increment 4.5).

`PROJECT_SPEC.md` §19 ends with one sentence that decides this module's shape:

    Injection is via explicit, testable seams (a fault-injection port), not by patching internals.

**Why a port rather than a callable per test.** The adapters already accept a ``responder``, which
is enough to make one thing go wrong once. What §19 asks for is different: seven named scenarios,
each run against three capability configurations, with a results table an auditor reads. That needs
the *fault* to be a value — declared, named, countable, and assertable — rather than a lambda whose
behaviour has to be re-read to know what it injects.

So a fault is a member of a closed enum, and :class:`FaultInjectingLedger` is the seam that applies
it. A test declares "inject ``COMMIT_THEN_LOSE_RESPONSE`` at the ledger" and the suite's table can
say so; the alternative is a table whose rows nobody can check against the code.

**The critical fault, and why it cannot be expressed any other way.** §19.1 requires the ledger to
**commit and then fail** — the posting is on the books and the client never learns it. None of the
three reference adapters can do that on its own:

- :class:`~.simulated.SimulatedLedger` consults its responder *before* applying, deliberately, so an
  injected ``Unknown`` there means nothing reached the books.
- :class:`~.simulated.NonIdempotentLedger` consults its responder *after* applying, so it can lose a
  response — but it declares ``NONE``/``NONE`` and cannot exercise the two strong configurations.
- :class:`~.simulated.QueryableNonIdempotentLedger` is in the same position for the same reason: it
  applies before consulting its responder, and declares only the middle configuration.

This wrapper applies the fault around whichever adapter it is given, so the same scenario runs
against all three configurations from one definition. That is the whole reason §19 wants a port: a
scenario expressed once and a capability varied around it, rather than three hand-written variants
that can quietly diverge.

**It forges nothing.** The wrapper forwards ``name``, ``capabilities``, ``endpoint`` and the query
method from the adapter it holds, and :func:`~.conformance.implementation_of` unwraps it on an exact
type check — the same arrangement :class:`~.transport.AttributedAdapter` uses and for the same
reason. A subclass that stopped delegating could not inherit the inner adapter's conformance record.
The applied-count every §19.1 assertion reads comes from the *inner* ledger, so what is measured is
the books, never the wrapper's opinion of them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
from typing import Final

from ledger_exception_control_plane.ledger.port import (
    LedgerAdapter,
    LedgerAdapterCapabilities,
    PartiallyApplied,
    PostingInstruction,
    PostingOutcome,
    QueryableLedgerAdapter,
    QueryOutcome,
    Throttled,
    Unknown,
    declared_endpoint,
)

__all__ = [
    "AMBIGUOUS_FAULTS",
    "Fault",
    "FaultInjectingLedger",
    "LedgerUnreachableError",
]


class Fault(enum.StrEnum):
    """What can be made to go wrong at the ledger boundary. Closed, and each member is a §19 row.

    Named for **what happens to the books**, not for what the client sees, because that distinction
    is the entire subject of §13.5 and a vocabulary that blurred it would make the suite unable to
    describe its own scenarios.
    """

    #: Nothing. The adapter behaves as itself — the control arm of every scenario.
    NONE = "none"

    #: **§19.1.** The ledger commits the posting and the response never arrives. The books moved;
    #: the client cannot know it. This is the fault the whole reliability layer exists for.
    COMMIT_THEN_LOSE_RESPONSE = "commit_then_lose_response"

    #: An ambiguous 5xx *after* the request was sent. §14: never classified as "not applied".
    #: Distinguished from the above by what happened to the books: here, nothing did.
    AMBIGUOUS_5XX = "ambiguous_5xx"

    #: The transport failed before the request left the client — §14's allowlisted `NOT_SENT`
    #: class. Nothing was sent and nothing applied, and 4.3's classifier is what says so.
    UNREACHABLE_BEFORE_FIRST_BYTE = "unreachable_before_first_byte"

    #: Some legs committed and some did not. Possible only under `NON_ATOMIC`; §14 sends it
    #: straight to manual recovery and never retries it.
    PARTIALLY_APPLIED = "partially_applied"

    #: The provider turned the request away before considering it. A scheduling signal, not a
    #: declination — and nothing was applied.
    THROTTLED = "throttled"


#: The faults whose outcome leaves the books in an undetermined state *from the client's view*.
#:
#: ``COMMIT_THEN_LOSE_RESPONSE`` and ``AMBIGUOUS_5XX`` are the two, and they are
#: indistinguishable to the caller by construction — which is exactly §14's point:
#: *"indistinguishable from 'never applied' at the transport layer"*. The suite asserts the same
#: client behaviour for both and a different ledger applied-count, which is only possible because
#: the fault says which is which and the caller cannot.
AMBIGUOUS_FAULTS: Final[frozenset[Fault]] = frozenset(
    {Fault.COMMIT_THEN_LOSE_RESPONSE, Fault.AMBIGUOUS_5XX}
)


class LedgerUnreachableError(ConnectionRefusedError):
    """The transport failed before the request left the client.

    A ``ConnectionRefusedError`` subclass rather than a new hierarchy, because 4.3's transport
    classifier decides ``NOT_SENT`` from the *exception type* against an enumerated allowlist. A
    bespoke class would be classified ``UNKNOWN`` — correctly, since the classifier refuses to
    guess — and the scenario would then be testing the classifier's caution rather than the
    ``NOT_SENT`` path it is written for.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class InjectedResponse:
    """What the wrapper answers with, and how many legs it lets through.

    Two fields because §19.1 turns on their independence: ``applied`` says what happened to the
    books and ``outcome`` says what the client is told, and the scenario that matters has the first
    true and the second unknowable.
    """

    applied: bool
    outcome: PostingOutcome | None


class FaultInjectingLedger:
    """An adapter that applies one declared fault and otherwise delegates.

    Structural, like every adapter here: it names no protocol and the type checker establishes the
    relationship. It satisfies :class:`~.port.QueryableLedgerAdapter` only when the adapter it wraps
    does — see :meth:`_query_inner`, which records how it once failed to — because a wrapper that
    could be queried while its inner ledger could not would be manufacturing the very capability
    §10.1 requires to be proven.
    """

    def __init__(
        self, adapter: LedgerAdapter, *, fault: Fault = Fault.NONE, fires: int = 1
    ) -> None:
        self.wrapped = adapter
        self.fault = fault

        #: How many of the earliest sends the fault applies to. One by default.
        #:
        #: **Bounded, because a permanent fault would make most of §19 unreachable.** The failures
        #: §19 names are transient — a lost response, a 5xx, a connection refused — and the question
        #: each asks is what the system does *afterwards*. A fault that fired on every attempt would
        #: mean nothing ever landed, and every branch would then look identically safe: an
        #: implementation that never posts never double-posts. The interesting arithmetic only
        #: exists when the first send is faulted and the second is not.
        self.fires = fires

        #: How many times a fault was injected. Read by the suite to prove the injection *happened*
        #: — a scenario whose fault silently failed to fire would pass for the worst possible
        #: reason, and one of the mutation tests plants exactly that.
        self.injections = 0

        # Forwarded the way `AttributedAdapter` forwards it, and for the reason recorded there:
        # 4.4 bounds a re-send by the endpoint the original send recorded, and a wrapper that
        # swallowed the declaration made every send through it record none. Set only when there is
        # one, so the wrapper stays invisible to `isinstance(..., EndpointDeclaringAdapter)`.
        endpoint = declared_endpoint(adapter)
        if endpoint is not None:
            setattr(self, "endpoint", endpoint)  # noqa: B010

        # Bound only around a queryable adapter, so `isinstance(..., QueryableLedgerAdapter)` is
        # answered by the inner ledger's real capability rather than by this wrapper's existence.
        # See `_query_inner`, which records the defect this replaced.
        if isinstance(adapter, QueryableLedgerAdapter):
            setattr(self, "get_by_operation_id", self._query_inner)  # noqa: B010

    @property
    def name(self) -> str:
        return self.wrapped.name

    def capabilities(self) -> LedgerAdapterCapabilities:
        """The inner adapter's declaration, untouched.

        A fault is something that happens to a request, never a change to what the provider
        contractually offers. A wrapper that altered the capability record would make every
        capability-branch assertion in the suite a statement about the wrapper.
        """
        return self.wrapped.capabilities()

    def _decide(self) -> InjectedResponse:
        """What this fault does to the books, and what the client is told."""
        match self.fault:
            case Fault.NONE:
                return InjectedResponse(applied=True, outcome=None)
            case Fault.COMMIT_THEN_LOSE_RESPONSE:
                return InjectedResponse(
                    applied=True,
                    outcome=Unknown(detail="connection reset before the response was read"),
                )
            case Fault.AMBIGUOUS_5XX:
                return InjectedResponse(
                    applied=False, outcome=Unknown(detail="502 from the ledger gateway")
                )
            case Fault.PARTIALLY_APPLIED:
                return InjectedResponse(
                    applied=False,
                    outcome=PartiallyApplied(applied_legs=1, posting_refs=("CHAOS-LEG-1",)),
                )
            case Fault.THROTTLED:
                return InjectedResponse(
                    applied=False, outcome=Throttled(retry_after=dt.timedelta(seconds=30))
                )
            case Fault.UNREACHABLE_BEFORE_FIRST_BYTE:
                return InjectedResponse(applied=False, outcome=None)
            case _:  # pragma: no cover - the enum is closed and mypy proves the branches
                raise ValueError(f"{self.fault!r} has no injection defined")

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        """Apply the fault, then delegate or answer.

        **The ordering is the contract.** For ``COMMIT_THEN_LOSE_RESPONSE`` the inner ledger's
        ``post`` is awaited *first* and its answer then discarded in favour of an ``Unknown``: the
        books move and the client is told nothing. Any other ordering would make §19.1 unreachable,
        because a fault that short-circuits before delegating is an ambiguity with nothing applied —
        which is ``AMBIGUOUS_5XX``, a different row of the table.
        """
        if self.fault is Fault.NONE or self.injections >= self.fires:
            return await self.wrapped.post(operation_id, instruction)

        self.injections += 1
        decision = self._decide()

        if self.fault is Fault.UNREACHABLE_BEFORE_FIRST_BYTE:
            raise LedgerUnreachableError(111, "connection refused")

        if decision.applied:
            # Delegated for real, so the applied-count the suite reads is the inner ledger's own
            # record of a posting it committed — not a number this class maintains.
            await self.wrapped.post(operation_id, instruction)

        assert decision.outcome is not None, "every remaining fault answers with an outcome"
        return decision.outcome

    async def _query_inner(self, operation_id: str) -> QueryOutcome:
        """Delegate the query. Bound as ``get_by_operation_id`` only around a queryable adapter.

        The fault is at the *posting* boundary; a fault that also broke the query would be two
        faults in one row of §19's table.

        **Not a plain method, and the reason is a defect this file used to have.** The first version
        defined ``get_by_operation_id`` unconditionally and answered ``Indeterminate`` when the
        inner ledger could not be asked — while its own docstring claimed the wrapper *"satisfies
        QueryableLedgerAdapter only when the adapter it wraps does"*. That claim was false:
        :class:`~.port.QueryableLedgerAdapter` is a runtime-checkable protocol, so the method merely
        existing made ``isinstance`` answer ``True`` around a ledger that cannot be queried at all.
        A wrapper that manufactures the capability §10.1 requires to be *proven* is the exact
        forgery the conformance keying exists to prevent, arriving through the type system instead.

        Binding it conditionally in ``__init__`` makes the wrapper genuinely invisible to the
        question, which is the same arrangement — and the same ``getattr_static`` reasoning — that
        :class:`~.transport.AttributedAdapter` uses for a declared endpoint.
        """
        assert isinstance(self.wrapped, QueryableLedgerAdapter), (
            "bound only when the inner ledger can be queried"
        )
        return await self.wrapped.get_by_operation_id(operation_id)

    def applied_count(self, operation_id: str) -> int:
        """**The measurement every §19.1 assertion reads, taken from the books.**

        §19.1: *"The test inspects the simulated ledger's applied-count for X directly — it does not
        infer the outcome from application state, since inferring from our own records is exactly
        what fails here."*

        Delegated, never counted here. A number this wrapper maintained would be this wrapper's
        opinion of what the ledger did, which is the same category of evidence the scenario exists
        to reject.
        """
        counter = getattr(self.wrapped, "applied_count", None)
        if counter is None:  # pragma: no cover - all three reference adapters expose one
            raise AttributeError(f"{self.wrapped!r} reports no applied-count to measure")
        count = counter(operation_id)
        if not isinstance(count, int):  # pragma: no cover - defensive
            raise TypeError(f"{self.wrapped!r} reported a non-integer applied-count")
        return count

    @property
    def total_applied(self) -> int:
        """Every posting the inner ledger committed, across all identifiers.

        §19's results column, delegated for the same reason :meth:`applied_count` is: a number this
        wrapper maintained would be its opinion of what the ledger did.
        """
        total = getattr(self.wrapped, "total_applied", None)
        if not isinstance(total, int):  # pragma: no cover - all three adapters expose one
            raise AttributeError(f"{self.wrapped!r} reports no total applied-count to measure")
        return total

    @property
    def posts_received(self) -> int:
        """How many posts reached the inner ledger. Kept separate from the applied-count.

        A suppressed duplicate is a *received* request that changed nothing, and a suite that only
        counted applications could not tell suppression from the request never arriving — which is
        precisely the difference between the strong configuration and the weak one.
        """
        received = getattr(self.wrapped, "posts_received", None)
        return received if isinstance(received, int) else -1
