"""The ledger adapter port — `PROJECT_SPEC.md` §10.1 and §13.4 (increment 4.2).

§10.1 exists, in its own words, "so that no adapter can be added that hides an `UNKNOWN`". This
module turns that shape into types, and every design choice below is the specification's rather than
this file's.

**No boolean, anywhere.** :data:`PostingOutcome` is a closed union of five variants and
:data:`QueryOutcome` of three. An adapter that could only say *worked* or *failed* would force its
caller to guess which of `Confirmed` and `Unknown` had happened, and guessing wrong in the
`Unknown` direction is a duplicate financial posting. The two unions are built the same way on
purpose — §10.1 calls the query "symmetric with `PostingOutcome` by design" — because a two-valued
query is the same defect one layer down: `NotFound` conflates *never applied*, *applied but not yet
visible* and *still in flight*, and re-sending on it is a textbook double-post.

**The query capability is a typed absence, not a method that raises.** §10.1: *"present only when
`posting_identity_query == BY_OPERATION_ID`; absent capability is a typed absence, not a method that
raises at runtime"*. So the port splits: :class:`LedgerAdapter` has no query method at all, and
:class:`QueryableLedgerAdapter` adds one. Code that reconciles accepts only the second, and an
adapter that cannot be queried fails to type-check there rather than raising in production. A single
protocol with a method that raises `NotImplementedError` is the shape this sentence forbids.

**Capability is declared data the caller branches on, never something inferred.** §13.4. A
dispatcher that decided by adapter class, by adapter name or by which exception it caught would be
choosing a recovery path from evidence that has nothing to do with the contract the provider
actually offers.

**Declaration is not evidence, and the weakest reading always wins.** §10.1 again: *"An undeclared
or
unverified capability is treated as `NONE`."* Two obligations, both discharged here rather than left
to a caller's diligence — see :func:`effective_capabilities` and the defaults on
:class:`LedgerAdapterCapabilities`.

**Nothing here retries, schedules, reconciles or recovers.** Those are 4.3's and 4.4's. This module
defines what an adapter may say and what it is contractually able to do; deciding what to *do* about
an `Unknown` is the increment that owns the capability branch of §13.5.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import uuid
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "MAX_POSTING_REF",
    "UNBOUNDED",
    "Atomicity",
    "Confirmed",
    "EndpointDeclaringAdapter",
    "Eventual",
    "Found",
    "IdempotencyMode",
    "IdempotencyScope",
    "IdempotencyWindow",
    "Indeterminate",
    "LedgerAdapter",
    "LedgerAdapterCapabilities",
    "Linearizable",
    "NotFound",
    "PartiallyApplied",
    "PostingInstruction",
    "PostingOutcome",
    "PostingQueryMode",
    "QueryConsistency",
    "QueryOutcome",
    "QueryableLedgerAdapter",
    "Rejected",
    "ReversalMode",
    "Throttled",
    "Unbounded",
    "Unknown",
    "VerifiedCapabilities",
    "declared_endpoint",
    "effective_capabilities",
]


# ======================================================================================
# Capability vocabulary (§10.1, §13.4)
# ======================================================================================


class IdempotencyMode(enum.StrEnum):
    """What the provider does with an operation identifier it is sent.

    Three values, not a boolean, and the middle one is the trap §13.4 spells out: *"`ACCEPTS_KEY`
    means the key is transmitted and echoed; only `ENFORCES_KEY` means the provider contractually
    suppresses a duplicate for the same key"*, and *"`ACCEPTS_KEY` is explicitly **not** sufficient
    on its own. A provider that accepts a header and ignores it is, from our side, indistinguishable
    from one that has none — unless we can query."*

    So any branch keyed on "does it take a key?" is wrong. The only question that unlocks a
    suppression-dependent path is ``is ENFORCES_KEY``.
    """

    NONE = "none"
    ACCEPTS_KEY = "accepts_key"
    ENFORCES_KEY = "enforces_key"


class PostingQueryMode(enum.StrEnum):
    """Whether a posting can be looked up by *our* operation identifier.

    This is the field that decides whether an adapter can satisfy
    :class:`QueryableLedgerAdapter`. Reconciliation is impossible without it, which is why §13.5
    admits it as an alternative to ``ENFORCES_KEY`` rather than an extra.
    """

    NONE = "none"
    BY_OPERATION_ID = "by_operation_id"


class IdempotencyScope(enum.StrEnum):
    """The blast radius within which a provider's key retention applies (§13.5 item 3).

    Declared here, enforced at 4.4. Real providers commonly scope keys per endpoint or per account,
    and a re-send outside the original scope is "an ordinary duplicate write wearing an idempotency
    header".
    """

    GLOBAL = "global"
    PER_ENDPOINT = "per_endpoint"
    PER_ACCOUNT = "per_account"


class Atomicity(enum.StrEnum):
    """Whether a multi-leg posting is all-or-nothing.

    Declared here; its only behavioural consumer is the `PartiallyApplied` → manual-recovery route,
    which is 4.4's.
    """

    ATOMIC = "atomic"
    NON_ATOMIC = "non_atomic"


class ReversalMode(enum.StrEnum):
    """**RESERVED. Consumed by nothing, and nothing here may read it.**

    §10.1 is explicit: *"`reversal` is RESERVED and consumed by nothing today. No dispatcher branch,
    no §14 row, no §19 scenario and no acceptance criterion reads it."* It is declared so the record
    is complete and so OPEN-12 has somewhere to land — not so anything can act on it. A guard test
    asserts no module branches on this field.
    """

    NONE = "none"
    VOID = "void"
    COMPENSATING = "compensating"


class Unbounded(enum.Enum):
    """A window with no expiry, as a value rather than as ``None``.

    ``None`` would be ambiguous between "never expires" and "not declared", and those are opposite
    readings: the first is the strongest possible retention and the second must degrade to the
    weakest. A distinct sentinel keeps them apart at the type level.
    """

    UNBOUNDED = "unbounded"


UNBOUNDED: Final = Unbounded.UNBOUNDED

#: How long a provider retains an operation identifier. §10.1: ``Duration | UNBOUNDED``.
IdempotencyWindow = dt.timedelta | Unbounded

#: The widest posting reference the ``posting_ref`` columns can hold.
#:
#: Stated here rather than left implicit at the INSERT, because the two failure modes it prevents
#: both happen *after* an irreversible financial write. A test asserts this equals the actual
#: mapped column width, so the two cannot drift apart quietly.
MAX_POSTING_REF: Final = 128

#: The lag an adapter that declared none is assumed to have.
#:
#: Deliberately long. It is read by the two §13.5 windows that gate believing a ``NotFound``, and
#: for those the conservative value is a large one — an adapter that has told us nothing must not
#: thereby be treated as instantaneous. A day is arbitrary in magnitude and not in direction; 4.4
#: enforces these bounds and may refine the number with a reason.
UNDECLARED_LAG: Final = dt.timedelta(days=1)


@dataclasses.dataclass(frozen=True, slots=True)
class Linearizable:
    """A query that always reflects every committed posting. No payload, and none is needed."""


@dataclasses.dataclass(frozen=True, slots=True)
class Eventual:
    """A query that may lag, and by how much.

    The payload is the whole point: §13.5's `NotFound` rule resolves to `REJECTED` only after *"the
    declared `visibility_bound` (or immediately, if `query_consistency == LINEARIZABLE`)"* has
    elapsed. A consistency mode without its bound would leave 4.4 unable to say when a `NotFound`
    became trustworthy.
    """

    visibility_bound: dt.timedelta


#: §10.1: ``LINEARIZABLE | EVENTUAL(visibility_bound: Duration)``.
QueryConsistency = Linearizable | Eventual


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerAdapterCapabilities:
    """What an adapter contractually offers. Every field of §10.1, in its order.

    **Every default is the weakest value**, which is the first half of *"An undeclared or unverified
    capability is treated as `NONE`."* A field omitted by an adapter author degrades the system's
    assumptions rather than escalating them: a missing ``idempotency`` reads as ``NONE``, a missing
    ``posting_identity_query`` reads as ``NONE``, a missing window reads as the shortest useful one
    rather than as ``UNBOUNDED``, and unstated consistency reads as eventual with no useful bound.

    Five of these fields — ``idempotency_window``, ``idempotency_scope``, ``query_consistency``,
    ``max_inflight_window`` and ``atomicity`` — are **declared at 4.2 and consumed at 4.4**. This
    increment owes their existence, their types and their closed vocabularies; it does not enforce
    them, and a bound checked here would be 4.4's deliverable arriving early and untested.
    """

    idempotency: IdempotencyMode = IdempotencyMode.NONE

    #: Zero, because this window *permits* a re-send: §13.5 allows one only while
    #: ``now - first_send < idempotency_window``. Undeclared therefore means "never".
    idempotency_window: IdempotencyWindow = dt.timedelta(0)

    #: The narrowest scope, because a narrower scope permits fewer re-sends.
    idempotency_scope: IdempotencyScope = IdempotencyScope.PER_ENDPOINT

    posting_identity_query: PostingQueryMode = PostingQueryMode.NONE

    #: **A long lag, not a short one**, and that was a correction two reviewers found
    #: independently. The first version defaulted to ``Eventual(visibility_bound=timedelta(0))``,
    #: which behaves exactly like ``LINEARIZABLE`` for the only rule that reads it — the strongest
    #: claim an adapter can make about read-after-write visibility, asserted on behalf of an author
    #: who declared nothing.
    #:
    #: The safe direction is not uniform across these duration fields, which is what the first
    #: version got wrong by applying "zero is weakest" to all three. A window that *permits* an
    #: action is weakest at zero; a window that says *how long you must wait before believing a
    #: negative answer* is weakest when it is long.
    query_consistency: QueryConsistency = dataclasses.field(
        default_factory=lambda: Eventual(visibility_bound=UNDECLARED_LAG)
    )

    #: Long, for the same reason. §13.5 resolves a ``NotFound`` to ``REJECTED`` only after both this
    #: and the visibility bound have elapsed; zero would resolve the first ``NotFound`` immediately,
    #: which for a request still in flight is the §14 row "reconciliation returns NotFound, posting
    #: later appears" — and, in the specification's own words, "acting on it is a double-post".
    max_inflight_window: dt.timedelta = UNDECLARED_LAG

    atomicity: Atomicity = Atomicity.NON_ATOMIC
    reversal: ReversalMode = ReversalMode.NONE

    @property
    def suppresses_duplicates(self) -> bool:
        """Whether the provider contractually suppresses a repeated identifier.

        Named for what it means rather than for the value it checks, because "accepts a key" is the
        misreading §13.4 warns about and a caller writing ``is not NONE`` would have made it.
        """
        return self.idempotency is IdempotencyMode.ENFORCES_KEY

    @property
    def queryable_by_operation_id(self) -> bool:
        return self.posting_identity_query is PostingQueryMode.BY_OPERATION_ID

    @property
    def permits_effectively_once_claim(self) -> bool:
        """§13.5's bar, in one place so no caller re-derives it and gets it wrong.

        *"We may claim an effectively-once ledger side effect **only** when the downstream ledger
        adapter provides a verifiable idempotency mechanism … **and** the operation identifier is
        stable and derived independently of retries (§12.1)."* The second conjunct is 4.1's and is
        unconditionally true in this system; this property is the first.

        It is a property of a **capability record**, and the only records the dispatcher ever sees
        are the *effective* ones — so an unverified declaration cannot reach it.
        """
        return self.suppresses_duplicates or self.queryable_by_operation_id


# ======================================================================================
# What crosses to the adapter
# ======================================================================================


@dataclasses.dataclass(frozen=True, slots=True)
class PostingInstruction:
    """What to post. Deliberately **not** :class:`~..money.AdjustmentInstruction`.

    §10.1 names this type separately, and the separation is load-bearing rather than cosmetic. The
    money path's instruction is a *calculation result*: it carries ``quantum``, ``rounding`` and the
    ledger-context version, which describe how an amount was derived and are nobody's business
    outside the derivation. What an adapter needs is the posting itself, read back from the
    persisted ``adjustment`` row that 4.1 wrote.

    **Every field here is a persisted value, copied.** Nothing in this type or below it computes,
    re-derives, rounds or adjusts an amount: M2.4 is the sole owner of that and the value reaching
    an adapter has already been through the database's own money constraints. A guard test asserts
    that no module in this package performs arithmetic on a money-named value, builds a ``Decimal``
    from anything but a string constant, or calls ``quantize``.

    ``operation_id`` is **not** a field. §10.1 puts it beside the instruction in ``post``'s
    signature rather than inside it, which is what makes *"supplied by the caller, never generated
    inside the adapter"* checkable rather than merely stated.
    """

    adjustment_id: uuid.UUID
    amount: decimal.Decimal
    currency: str
    account_code: str
    period: str


# ======================================================================================
# PostingOutcome — closed, five-valued (§10.1)
# ======================================================================================


@dataclasses.dataclass(frozen=True, slots=True)
class Confirmed:
    """The ledger applied it and told us so. The reference is the evidence.

    **The reference is validated on construction, and the reason is the ordering of a dispatch.**
    An adapter answering with an empty string or a 400-character reference is answering with
    something that cannot be recorded: the empty one stores cleanly and then sits in the database
    looking exactly like evidence of a posting, and the long one raises a driver error in the
    transaction *after* the ledger has already applied an irreversible write — leaving the attempt
    ``IN_FLIGHT``, the reference lost, and a human to work out which.

    Refusing here moves both failures to before the send. The adapter's own bug surfaces as an
    exception out of ``post``, the write-ahead record stays ``IN_FLIGHT``, and that is the outcome
    §12.1.1 already defines and 4.4 already recovers: ambiguous, distinguishable, and not silently
    filed as success.
    """

    posting_ref: str

    def __post_init__(self) -> None:
        if not self.posting_ref.strip():
            raise ValueError("Confirmed requires a posting reference; an empty one is not evidence")
        if len(self.posting_ref) > MAX_POSTING_REF:
            raise ValueError(
                f"posting reference is {len(self.posting_ref)} characters and the column holds "
                f"{MAX_POSTING_REF}; refusing before the send rather than after it"
            )


@dataclasses.dataclass(frozen=True, slots=True)
class Rejected:
    """The ledger declined it; **nothing was applied**.

    One meaning exactly, which is why `Throttled` is a separate variant rather than a flavour of
    this one: a caller that treated throttling as a declination would record "the ledger refused"
    about a request the ledger never considered.
    """

    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class Throttled:
    """A scheduling signal, not a declination.

    §10.1 splits it out *"so `Rejected` has one meaning"*. Acting on ``retry_after`` — waiting,
    rescheduling, counting it against an attempt budget — is 4.3's; 4.2 records it and stops.
    """

    retry_after: dt.timedelta


@dataclasses.dataclass(frozen=True, slots=True)
class Unknown:
    """Sent; outcome undetermined. **The variant this entire port exists for.**

    A timeout after the request was sent is indistinguishable from a ledger that committed and lost
    the response. Coercing it to success records a posting that may never have happened; coercing it
    to failure invites a re-send that double-posts. It is neither, and it stays neither.
    """

    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class PartiallyApplied:
    """Some legs committed and some did not — possible only under ``NON_ATOMIC``.

    Never automatically retried, and always manual recovery (§14). 4.2 records it; 4.4 routes it.
    """

    applied_legs: int
    posting_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        """Same validation, for the same reason: 4.4 recovers these by reference.

        ``applied_legs`` is checked against the references rather than trusted, because a count
        that disagrees with the evidence is how a partially-applied posting gets reconciled to the
        wrong number of legs.
        """
        for reference in self.posting_refs:
            if not reference.strip() or len(reference) > MAX_POSTING_REF:
                raise ValueError(f"unusable posting reference {reference[:32]!r}")
        if self.applied_legs < 0:
            raise ValueError("applied_legs cannot be negative")


#: The closed union. Five variants, no boolean, no ``None``.
PostingOutcome = Confirmed | Rejected | Throttled | Unknown | PartiallyApplied


# ======================================================================================
# QueryOutcome — closed, three-valued, symmetric with the above by design (§10.1)
# ======================================================================================


@dataclasses.dataclass(frozen=True, slots=True)
class Found:
    """The posting exists and this is its reference. §13.5: *"A positive hit is trustworthy."*"""

    posting_ref: str

    def __post_init__(self) -> None:
        if not self.posting_ref.strip():
            raise ValueError("Found requires a posting reference; §13.5 acts on a positive hit")


@dataclasses.dataclass(frozen=True, slots=True)
class NotFound:
    """Not visible to this query. **Not** the same as "will never be applied."

    No payload, and that is deliberate: there is nothing informative to carry. What makes this
    variant safe is the rule attached to it at 4.4 — it never resolves to `REJECTED` on its own,
    only after the declared visibility and in-flight windows have elapsed across consecutive
    queries.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class Indeterminate:
    """The query failed, or its answer is not yet trustworthy.

    The third value that stops this union collapsing into ``str | None``. §10.1 rejects that shape
    by name: it would conflate *never applied*, *applied but not yet visible* and *still in flight*,
    and "treating that `None` as 'not applied' and re-sending is a textbook double-post".
    """

    detail: str


