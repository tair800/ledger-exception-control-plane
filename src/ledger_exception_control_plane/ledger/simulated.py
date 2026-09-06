"""The reference simulated ledger (increment 4.2).

`PROJECT_SPEC.md` §13.6: *"The reference adapter shipped here is a simulated ledger declaring
`ENFORCES_KEY` **and** `BY_OPERATION_ID`."* This is that adapter.

**It keeps its own applied-count, and that is the point rather than a convenience.** Acceptance
criteria 8, 8a and the conformance suite all insist the duplicate-suppression claim is checked
against *the ledger's* count and never inferred from our own records — because inferring from our
own records is precisely what fails in the lost-response scenario this project exists to survive.
:meth:`SimulatedLedger.applied_count` is that instrument, and 4.3 and 4.5 reuse it.

**Suppression is real, not declared.** A repeat of an operation identifier this ledger has already
applied returns the original posting reference and increments nothing. That is what makes
``ENFORCES_KEY`` an honest declaration here: the conformance suite proves the behaviour before the
capability is recorded as verified, and an unproven claim is downgraded to ``NONE`` regardless of
what this module says about itself.

**No socket, and none is coming here.** A simulated ledger is what lets the whole reliability layer
be proven offline and in CI. A real HTTP adapter would need a live counterparty, and its capability
profile would have to be established from that vendor's documentation rather than assumed — OPEN-11,
which 4.2 does not cross.

**Nothing here computes money.** The amount arrives already computed, already persisted and already
past the database's own money constraints; this module stores what it is handed and never arithmetic
on it. A guard test asserts that no module here performs arithmetic on a money-named value,
builds a ``Decimal`` from anything but a string constant, or calls ``quantize``. The integer
applied-count below is deliberately outside that fence, and the fence names it as admitted.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import uuid
from collections.abc import Callable

from ledger_exception_control_plane.ledger.port import (
    Atomicity,
    Confirmed,
    Found,
    IdempotencyMode,
    IdempotencyScope,
    LedgerAdapterCapabilities,
    Linearizable,
    NotFound,
    PostingInstruction,
    PostingOutcome,
    PostingQueryMode,
    QueryOutcome,
    ReversalMode,
)

__all__ = [
    "AppliedPosting",
    "NonIdempotentLedger",
    "QueryResponder",
    "Responder",
    "SimulatedLedger",
]

#: What a test may substitute for the ledger's ordinary behaviour.
#:
#: Deliberately a plain callable rather than a fault-injection *port*: §19's explicit injection seam
#: is 4.5's deliverable, and building it here would be that increment arriving early and untested.
#: This is the same shape M3.2 used for its transport — the behaviour under test is injected, and
#: the default is the honest one.
Responder = Callable[[str, PostingInstruction], PostingOutcome | None]

#: What a test may substitute for the ledger's ordinary *answer about* a posting (4.4).
#:
#: Symmetric with :data:`Responder`, and needed for the same reason the ``QueryOutcome`` union has
#: three members: this ledger declares ``LINEARIZABLE``, so its own answer is never
#: ``Indeterminate`` — and §13.5 has a rule specifically about ``Indeterminate``, which nothing
#: could exercise otherwise. `port.py` already says an adapter declaring ``EVENTUAL`` *"would have
#: to be able to say `Indeterminate`, and the union exists so that it can"*; this is the seam that
#: lets one.
#:
#: Returning ``None`` falls through to the honest answer, so an injected script can cover the first
#: few queries and let the ledger speak for itself afterwards.
QueryResponder = Callable[[str], "QueryOutcome | None"]


@dataclasses.dataclass(frozen=True, slots=True)
class AppliedPosting:
    """One posting this ledger actually committed."""

    operation_id: str
    posting_ref: str
    amount: decimal.Decimal
    currency: str
    account_code: str
    period: str


class SimulatedLedger:
    """A ledger that enforces the operation identifier and can be queried by it.

    Implements :class:`~.port.QueryableLedgerAdapter`, and therefore
    :class:`~.port.LedgerAdapter`. Both protocols are structural, so this class names neither — the
    conformance suite and the type checker are what establish the relationship, which is the
    arrangement §10.1 asks for: an adapter's fitness is a property of its shape and its proven
    behaviour, not of a base class it inherited.
    """

    def __init__(
        self,
        *,
        name: str = "simulated-ledger",
        endpoint: str = "sim://ledger/postings",
        responder: Responder | None = None,
        query_responder: QueryResponder | None = None,
        capabilities: LedgerAdapterCapabilities | None = None,
    ) -> None:
        self._name = name
        self._endpoint = endpoint
        self._responder = responder
        self._query_responder = query_responder
        self._applied: dict[str, AppliedPosting] = {}
        #: How many times each identifier has actually been *applied to the books*.
        #:
        #: Separate from ``_applied``, and that separation is the whole instrument. The first
        #: version derived the count from membership in ``_applied`` — ``1 if op in applied else
        #: 0`` — which can never return 2, so the conformance suppression proof asserting
        #: "applied-count == 1" was true by construction and would have survived the suppression
        #: being deleted entirely. A reviewer proved it. A real counter can report 2, which is what
        #: makes the proof a proof.
        self._application_count: dict[str, int] = {}
        self._posts_received = 0
        self._capabilities = capabilities or LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.ENFORCES_KEY,
            idempotency_window=dt.timedelta(days=1),
            idempotency_scope=IdempotencyScope.PER_ENDPOINT,
            posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
            query_consistency=Linearizable(),
            max_inflight_window=dt.timedelta(seconds=30),
            atomicity=Atomicity.ATOMIC,
            # RESERVED and read by nothing. Declared as NONE because this ledger offers no unwind,
            # and OPEN-12 must be decided before anything claims otherwise.
            reversal=ReversalMode.NONE,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def endpoint(self) -> str:
        """Where this ledger accepts postings (4.4).

        Configurable so a test can construct the case §13.5 clause 3 cares about: the same operation
        re-sent to a *different* endpoint under ``PER_ENDPOINT`` retention, which is a duplicate
        write wearing an idempotency header rather than a suppressed re-send.
        """
        return self._endpoint

    def capabilities(self) -> LedgerAdapterCapabilities:
        """Declared data. Note this says nothing about whether the claim has been *proven* — that
        is :mod:`~.conformance`'s job, and an unproven claim is downgraded before any caller acts on
        it."""
        return self._capabilities

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        """Apply the instruction, or return whatever the injected responder decided.

        **Suppression happens before the responder is consulted**, so a test that injects an
        ``Unknown`` still cannot make this ledger apply one operation twice. That ordering is what
        the conformance suite proves and what the lost-response scenario depends on: the second send
        of an identifier the ledger already applied is a no-op at the ledger, whatever the client
        subsequently believes.
        """
        self._posts_received += 1

        existing = self._applied.get(operation_id)
        if existing is not None:
            return Confirmed(posting_ref=existing.posting_ref)

        if self._responder is not None:
            injected = self._responder(operation_id, instruction)
            if injected is not None:
                # A responder may decide the request never reached the books. Nothing is applied,
                # and the applied-count is unchanged — which is what makes a simulated `Unknown`
                # a genuine ambiguity rather than a relabelled failure.
                return injected

        posting_ref = f"SIM-{uuid.uuid5(uuid.NAMESPACE_OID, operation_id).hex[:16]}"
        self._application_count[operation_id] = self._application_count.get(operation_id, 0) + 1
        self._applied[operation_id] = AppliedPosting(
            operation_id=operation_id,
            posting_ref=posting_ref,
            amount=instruction.amount,
            currency=instruction.currency,
            account_code=instruction.account_code,
            period=instruction.period,
        )
        return Confirmed(posting_ref=posting_ref)

    async def get_by_operation_id(self, operation_id: str) -> QueryOutcome:
        """Look a posting up by *our* identifier.

        Returns :class:`~.port.NotFound` rather than :class:`~.port.Indeterminate` because this
        ledger declares ``LINEARIZABLE`` consistency: its answer is always current. An adapter
        declaring ``EVENTUAL`` would have to be able to say `Indeterminate`, and the union exists so
        that it can.
        """
        if self._query_responder is not None:
            injected = self._query_responder(operation_id)
            if injected is not None:
                return injected

        applied = self._applied.get(operation_id)
        return Found(posting_ref=applied.posting_ref) if applied is not None else NotFound()

    # -- inspection, for the conformance suite and the chaos suite that follows -----------

    def applied_count(self, operation_id: str) -> int:
        """How many postings this ledger has actually committed for one identifier.

        The measurement every duplicate-suppression claim in this project is checked against,
        deliberately not derived from anything the application recorded — and, since a reviewer
        found the first version derived it from dictionary membership, deliberately not derived
        from anything *this* class recorded as state either. It counts applications, so it can
        return 2, which is the only reason "applied-count == 1" is worth asserting.
        """
        return self._application_count.get(operation_id, 0)

    @property
    def posts_received(self) -> int:
        """How many times ``post`` was called, applied or not.

        Kept separate from :meth:`applied_count` so a test can show the difference: a suppressed
        duplicate is a *received* request that changed nothing, and a test that only counted
        applications could not tell suppression from the request never arriving.
        """
        return self._posts_received

    def applied(self, operation_id: str) -> AppliedPosting | None:
        return self._applied.get(operation_id)


class NonIdempotentLedger:
    """A ledger that declares nothing and suppresses nothing (increment 4.4).

    `IMPLEMENTATION_PLAN.md` §4.4 requires *"a second simulated adapter configured
    `idempotency=NONE, posting_identity_query=NONE`"*, and §13.6 explains what it is for: the chaos
    suite runs every scenario against three capability configurations, because *"a suite that tests
    only the strong adapter proves only the easy case"*.

    **It really does apply the same operation twice**, and that is the whole point. Configuring
    :class:`SimulatedLedger` with weak capabilities would produce an adapter that *declares* nothing
    while still suppressing internally — so a duplicate send would be absorbed by the test double
    and the branch would look safe for a reason that has nothing to do with the system under test.
    Here the applied-count rises with every post.

    That inversion is what makes the ``NONE``/``NONE`` assertions meaningful. When §19.1 requires
    the applied-count to stay at 1 in this branch, it is measuring **our restraint** — no automatic
    re-send occurs at all — rather than the ledger's forgiveness. Under this adapter, a system that
    retried would visibly double-post, which is exactly what 4.5's naive baseline must demonstrate.

    It is not queryable: :meth:`get_by_operation_id` does not exist, so the type itself says the
    reconciliation branch is unavailable rather than a method saying so at runtime.
    """

    def __init__(
        self,
        *,
        name: str = "non-idempotent-ledger",
        endpoint: str = "sim://weak/postings",
        responder: Responder | None = None,
    ) -> None:
        self._name = name
        self._endpoint = endpoint
        self._responder = responder
        self._applications: dict[str, int] = {}
        self._posts_received = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def endpoint(self) -> str:
        """Declared, and it changes nothing. Recording where a send went is not a capability, and
        under ``idempotency=NONE`` there is no bounded re-send for a scope to bound."""
        return self._endpoint

    def capabilities(self) -> LedgerAdapterCapabilities:
        """The weakest honest record: it enforces no key and answers no query.

        Every other field is left at its default, which §10.1 requires to be the weakest value —
        and which 4.2 had to correct once, because two of the duration defaults were the *strongest*
        claim rather than the weakest.
        """
        return LedgerAdapterCapabilities()

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        """Apply unconditionally, then answer. **The responder runs after the books move.**

        A real ledger without an idempotency mechanism behaves exactly this way: it is handed a
        posting instruction and it posts it. Sending twice books twice.

        **The responder ordering is the opposite of :class:`SimulatedLedger`'s, and deliberately
        so.** There, an injected outcome short-circuits before anything is applied, because that
        adapter's job is to prove *suppression* and an injected `Unknown` must be a genuine
        ambiguity with nothing on the books. Here the adapter's job is to be the weakest honest
        ledger, and the scenario that matters against it is §19.1 — *the posting is applied and the
        response is lost*. Consulting the responder first would make an injected `Unknown` mean
        "nothing happened", which is the one reading that cannot double-post and therefore the one
        that would make every assertion against this adapter vacuous.

        So the books always move, and the responder decides only what the client is told.
        """
        self._posts_received += 1
        self._applications[operation_id] = self._applications.get(operation_id, 0) + 1
        reference = f"NON-{uuid.uuid4().hex[:16]}"

        if self._responder is not None:
            injected = self._responder(operation_id, instruction)
            if injected is not None:
                return injected
        return Confirmed(posting_ref=reference)

    def applied_count(self, operation_id: str) -> int:
        """How many postings this ledger has committed for one identifier.

        Unlike the reference adapter's, this one **can and will exceed 1** — which is the property
        that makes every "applied-count == 1" assertion against it a statement about the system
        rather than about the double.
        """
        return self._applications.get(operation_id, 0)

    @property
    def posts_received(self) -> int:
        return self._posts_received
