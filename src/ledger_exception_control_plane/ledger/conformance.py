"""Declaration is not evidence — the capability conformance suite (increment 4.2).

`PROJECT_SPEC.md` §10.1: *"Before `ENFORCES_KEY` or `BY_OPERATION_ID` may be declared for any
adapter, a **capability conformance suite** must pass against it: post the same `operation_id` twice
and assert the applied-count is 1; query a known posting and assert it is returned. The conformance
run and its date are recorded in the repository. An undeclared or unverified capability is treated
as `NONE`."*

Two proofs, no more, and each one measured **against the ledger** rather than against anything this
system recorded. Inferring suppression from our own attempt rows would be the lost-response defect
wearing a conformance badge: the records would agree with themselves while the ledger held two
postings.

**This is a gate, not a regression test.** :func:`run_conformance` returns what was *proven*, and
:func:`capabilities_for` is the only supported route from an adapter to a capability record a caller
may act on — it downgrades every unproven strong claim to ``NONE`` before returning. An adapter can
say ``ENFORCES_KEY`` as loudly as it likes; without a passing run behind it the dispatcher sees
``NONE`` and behaves accordingly.

**Where the run and its date are recorded.** In :data:`CONFORMANCE_RUNS` below — a committed
constant, reviewed like any other code change, following the same convention as the model pins in
ADR-049 ("pinned 2026-09-01"). Not a database table: `adapter_capability` is named in two separate
guard tests as a table that must not exist, so the repository has already decided this record is
committed data rather than runtime state. The suite itself runs in CI, so a declaration that stopped
being true would fail the build rather than sit in a stale row.
"""

from __future__ import annotations

import dataclasses
import decimal
import typing
import uuid
from typing import Final

from ledger_exception_control_plane.ledger.port import (
    Confirmed,
    Found,
    IdempotencyMode,
    Indeterminate,
    LedgerAdapter,
    LedgerAdapterCapabilities,
    PostingInstruction,
    PostingQueryMode,
    QueryableLedgerAdapter,
    Unknown,
    VerifiedCapabilities,
    effective_capabilities,
)

__all__ = [
    "CONFORMANCE_RUNS",
    "AdapterInadmissibleError",
    "ConformanceReport",
    "ConformanceRun",
    "assert_admissible",
    "capabilities_for",
    "run_conformance",
    "verified_for",
]


class AdapterInadmissibleError(TypeError):
    """The adapter's own types cannot express an outcome the specification requires.

    Raised rather than returned, and a ``TypeError`` because that is what it is: the object is the
    wrong shape for the contract. §10.1 — *"An adapter whose return type cannot express it is
    inadmissible, and a contract test rejects it."*
    """


def assert_admissible(adapter: object) -> None:
    """Refuse an adapter whose signatures cannot say ``Unknown`` or ``Indeterminate``.

    **A real rejection, not a type annotation nobody reads.** The two variants exist because a
    caller must be able to tell *sent, outcome undetermined* from *the ledger declined*, and an
    adapter whose ``post`` returns ``Confirmed | Rejected`` has removed that distinction at the
    boundary — every caller downstream then guesses, and guessing toward success records a posting
    that may never have happened while guessing toward failure invites a double-post.

    The same argument applies one layer down to the query, which is why ``str | None`` is
    inadmissible too: §10.1 rejects it by name because ``None`` would conflate *never applied*,
    *applied but not yet visible* and *still in flight*.

    Checked by resolving the annotations rather than by calling anything, so an adapter is judged
    before it is ever handed a real operation.
    """
    if not callable(getattr(adapter, "capabilities", None)):
        raise AdapterInadmissibleError(
            f"{_label(adapter)} declares no capabilities(); capability is data the system reads "
            "and branches on, and an adapter that publishes none has published NONE"
        )

    post = getattr(adapter, "post", None)
    if post is None:
        raise AdapterInadmissibleError(f"{_label(adapter)} has no post method")
    if not _can_return(post, Unknown):
        raise AdapterInadmissibleError(
            f"{_label(adapter)}.post cannot return Unknown, so it cannot express "
            "'sent, outcome undetermined' — the one outcome this port exists for"
        )

    query = getattr(adapter, "get_by_operation_id", None)
    if query is None:
        # A typed absence is correct and expected: an adapter without the capability simply does
        # not have the method. What is inadmissible is *having* one that cannot say Indeterminate.
        return
    if not _can_return(query, Indeterminate):
        raise AdapterInadmissibleError(
            f"{_label(adapter)}.get_by_operation_id cannot return Indeterminate; a two-valued "
            "query conflates 'never applied' with 'not yet visible', and acting on that is a "
            "double-post"
        )