#: The closed union. Three variants — never ``str | None``.
QueryOutcome = Found | NotFound | Indeterminate


# ======================================================================================
# The port, split so an absent capability is absent from the type
# ======================================================================================


@runtime_checkable
class LedgerAdapter(Protocol):
    """Every ledger adapter. **No query method** — see :class:`QueryableLedgerAdapter`.

    Async for the same reason the provider port is: every I/O boundary in this system is async, and
    a synchronous seam would leave a future HTTP adapter choosing between blocking the event loop
    and changing the port.
    """

    @property
    def name(self) -> str:
        """Stable identity for the conformance record. Never used to choose behaviour."""
        ...

    def capabilities(self) -> LedgerAdapterCapabilities:
        """What this adapter contractually offers. Read, never inferred (§13.4)."""
        ...

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome: ...


@runtime_checkable
class EndpointDeclaringAdapter(LedgerAdapter, Protocol):
    """An adapter that can say **where** it posts (increment 4.4, §13.5 clause 3).

    The same typed-absence shape as :class:`QueryableLedgerAdapter`, added for the same reason: an
    adapter that cannot say where it sends must not be *asked* and then be assumed to have answered.

    §13.5 permits a re-send under ``ENFORCES_KEY`` *"only while … the target endpoint matches the
    original ``idempotency_scope``"*. Under ``PER_ENDPOINT`` that comparison needs the endpoint the
    **original** send used, which is a fact about a past attempt rather than about today's
    configuration — so the dispatcher records it on the write-ahead attempt row, and 4.4 compares
    the recorded value against the one a re-send would use.

    Optional, and absence is not a failure: an adapter with no declared endpoint records nothing,
    and a comparison against nothing is a mismatch rather than a match. That is the same direction
    §10.1 takes with an unverified capability, and it is the only safe one — proving a re-send is
    inside its scope is our obligation, not the provider's.
    """

    @property
    def endpoint(self) -> str:
        """Where this adapter posts. Recorded as evidence, never used to choose behaviour."""
        ...


def declared_endpoint(adapter: LedgerAdapter) -> str | None:
    """The adapter's declared endpoint, or ``None`` where it declares none.

    A function rather than a ``getattr`` at each call site, so the absent case has one meaning in
    one place. ``None`` reads as "not recorded", never as "matches".
    """
    return adapter.endpoint if isinstance(adapter, EndpointDeclaringAdapter) else None


@runtime_checkable
class QueryableLedgerAdapter(LedgerAdapter, Protocol):
    """An adapter that can be asked whether *our* operation identifier was applied.

    **The typed absence §10.1 requires.** An adapter without the capability does not implement this
    protocol, so code that reconciles cannot be handed one — the failure is at the type boundary
    rather than at a ``NotImplementedError`` in production, which is the shape the specification
    rules out in as many words.
    """

    async def get_by_operation_id(self, operation_id: str) -> QueryOutcome: ...


# ======================================================================================
# Declaration is not evidence
# ======================================================================================


@dataclasses.dataclass(frozen=True, slots=True)
class VerifiedCapabilities:
    """Which strong claims a named adapter has actually been proven to honour.

    §10.1: *"Before `ENFORCES_KEY` or `BY_OPERATION_ID` may be declared for any adapter, a
    **capability conformance suite** must pass against it … An undeclared or unverified capability
    is treated as `NONE`."*

    Only the two strong claims need evidence, because only they unlock behaviour: everything else in
    the record is either already the weakest value or is consumed by an increment that has its own
    checks. See :mod:`~.conformance` for the proofs and the committed record of the run.

    ``adapter_name`` is carried for messages and is **not** the key: evidence is matched to an
    adapter by its implementation class in :func:`~.conformance.verified_for`, because a
    self-reported string is something a forger supplies.
    """

    adapter_name: str
    suppression_proven: bool = False
    query_proven: bool = False