def _label(adapter: object) -> str:
    return getattr(adapter, "name", None) or type(adapter).__name__


def _can_return(method: object, variant: type) -> bool:
    """Whether a method's declared return type admits ``variant``.

    Unannotated is inadmissible rather than assumed-fine: an adapter that declares nothing has
    declared nothing, and the whole section is about not assuming.
    """
    try:
        hints = typing.get_type_hints(method)
    except Exception:  # an unresolvable annotation is itself a refusal
        return False

    returns = hints.get("return")
    if returns is None:
        return False

    arms = set(typing.get_args(returns)) or {returns}
    if variant not in arms:
        return False

    # A union that *also* admits None or a bool has not expressed the outcome, it has smuggled a
    # two-valued answer back in beside it. §10.1 rejects the ``str | None`` shape by name for the
    # query, and the same reasoning applies to the posting outcome: a caller receiving
    # ``PostingOutcome | None`` still has to decide what ``None`` meant.
    return all(banned not in arms for banned in (type(None), bool))


def implementation_of(adapter: object) -> str:
    """The adapter's implementation identity: the class that actually runs.

    **Not its ``name``**, and that was a correction four reviewers found independently. Keying
    verification on a self-reported string meant any object could inherit the reference adapter's
    proven claims by declaring ``name = "simulated-ledger"`` — a two-line forgery that turned
    ``ENFORCES_KEY`` and ``BY_OPERATION_ID`` on for an adapter that suppresses nothing. A module
    path and qualified name cannot be adopted without actually being that class.

    ``name`` survives for display and for the human-readable record; it decides nothing.

    **One wrapper is unwrapped, on an exact type check.** 4.3 dispatches through
    :class:`~.transport.AttributedAdapter`, whose only job is to label the exceptions the adapter
    raises so a database failure cannot be mistaken for a ledger one. It forwards every call to the
    adapter it holds, so the wrapped adapter's conformance record genuinely applies — and without
    this, wrapping the reference ledger produced an unrecognised class, downgraded both proven
    claims to ``NONE``, and refused a re-send §13.5 permits.

    ``type(...) is`` rather than ``isinstance``, deliberately: a subclass could override ``post``
    and stop delegating, which would be the forgery this function exists to prevent wearing a
    different hat. Nothing else is unwrapped, and a plain attribute named ``wrapped`` on some other
    object means nothing here.
    """
    from ledger_exception_control_plane.ledger.transport import AttributedAdapter

    if type(adapter) is AttributedAdapter:
        adapter = adapter.wrapped

    cls = type(adapter)
    return f"{cls.__module__}.{cls.__qualname__}"


@dataclasses.dataclass(frozen=True, slots=True)
class ConformanceRun:
    """A recorded pass of the suite against one adapter implementation, with the date it was run."""

    #: The fully-qualified class, from :func:`implementation_of`. The key.
    implementation: str
    #: What the adapter calls itself. Display only.
    adapter_name: str
    suppression_proven: bool
    query_proven: bool
    #: ISO date, by hand. A clock reading here would make the record change without review, and the
    #: whole value of this constant is that changing it is a reviewed edit.
    run_on: str


#: The committed record §10.1 asks for. One entry per adapter that has passed the suite.
#:
#: `simulated-ledger` is the reference adapter of §13.6. Its two strong claims are proven by
#: :func:`run_conformance` in the committed test suite, which runs on every CI build — so this
#: constant records *that* the run happened and when it was last confirmed by hand, while the suite
#: keeps it honest continuously.
CONFORMANCE_RUNS: Final[tuple[ConformanceRun, ...]] = (
    ConformanceRun(
        implementation="ledger_exception_control_plane.ledger.simulated.SimulatedLedger",
        adapter_name="simulated-ledger",
        suppression_proven=True,
        query_proven=True,
        run_on="2026-09-05",
    ),
)


@dataclasses.dataclass(frozen=True, slots=True)
class ConformanceReport:
    """What a live run proved, and what it observed while proving it.

    The counts are carried so a caller — and a reviewer reading a failure — can see the evidence
    rather than a bare boolean. ``applied_after_two_posts`` is the number §10.1 names.
    """

    adapter_name: str
    suppression_proven: bool
    query_proven: bool
    applied_after_two_posts: int
    posts_received: int

    def as_verified(self) -> VerifiedCapabilities:
        return VerifiedCapabilities(
            adapter_name=self.adapter_name,
            suppression_proven=self.suppression_proven,
            query_proven=self.query_proven,
        )