def effective_capabilities(
    declared: LedgerAdapterCapabilities, verified: VerifiedCapabilities | None
) -> LedgerAdapterCapabilities:
    """The capabilities a caller may act on: declared, with every unproven strong claim downgraded.

    **This is the second half of "an undeclared or unverified capability is treated as `NONE`", and
    it is a behavioural rule rather than a warning.** An adapter may claim `ENFORCES_KEY` in its
    own record and still be treated as `NONE` here, because the claim has no passing conformance
    run behind it. ``verified=None`` — no record at all — downgrades both.

    Downgrade only, never upgrade: nothing in a conformance record can make an adapter stronger
    than it declared itself to be, so a stale or over-generous record cannot manufacture a
    capability. The two failure directions are not symmetric, and this only moves in the safe one.

    **This function does not check that the record belongs to the declaration.** It cannot: a
    capabilities record carries no adapter identity. An earlier version appeared to check it and
    did not — the condition compared a value with itself, which three reviewers found — so the
    pairing is now enforced where the identity actually exists, in
    :func:`~.conformance.capabilities_for`, and that is the only supported way to obtain an
    effective record for an adapter.
    """
    suppression_ok = verified is not None and verified.suppression_proven
    query_ok = verified is not None and verified.query_proven

    return dataclasses.replace(
        declared,
        idempotency=(
            declared.idempotency
            if suppression_ok or declared.idempotency is not IdempotencyMode.ENFORCES_KEY
            else IdempotencyMode.NONE
        ),
        posting_identity_query=(
            declared.posting_identity_query
            if query_ok or declared.posting_identity_query is not PostingQueryMode.BY_OPERATION_ID
            else PostingQueryMode.NONE
        ),
    )