#: A posting the suite uses and then leaves behind in the adapter under test.
#:
#: Deterministic, so a failure is reproducible, and obviously synthetic so it cannot be mistaken for
#: a real adjustment. The amount is a fixed literal: this module computes nothing.
_PROBE_INSTRUCTION: Final = PostingInstruction(
    adjustment_id=uuid.UUID("00000000-0000-5000-8000-00000000c0f0"),
    amount=decimal.Decimal("1.0000"),
    currency="EUR",
    account_code="4100",
    period="2026-06",
)


async def run_conformance(adapter: LedgerAdapter, *, probe_operation_id: str) -> ConformanceReport:
    """Run the two proofs §10.1 requires against a live adapter.

    ``probe_operation_id`` is supplied by the caller and must be a value no real operation could
    use, because this genuinely posts to the adapter under test. The suite is run against simulated
    ledgers; pointing it at a real one would apply a real posting, which is a decision OPEN-11 has
    to settle before any real adapter ships.

    **Suppression is measured at the ledger.** Two posts of one identifier, then the adapter's own
    applied-count. An adapter with no way to report that count cannot prove the claim and is
    therefore never verified — which is the correct outcome, not a gap: a claim nobody can check is
    exactly what "declaration is not evidence" refuses to accept.
    """
    declared = adapter.capabilities()

    suppression_proven = False
    applied = 0
    posts_received = 0
    if declared.idempotency is IdempotencyMode.ENFORCES_KEY:
        first = await adapter.post(probe_operation_id, _PROBE_INSTRUCTION)
        second = await adapter.post(probe_operation_id, _PROBE_INSTRUCTION)
        applied = _applied_count(adapter, probe_operation_id)
        posts_received = _posts_received(adapter)
        # Both sends must be answered, and the ledger must hold exactly one posting. A pair of
        # confirmations with an applied-count of 2 is the failure this proof exists to catch, and it
        # is invisible from the client's side without asking the ledger.
        suppression_proven = (
            applied == 1 and isinstance(first, Confirmed) and isinstance(second, Confirmed)
        )

    query_proven = False
    if declared.posting_identity_query is PostingQueryMode.BY_OPERATION_ID:
        if not isinstance(adapter, QueryableLedgerAdapter):
            # Declared queryable, structurally not queryable. Unprovable, so unverified, so NONE.
            query_proven = False
        else:
            if not suppression_proven:
                # The query proof needs a posting to look up. If suppression was not exercised
                # above (because the adapter does not claim ENFORCES_KEY) one is created here.
                await adapter.post(probe_operation_id, _PROBE_INSTRUCTION)
                posts_received = _posts_received(adapter)
            found = await adapter.get_by_operation_id(probe_operation_id)
            query_proven = isinstance(found, Found)

    return ConformanceReport(
        adapter_name=adapter.name,
        suppression_proven=suppression_proven,
        query_proven=query_proven,
        applied_after_two_posts=applied,
        posts_received=posts_received,
    )


def verified_for(adapter: object) -> VerifiedCapabilities | None:
    """The committed conformance record for an adapter implementation, or ``None``.

    Matched on :func:`implementation_of`, so an adapter cannot claim another's evidence by adopting
    its name. ``None`` is the ordinary case for anything nobody has run the suite against, and it
    downgrades both strong claims — an adapter is weak until proven otherwise.
    """
    implementation = implementation_of(adapter)
    for run in CONFORMANCE_RUNS:
        if run.implementation == implementation:
            return VerifiedCapabilities(
                adapter_name=run.adapter_name,
                suppression_proven=run.suppression_proven,
                query_proven=run.query_proven,
            )
    return None


def capabilities_for(adapter: LedgerAdapter) -> LedgerAdapterCapabilities:
    """**The only supported way to obtain capabilities a caller may act on.**

    Reading ``adapter.capabilities()`` directly gives the *declaration*; this gives the declaration
    with every unproven strong claim downgraded to ``NONE``, which is what §10.1 requires anyone
    branching on capability to see. A guard test asserts the dispatcher calls this and never the raw
    method.
    """
    return effective_capabilities(adapter.capabilities(), verified_for(adapter))


def _applied_count(adapter: LedgerAdapter, operation_id: str) -> int:
    """The adapter's own applied-count, if it can report one.

    Duck-typed rather than added to the port, deliberately: an applied-count is a property of a
    *simulated* ledger used for proving a claim, not something a real provider exposes. Putting it
    on :class:`~.port.LedgerAdapter` would oblige every future adapter to invent one, and a made-up
    count is worse than an unprovable claim.
    """
    counter = getattr(adapter, "applied_count", None)
    if counter is None:
        return -1
    count = counter(operation_id)
    return count if isinstance(count, int) else -1


def _posts_received(adapter: LedgerAdapter) -> int:
    received = getattr(adapter, "posts_received", None)
    return received if isinstance(received, int) else -1
