"""M4.2 — the capability-declaring ledger adapter, its outcomes, and the conformance gate.

The plan asks 4.2 for seven assertions. Five of them need no database and live here: the contract
test that rejects an adapter which cannot express `Unknown` or `Indeterminate`; the conformance
proof that a declared `ENFORCES_KEY` adapter really suppresses a repeated identifier, measured by
the ledger's own applied-count; the proof that a declared `BY_OPERATION_ID` adapter really returns a
known posting; that an unverified capability is treated as `NONE`; and that capability is read
rather than inferred. The remaining two — outbox atomicity and the identifier across attempts one
and five — need a real database and live in ``test_dispatch_postgres.py``.

**What is deliberately not tested here, because 4.2 does not build it.** No retry, no backoff, no
attempt ceiling, no `Throttled` scheduling, no DLQ, no `UNKNOWN` reconciliation, no window or scope
enforcement, no manual recovery. Those are 4.3's and 4.4's, and a test for them here would be a
later increment's design arriving without its increment.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import decimal
import inspect
import typing
import uuid
from typing import Final

import pytest
from sqlalchemy import String

from ledger_exception_control_plane.db.control import Adjustment, PostingAttempt
from ledger_exception_control_plane.ledger import (
    MAX_POSTING_REF,
    UNBOUNDED,
    AdapterInadmissibleError,
    Confirmed,
    Eventual,
    Found,
    IdempotencyMode,
    LedgerAdapter,
    LedgerAdapterCapabilities,
    Linearizable,
    NotFound,
    PartiallyApplied,
    PostingInstruction,
    PostingOutcome,
    PostingQueryMode,
    QueryableLedgerAdapter,
    QueryOutcome,
    Rejected,
    ReversalMode,
    SimulatedLedger,
    Throttled,
    Unknown,
    VerifiedCapabilities,
    assert_admissible,
    capabilities_for,
    effective_capabilities,
    run_conformance,
    verified_for,
)
from ledger_exception_control_plane.ledger import conformance as conformance_module
from ledger_exception_control_plane.operations import ResendDecision, resend_decision

PROBE: Final = "c" * 64

INSTRUCTION: Final = PostingInstruction(
    adjustment_id=uuid.UUID("11111111-1111-5111-8111-111111111111"),
    amount=decimal.Decimal("2799.9700"),
    currency="EUR",
    account_code="4100",
    period="2026-06",
)


def _operation(seed: str) -> str:
    """A 64-character hex identifier, shaped like the ones 4.1 derives."""
    return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex * 2


# ======================================================================================
# The outcome unions — closed, and neither collapses to a boolean
# ======================================================================================


def test_the_posting_outcome_union_has_exactly_the_five_variants_the_spec_declares() -> None:
    """§10.1 lists five. Fewer would force a caller to guess; more would be this increment
    inventing vocabulary the persisted enum could not store."""
    from typing import get_args

    assert {variant.__name__ for variant in get_args(PostingOutcome)} == {
        "Confirmed",
        "Rejected",
        "Throttled",
        "Unknown",
        "PartiallyApplied",
    }


def test_the_query_outcome_union_has_exactly_the_three_variants_the_spec_declares() -> None:
    """Three-valued for the same reason the posting outcome is five-valued.

    ``str | None`` is rejected by §10.1 by name: ``None`` would conflate *never applied*, *applied
    but not yet visible* and *still in flight*, and re-sending on it is a textbook double-post.
    """
    from typing import get_args

    assert {variant.__name__ for variant in get_args(QueryOutcome)} == {
        "Found",
        "NotFound",
        "Indeterminate",
    }


def test_the_persisted_vocabulary_covers_every_outcome_variant() -> None:
    """A variant the database cannot store would be an outcome nobody could record.

    The two lists are maintained in different files for different reasons — one is the wire
    contract, the other a column's check constraint — so nothing but this test relates them.

    **The relation is containment, not equality, and it stopped being equality at 4.3.** The
    persisted vocabulary gained ``not_sent``, which no adapter can return because it is not an
    answer: it records an allowlisted transport failure that happened *instead of* one. Equality
    would now force either a sixth adapter variant — breaking the closed five-valued union
    acceptance criterion 8d rests on — or a transport failure with nowhere to be written down.

    Containment in this direction is the property that matters. It fails if an adapter variant
    becomes unstorable, which is the defect the test was written for, and the excess is named
    explicitly below so it cannot grow silently.
    """
    from typing import get_args

    from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
    from ledger_exception_control_plane.operations import outcome_code

    samples: list[PostingOutcome] = [
        Confirmed(posting_ref="ref"),
        Rejected(reason="declined"),
        Throttled(retry_after=dt.timedelta(seconds=1)),
        Unknown(detail="timeout"),
        PartiallyApplied(applied_legs=1, posting_refs=("ref",)),
    ]
    assert len(samples) == len(get_args(PostingOutcome))

    storable = {outcome_code(sample) for sample in samples}
    assert storable <= set(OutcomeCode), "an adapter variant the database cannot store"
    assert set(OutcomeCode) - storable == {OutcomeCode.NOT_SENT}, (
        "the persisted vocabulary has grown a value no adapter produces and nothing explains"
    )


def test_not_sent_is_recorded_by_us_and_never_returned_by_an_adapter() -> None:
    """**The one asymmetry between the two vocabularies, stated so it cannot be widened quietly.**

    §14 names the classification — *"Classified `NOT_SENT`; nothing applied; bounded retry, then
    DLQ. Distinct from `Rejected`, which means the ledger declined"* — and the distinction is the
    whole reason the retry path exists. It is written by the transport classifier when an adapter
    *raises*, never by an adapter answering, so it must be absent from the union and present in the
    column.
    """
    from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode

    assert OutcomeCode.NOT_SENT.value == "not_sent"
    assert "NotSent" not in {arm.__name__ for arm in typing.get_args(PostingOutcome)}
    assert len(typing.get_args(PostingOutcome)) == 5, "the adapter union is closed at five"


def test_every_outcome_carries_the_payload_the_spec_gives_it() -> None:
    """The payloads are not decoration: ``retry_after`` is what 4.3 schedules on and ``detail`` is
    what 4.4 shows an operator. Dropping them would be a later increment's evidence discarded."""
    assert Confirmed(posting_ref="r").posting_ref == "r"
    assert Rejected(reason="why").reason == "why"
    assert Throttled(retry_after=dt.timedelta(seconds=5)).retry_after == dt.timedelta(seconds=5)
    assert Unknown(detail="sent, no response").detail == "sent, no response"

    partial = PartiallyApplied(applied_legs=2, posting_refs=("a", "b"))
    assert (partial.applied_legs, partial.posting_refs) == (2, ("a", "b"))
    assert dataclasses.fields(NotFound) == ()


# ======================================================================================
# Admissibility — the contract test §10.1 requires
# ======================================================================================


class _CannotSayUnknown:
    """An adapter whose post outcome collapses the ambiguity. Inadmissible by construction."""

    name = "cannot-say-unknown"

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities()

    async def post(
        self, operation_id: str, instruction: PostingInstruction
    ) -> Confirmed | Rejected:
        return Confirmed(posting_ref="r")


class _CannotSayIndeterminate:
    """A two-valued query — the ``str | None`` shape §10.1 rejects by name."""

    name = "cannot-say-indeterminate"

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities()

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        return Unknown(detail="x")

    async def get_by_operation_id(self, operation_id: str) -> Found | NotFound:
        return NotFound()


class _Unannotated:
    """Declares nothing about what it returns."""

    name = "unannotated"

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities()

    async def post(self, operation_id, instruction):  # type: ignore[no-untyped-def]
        return Unknown(detail="x")


def test_an_adapter_that_cannot_express_unknown_is_rejected() -> None:
    """**Acceptance criterion 8d, posting half.**

    An adapter returning only ``Confirmed | Rejected`` has deleted the distinction between *the
    ledger declined* and *we do not know* at the boundary. Every caller downstream then guesses, and
    both guesses are wrong in a way that costs money.
    """
    with pytest.raises(AdapterInadmissibleError, match="cannot return Unknown"):
        assert_admissible(_CannotSayUnknown())


def test_an_adapter_whose_query_cannot_express_indeterminate_is_rejected() -> None:
    """**Acceptance criterion 8d, query half.** The same defect one layer down."""
    with pytest.raises(AdapterInadmissibleError, match="cannot return Indeterminate"):
        assert_admissible(_CannotSayIndeterminate())


def test_an_unannotated_adapter_is_rejected() -> None:
    """Undeclared is not the same as fine. The whole section is about not assuming."""
    with pytest.raises(AdapterInadmissibleError):
        assert_admissible(_Unannotated())


def test_an_adapter_with_nothing_at_all_is_rejected() -> None:
    """A bare object declares no capabilities, which is itself a refusal."""
    with pytest.raises(AdapterInadmissibleError, match="declares no capabilities"):
        assert_admissible(object())


def test_an_adapter_with_capabilities_but_no_post_method_is_rejected() -> None:
    class NoPost:
        name = "no-post"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

    with pytest.raises(AdapterInadmissibleError, match="no post method"):
        assert_admissible(NoPost())


def test_an_outcome_union_that_smuggles_none_beside_it_is_rejected() -> None:
    """**``PostingOutcome | None`` is not five-valued, it is six**, and the sixth means nothing.

    §10.1 rejects the ``str | None`` shape for the query by name; the same reasoning applies to the
    posting outcome. A caller handed ``None`` beside a perfectly good union still has to decide what
    it meant, which is exactly the guess the union exists to remove. The first admissibility check
    passed this adapter because it only asked whether ``Unknown`` was *among* the arms.
    """

    class Optional:
        name = "optional"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(
            self, operation_id: str, instruction: PostingInstruction
        ) -> PostingOutcome | None:
            return Unknown(detail="x")

    with pytest.raises(AdapterInadmissibleError, match="cannot return Unknown"):
        assert_admissible(Optional())


def test_an_outcome_union_that_smuggles_a_bool_beside_it_is_rejected() -> None:
    """The same defect wearing the older shape: a success flag kept alongside the real answer."""

    class Flagged:
        name = "flagged"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(
            self, operation_id: str, instruction: PostingInstruction
        ) -> PostingOutcome | bool:
            return Unknown(detail="x")

    with pytest.raises(AdapterInadmissibleError, match="cannot return Unknown"):
        assert_admissible(Flagged())


def test_the_reference_adapter_is_admissible() -> None:
    """The control. A check that rejected everything would pass the four tests above and be
    useless."""
    assert_admissible(SimulatedLedger())


def test_an_adapter_without_a_query_method_is_admissible_not_rejected() -> None:
    """**A typed absence is correct, and must not be mistaken for a defect.**

    §10.1 wants the query method *absent* when the capability is absent. An admissibility check
    that demanded one would push every author toward the shape the specification forbids: a method
    that exists and raises.
    """

    class PostOnly:
        name = "post-only"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            return Unknown(detail="x")

    assert_admissible(PostOnly())
    assert not isinstance(PostOnly(), QueryableLedgerAdapter)
    assert isinstance(PostOnly(), LedgerAdapter)


# ======================================================================================
# The typed absence, structurally
# ======================================================================================


def test_the_reference_adapter_satisfies_both_protocols() -> None:
    ledger = SimulatedLedger()
    assert isinstance(ledger, LedgerAdapter)
    assert isinstance(ledger, QueryableLedgerAdapter)


def test_the_base_port_declares_no_query_method() -> None:
    """**The structural half of the typed absence.**

    If ``LedgerAdapter`` carried ``get_by_operation_id``, every adapter would have to supply one and
    the ones that cannot would raise — which is precisely the shape §10.1 rules out. Asserted on the
    protocol itself rather than on prose about it.
    """
    assert not hasattr(LedgerAdapter, "get_by_operation_id")
    assert hasattr(QueryableLedgerAdapter, "get_by_operation_id")


# ======================================================================================
# Capability matrix — declared, and what each declaration does and does not unlock
# ======================================================================================


@pytest.mark.parametrize(
    ("idempotency", "query", "suppresses", "queryable", "may_claim"),
    [
        (IdempotencyMode.NONE, PostingQueryMode.NONE, False, False, False),
        (IdempotencyMode.ACCEPTS_KEY, PostingQueryMode.NONE, False, False, False),
        (IdempotencyMode.ENFORCES_KEY, PostingQueryMode.NONE, True, False, True),
        (IdempotencyMode.NONE, PostingQueryMode.BY_OPERATION_ID, False, True, True),
        (IdempotencyMode.ACCEPTS_KEY, PostingQueryMode.BY_OPERATION_ID, False, True, True),
        (IdempotencyMode.ENFORCES_KEY, PostingQueryMode.BY_OPERATION_ID, True, True, True),
    ],
)
def test_the_capability_matrix_decides_what_may_be_claimed(
    idempotency: IdempotencyMode,
    query: PostingQueryMode,
    suppresses: bool,
    queryable: bool,
    may_claim: bool,
) -> None:
    """§13.5's bar, over every combination of the two fields that can meet it.

    ``idempotency == ENFORCES_KEY`` **or** ``posting_identity_query == BY_OPERATION_ID``. The row
    that matters most is the second: **`ACCEPTS_KEY` alone unlocks nothing.** §13.4 is explicit that
    a provider which accepts a header and ignores it is indistinguishable from one that has none.
    """
    capabilities = LedgerAdapterCapabilities(idempotency=idempotency, posting_identity_query=query)

    assert capabilities.suppresses_duplicates is suppresses
    assert capabilities.queryable_by_operation_id is queryable
    assert capabilities.permits_effectively_once_claim is may_claim


def test_accepts_key_is_never_treated_as_suppression() -> None:
    """Stated separately because it is the single easiest mistake in this section to make.

    A branch written as ``idempotency is not NONE`` passes every other test in this module and is
    wrong: it would permit a re-send of an irreversible write to a provider that echoes the key and
    ignores it.
    """
    accepts = LedgerAdapterCapabilities(idempotency=IdempotencyMode.ACCEPTS_KEY)
    none = LedgerAdapterCapabilities(idempotency=IdempotencyMode.NONE)

    assert accepts.suppresses_duplicates is False
    assert accepts.permits_effectively_once_claim == none.permits_effectively_once_claim


def test_every_capability_field_the_spec_names_exists() -> None:
    """All eight, checked against the record rather than against a list in a docstring.

    Five of them are declared here and consumed at 4.4. A field missing now would leave that
    increment unable to enforce a bound the specification gives it.
    """
    assert {field.name for field in dataclasses.fields(LedgerAdapterCapabilities)} == {
        "idempotency",
        "idempotency_window",
        "idempotency_scope",
        "posting_identity_query",
        "query_consistency",
        "max_inflight_window",
        "atomicity",
        "reversal",
    }


def test_an_undeclared_capability_defaults_to_the_weakest_value() -> None:
    """**The first half of "an undeclared or unverified capability is treated as NONE".**

    An adapter author who omits a field must make the system assume *less*, never more. The two
    fields that unlock behaviour both default to ``NONE``, so a record built with no arguments at
    all permits nothing.
    """
    bare = LedgerAdapterCapabilities()

    assert bare.idempotency is IdempotencyMode.NONE
    assert bare.posting_identity_query is PostingQueryMode.NONE
    assert bare.permits_effectively_once_claim is False
    assert bare.reversal is ReversalMode.NONE


def test_the_undeclared_durations_default_in_the_safe_direction() -> None:
    """**The second half, and the half where "weakest" is not the same as "zero".**

    A mutation reverting :data:`UNDECLARED_LAG` to zero survived the whole unit suite, because the
    defaults test above checks the two enum fields and nothing else — while the direction of these
    three durations is the subject of its own decision record.

    They do not all point the same way:

    - ``idempotency_window`` **permits** a re-send (§13.5 allows one only while
      ``now - first_send < idempotency_window``), so undeclared must mean *never*: zero.
    - ``query_consistency`` and ``max_inflight_window`` say **how long you must wait before
      believing a negative answer**, so undeclared must mean *a long lag*. ``Eventual(0)`` behaves
      exactly like ``LINEARIZABLE`` for the only rule that reads it — the strongest possible claim
      about read-after-write visibility, asserted on behalf of an author who declared nothing.

    The bound is asserted as "at least an hour" rather than as equality with the constant, because
    equality with the constant is satisfied by any value the constant happens to have — including
    zero, which is what the mutation set it to.
    """
    bare = LedgerAdapterCapabilities()

    assert bare.idempotency_window == dt.timedelta(0), "a window that permits must be zero"

    assert not isinstance(bare.query_consistency, Linearizable), (
        "an adapter that declared nothing must not be credited with linearizable reads"
    )
    assert isinstance(bare.query_consistency, Eventual)
    assert bare.query_consistency.visibility_bound >= dt.timedelta(hours=1), (
        "an undeclared visibility lag must be long; zero resolves the first NotFound immediately"
    )
    assert bare.max_inflight_window >= dt.timedelta(hours=1), (
        "§13.5 resolves a NotFound to REJECTED only after this elapses; zero does it at once"
    )


def test_the_query_consistency_arm_that_lags_carries_its_bound() -> None:
    """A consistency mode without its bound would leave 4.4 unable to say when a `NotFound` became
    trustworthy — which is the whole reason §10.1 gives that arm a payload."""
    eventual = Eventual(visibility_bound=dt.timedelta(seconds=90))
    assert eventual.visibility_bound == dt.timedelta(seconds=90)
    assert dataclasses.fields(Linearizable) == ()


def test_an_unbounded_window_is_a_value_and_not_a_missing_one() -> None:
    """``None`` would be ambiguous between "never expires" and "not declared" — opposite readings,
    one the strongest possible retention and one that must degrade to the weakest."""
    forever = LedgerAdapterCapabilities(idempotency_window=UNBOUNDED)
    assert forever.idempotency_window is UNBOUNDED
    assert forever.idempotency_window != dt.timedelta(0)


def test_the_applied_count_can_report_a_second_application() -> None:
    """**The instrument, tested as an instrument.**

    Every duplicate-suppression claim in this project is checked by asserting
    ``applied_count(op) == 1``. That assertion is worth nothing unless the counter is capable of
    returning 2 — and the first version was not: it returned ``1 if op in self._applied else 0``,
    which cannot. A mutation restoring that form survived the entire unit suite, because with
    suppression working the two implementations agree on every input the suite produces.

    So the suppression is defeated here deliberately — the applied set is cleared between sends,
    which is precisely what a ledger that failed to suppress would look like from outside — and the
    counter must report the second application. A membership test reports 1 and fails this.
    """
    ledger = SimulatedLedger()

    async def apply_twice() -> None:
        await ledger.post(PROBE, INSTRUCTION)
        # What a non-suppressing ledger looks like: the identifier is no longer recognised.
        ledger._applied.clear()  # defeating suppression is the point of this test
        await ledger.post(PROBE, INSTRUCTION)

    asyncio.run(apply_twice())

    assert ledger.applied_count(PROBE) == 2, (
        "the applied-count cannot report a second application, so every 'applied-count == 1' "
        "assertion in this project is true by construction"
    )
    assert ledger.posts_received == 2


def test_the_applied_count_is_not_derived_from_the_applied_set() -> None:
    """The same property from the other side: the two must be able to disagree.

    Kept separate so the reason survives a refactor. If ``applied_count`` is ever re-derived from
    ``_applied``, this fails immediately rather than at the next review.
    """
    ledger = SimulatedLedger()
    asyncio.run(ledger.post(PROBE, INSTRUCTION))

    ledger._applied.clear()  # the count must survive the set being emptied

    assert ledger.applied("nothing-here") is None
    assert ledger.applied_count(PROBE) == 1, "the count is a memory of applications, not a lookup"


# ======================================================================================
# Declaration is not evidence
# ======================================================================================


class _UnprovenStrongAdapter:
    """A different implementation making both strong claims, with no conformance run behind it."""

    def __init__(self, name: str = "unproven") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> LedgerAdapterCapabilities:
        return LedgerAdapterCapabilities(
            idempotency=IdempotencyMode.ENFORCES_KEY,
            posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
        )

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        return Confirmed(posting_ref="pretend")

    async def get_by_operation_id(self, operation_id: str) -> QueryOutcome:
        return Found(posting_ref="pretend")


def test_an_unverified_declaration_is_downgraded_to_none() -> None:
    """**The plan's fourth required test, and it is behavioural rather than advisory.**

    §10.1: *"An undeclared or unverified capability is treated as `NONE`."* This adapter declares
    both strong capabilities and has no conformance record, so what a caller may act on is neither.
    """
    unverified = _UnprovenStrongAdapter()

    declared = unverified.capabilities()
    assert declared.idempotency is IdempotencyMode.ENFORCES_KEY
    assert declared.posting_identity_query is PostingQueryMode.BY_OPERATION_ID

    effective = capabilities_for(unverified)
    assert effective.idempotency is IdempotencyMode.NONE
    assert effective.posting_identity_query is PostingQueryMode.NONE
    assert effective.permits_effectively_once_claim is False


def test_an_adapter_cannot_inherit_another_adapters_evidence_by_taking_its_name() -> None:
    """**A regression test for a two-line forgery four reviewers found independently.**

    Verification was keyed on ``adapter.name`` — a string the adapter supplies. Declaring
    ``name = "simulated-ledger"`` therefore granted a strange class the reference adapter's proven
    ``ENFORCES_KEY`` and ``BY_OPERATION_ID``, and with them the effectively-once bar, on the
    strength of evidence gathered against something else entirely.

    Evidence is now matched on the implementation, which cannot be adopted by assignment.
    """
    forger = _UnprovenStrongAdapter(name="simulated-ledger")
    assert forger.name == SimulatedLedger().name

    effective = capabilities_for(forger)
    assert effective.idempotency is IdempotencyMode.NONE
    assert effective.posting_identity_query is PostingQueryMode.NONE
    assert effective.permits_effectively_once_claim is False

    assert capabilities_for(SimulatedLedger()).permits_effectively_once_claim is True


def test_the_reference_adapter_keeps_its_evidence_under_any_name() -> None:
    """The other direction: the name decides nothing at all, in either direction."""
    renamed = SimulatedLedger(name="whatever-it-calls-itself")
    assert capabilities_for(renamed).permits_effectively_once_claim is True


def test_a_partial_conformance_record_downgrades_only_what_it_failed_to_prove() -> None:
    """Each claim needs its own evidence. Proving one does not vouch for the other."""
    declared = LedgerAdapterCapabilities(
        idempotency=IdempotencyMode.ENFORCES_KEY,
        posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
    )

    only_suppression = effective_capabilities(
        declared, VerifiedCapabilities("a", suppression_proven=True, query_proven=False)
    )
    assert only_suppression.idempotency is IdempotencyMode.ENFORCES_KEY
    assert only_suppression.posting_identity_query is PostingQueryMode.NONE

    only_query = effective_capabilities(
        declared, VerifiedCapabilities("a", suppression_proven=False, query_proven=True)
    )
    assert only_query.idempotency is IdempotencyMode.NONE
    assert only_query.posting_identity_query is PostingQueryMode.BY_OPERATION_ID


def test_a_conformance_record_can_only_downgrade_never_upgrade() -> None:
    """**The asymmetry that makes a stale record safe.**

    Nothing in a record may make an adapter stronger than it declared itself to be, so an
    over-generous or out-of-date record cannot manufacture a capability out of nothing. The two
    failure directions are not equally bad, and this only moves in the harmless one.
    """
    weak = LedgerAdapterCapabilities(
        idempotency=IdempotencyMode.NONE, posting_identity_query=PostingQueryMode.NONE
    )
    generous = VerifiedCapabilities("a", suppression_proven=True, query_proven=True)

    upgraded = effective_capabilities(weak, generous)
    assert upgraded.idempotency is IdempotencyMode.NONE
    assert upgraded.posting_identity_query is PostingQueryMode.NONE


def test_a_weak_declaration_is_left_alone_by_a_missing_record() -> None:
    """The control for the downgrade: an adapter claiming nothing loses nothing."""
    weak = LedgerAdapterCapabilities(idempotency=IdempotencyMode.ACCEPTS_KEY)
    assert effective_capabilities(weak, None).idempotency is IdempotencyMode.ACCEPTS_KEY


def test_the_committed_record_names_the_reference_adapter_and_a_date() -> None:
    """§10.1: *"The conformance run and its date are recorded in the repository."*

    A committed constant rather than a table, because ``adapter_capability`` is named in two
    separate guard tests as a table that must not exist — the repository has already decided this
    record is reviewed data, not runtime state.
    """
    record = verified_for(SimulatedLedger())
    assert record is not None
    assert record.suppression_proven and record.query_proven

    (run,) = conformance_module.CONFORMANCE_RUNS
    assert run.implementation == conformance_module.implementation_of(SimulatedLedger())
    assert dt.date.fromisoformat(run.run_on) <= dt.date(2100, 1, 1), "the date must be a real date"


def test_an_adapter_with_no_committed_record_has_none() -> None:
    assert verified_for(_UnprovenStrongAdapter()) is None


@pytest.mark.asyncio
async def test_every_committed_conformance_record_is_backed_by_a_live_run() -> None:
    """**The record is not taken on trust**, which is what "declaration is not evidence" means
    applied to the evidence itself.

    A hand-written entry in :data:`CONFORMANCE_RUNS` would otherwise grant an adapter both strong
    claims with nothing behind it — a reviewer pointed out that the constant was the one link in the
    chain nobody checked. This runs the suite against every recorded implementation and requires the
    outcome to match what the record claims, so an entry that stopped being true fails the build.
    """
    implementations = {
        conformance_module.implementation_of(SimulatedLedger()): SimulatedLedger,
    }

    for run in conformance_module.CONFORMANCE_RUNS:
        factory = implementations.get(run.implementation)
        assert factory is not None, (
            f"{run.implementation} is recorded as conformant but this test cannot construct it; "
            "a record nothing can re-run is a record nobody is checking"
        )
        report = await run_conformance(factory(), probe_operation_id=PROBE)
        assert report.suppression_proven == run.suppression_proven, run.implementation
        assert report.query_proven == run.query_proven, run.implementation


# ======================================================================================
# The conformance suite itself — measured at the ledger, never inferred
# ======================================================================================


@pytest.mark.asyncio
async def test_conformance_proves_suppression_by_the_ledgers_own_applied_count() -> None:
    """**The plan's second required test.** Post the same identifier twice; applied-count is 1.

    Measured by asking the ledger, exactly as acceptance criteria 8 and 8a insist — *"verified by
    the simulated ledger's applied-count, not by our own records"*. Our own records would agree with
    themselves while the ledger held two postings, which is the failure this project exists to
    prevent.
    """
    ledger = SimulatedLedger()
    report = await run_conformance(ledger, probe_operation_id=PROBE)

    assert report.suppression_proven is True
    assert report.applied_after_two_posts == 1
    assert report.posts_received == 2, "both sends really did reach the ledger"


@pytest.mark.asyncio
async def test_conformance_proves_the_query_returns_a_known_posting() -> None:
    """**The plan's third required test.** Query a known posting; it comes back."""
    ledger = SimulatedLedger()
    report = await run_conformance(ledger, probe_operation_id=PROBE)

    assert report.query_proven is True
    assert isinstance(await ledger.get_by_operation_id(PROBE), Found)


@pytest.mark.asyncio
async def test_an_adapter_that_claims_suppression_and_does_not_have_it_fails_the_suite() -> None:
    """**The negative control, and the reason the suite is a gate rather than a formality.**

    This ledger declares ``ENFORCES_KEY`` and applies every post. The declaration is a lie, the
    applied-count says so, and the suite refuses to verify it — after which
    :func:`effective_capabilities` downgrades the claim to ``NONE`` and no caller may act on it.
    """

    class LyingLedger:
        name = "lying-ledger"

        def __init__(self) -> None:
            self.applied: list[str] = []

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities(idempotency=IdempotencyMode.ENFORCES_KEY)

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            self.applied.append(operation_id)
            return Confirmed(posting_ref=f"ref-{len(self.applied)}")

        def applied_count(self, operation_id: str) -> int:
            return self.applied.count(operation_id)

    lying = LyingLedger()
    report = await run_conformance(lying, probe_operation_id=PROBE)

    assert report.applied_after_two_posts == 2, "it really did apply the duplicate"
    assert report.suppression_proven is False

    downgraded = effective_capabilities(lying.capabilities(), report.as_verified())
    assert downgraded.idempotency is IdempotencyMode.NONE


@pytest.mark.asyncio
async def test_an_adapter_that_cannot_report_an_applied_count_is_never_verified() -> None:
    """A claim nobody can check is exactly what "declaration is not evidence" refuses.

    The correct outcome is *unverified*, not *assumed true* — which is the fail-safe direction and
    the only one that degrades rather than escalates.
    """

    class Opaque:
        name = "opaque"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities(idempotency=IdempotencyMode.ENFORCES_KEY)

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            return Confirmed(posting_ref="ref")

    report = await run_conformance(Opaque(), probe_operation_id=PROBE)
    assert report.suppression_proven is False


@pytest.mark.asyncio
async def test_an_adapter_declaring_a_query_it_structurally_lacks_is_never_verified() -> None:
    """Declared queryable, no method to query with. Unprovable, therefore unverified, therefore
    ``NONE`` — and the type system already said so."""

    class ClaimsQueryHasNone:
        name = "claims-query-has-none"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities(
                posting_identity_query=PostingQueryMode.BY_OPERATION_ID
            )

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            return Confirmed(posting_ref="ref")

    adapter = ClaimsQueryHasNone()
    report = await run_conformance(adapter, probe_operation_id=PROBE)

    assert report.query_proven is False
    assert not isinstance(adapter, QueryableLedgerAdapter)
    assert (
        effective_capabilities(adapter.capabilities(), report.as_verified()).posting_identity_query
        is PostingQueryMode.NONE
    )


@pytest.mark.asyncio
async def test_conformance_does_not_probe_a_capability_that_was_never_claimed() -> None:
    """A weak adapter is not posted to. The suite proves claims; it does not go looking for them."""
    weak = SimulatedLedger(name="weak", capabilities=LedgerAdapterCapabilities())
    report = await run_conformance(weak, probe_operation_id=PROBE)

    assert (report.suppression_proven, report.query_proven) == (False, False)
    assert weak.posts_received == 0, "nothing was sent to an adapter claiming nothing"


# ======================================================================================
# Suppression as behaviour, not as a declaration
# ======================================================================================


@pytest.mark.asyncio
async def test_the_reference_ledger_suppresses_a_repeated_identifier() -> None:
    """The behaviour ``ENFORCES_KEY`` names, exercised directly.

    Both sends are answered; one posting exists; the second returns the *first* reference — a
    provider that suppressed by answering with a different reference would have applied twice.
    """
    ledger = SimulatedLedger()
    operation = _operation("suppression")

    first = await ledger.post(operation, INSTRUCTION)
    second = await ledger.post(operation, INSTRUCTION)

    assert isinstance(first, Confirmed) and isinstance(second, Confirmed)
    assert first.posting_ref == second.posting_ref
    assert ledger.applied_count(operation) == 1
    assert ledger.posts_received == 2


@pytest.mark.asyncio
async def test_suppression_holds_even_when_the_adapter_is_told_to_fail() -> None:
    """**Suppression is checked before anything else can intervene**, which is what makes the
    lost-response scenario survivable: a second send of an already-applied identifier is a no-op at
    the ledger whatever the client subsequently believes."""
    ledger = SimulatedLedger(responder=lambda _op, _instruction: Unknown(detail="injected"))
    operation = _operation("suppression-under-fault")

    assert isinstance(await ledger.post(operation, INSTRUCTION), Unknown)
    assert ledger.applied_count(operation) == 0, "an injected Unknown applies nothing"

    ledger_that_applied = SimulatedLedger()
    await ledger_that_applied.post(operation, INSTRUCTION)
    assert ledger_that_applied.applied_count(operation) == 1


@pytest.mark.asyncio
async def test_two_different_identifiers_are_two_different_postings() -> None:
    """The control for suppression: a ledger that suppressed everything would pass the one
    above."""
    ledger = SimulatedLedger()

    await ledger.post(_operation("one"), INSTRUCTION)
    await ledger.post(_operation("two"), INSTRUCTION)

    assert ledger.applied_count(_operation("one")) == 1
    assert ledger.applied_count(_operation("two")) == 1
    assert isinstance(await ledger.get_by_operation_id(_operation("one")), Found)


@pytest.mark.asyncio
async def test_a_posting_that_was_never_made_is_not_found() -> None:
    """`NotFound`, and specifically not `Indeterminate`: this ledger declares ``LINEARIZABLE``, so
    its answer is current. An adapter declaring ``EVENTUAL`` would have to be able to say the
    third thing, which is why the union has three arms."""
    ledger = SimulatedLedger()
    assert isinstance(await ledger.get_by_operation_id(_operation("never-sent")), NotFound)


# ======================================================================================
# operation_id is the caller's, never the adapter's
# ======================================================================================


@pytest.mark.asyncio
async def test_the_adapter_posts_under_exactly_the_identifier_it_was_given() -> None:
    """§10.1: *"`operation_id` is supplied by the caller, never generated inside the adapter, so it
    cannot acquire a retry-dependent component."*"""
    seen: list[str] = []

    def record(operation_id: str, _instruction: PostingInstruction) -> None:
        seen.append(operation_id)
        return None

    ledger = SimulatedLedger(responder=record)
    operation = _operation("propagation")
    await ledger.post(operation, INSTRUCTION)

    assert seen == [operation]
    applied = ledger.applied(operation)
    assert applied is not None and applied.operation_id == operation


def test_the_posting_instruction_carries_no_identifier_field() -> None:
    """**The structural reason "supplied by the caller" is checkable.**

    §10.1 puts ``operation_id`` beside the instruction in ``post``'s signature rather than inside
    it. Folded in, an adapter could quietly replace it and nothing would read differently.
    """
    assert "operation_id" not in {field.name for field in dataclasses.fields(PostingInstruction)}


def test_the_posting_instruction_carries_no_derivation_metadata() -> None:
    """It is not :class:`~...money.AdjustmentInstruction`, and the difference is deliberate.

    ``quantum``, ``rounding`` and the ledger-context version describe how an amount was derived and
    are nobody's business outside the derivation. What crosses to an adapter is the posting.
    """
    fields = {field.name for field in dataclasses.fields(PostingInstruction)}
    assert fields == {"adjustment_id", "amount", "currency", "account_code", "period"}
    for derivation_only in ("quantum", "rounding", "ledger_context_version", "treatment"):
        assert derivation_only not in fields


@pytest.mark.asyncio
async def test_the_adapter_stores_the_amount_it_was_handed_unchanged() -> None:
    """M2.4 is the sole owner of what an amount is. Nothing at this boundary may adjust one."""
    ledger = SimulatedLedger()
    operation = _operation("amount")
    await ledger.post(operation, INSTRUCTION)

    applied = ledger.applied(operation)
    assert applied is not None
    assert applied.amount == INSTRUCTION.amount
    assert applied.amount.as_tuple() == INSTRUCTION.amount.as_tuple(), "not even re-spelled"
    assert (applied.currency, applied.account_code, applied.period) == (
        INSTRUCTION.currency,
        INSTRUCTION.account_code,
        INSTRUCTION.period,
    )


# ======================================================================================
# Capability is read, not inferred
# ======================================================================================


def test_capability_is_a_function_of_the_record_and_not_of_the_adapter_type() -> None:
    """**The plan's seventh required test.**

    §13.4: *"Capability is data the system reads and branches on, never an assumption baked into
    the dispatcher."* Two adapters of the *same class* with different declarations must produce
    different answers, and two of different classes with the same declaration must produce the same
    one — which is only true if nothing is keying on the type.
    """
    strong = SimulatedLedger(name="strong")
    weak = SimulatedLedger(name="weak", capabilities=LedgerAdapterCapabilities())

    assert type(strong) is type(weak)
    assert strong.capabilities().permits_effectively_once_claim is True
    assert weak.capabilities().permits_effectively_once_claim is False


def test_two_adapters_of_different_types_with_one_declaration_agree() -> None:
    """The other direction of the same property."""

    class OtherClass:
        name = "other"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities(idempotency=IdempotencyMode.ENFORCES_KEY)

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            return Unknown(detail="x")

    same = LedgerAdapterCapabilities(idempotency=IdempotencyMode.ENFORCES_KEY)
    assert OtherClass().capabilities() == same
    assert (
        SimulatedLedger(name="s", capabilities=same).capabilities().permits_effectively_once_claim
        is OtherClass().capabilities().permits_effectively_once_claim
    )


def test_the_reserved_reversal_capability_is_read_by_no_module() -> None:
    """§10.1: *"`reversal` is RESERVED and consumed by nothing today. No dispatcher branch … reads
    it."*

    Declared so the record is complete and so OPEN-12 has somewhere to land — not so anything can
    act on it. Walked as syntax over the whole package, because a branch on it would be a claim
    that the system can correct a wrongly applied posting, which nothing has decided.
    """
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"
    readers: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "reversal":
                readers.append(f"{path.name}:{node.lineno}")

    assert readers == [], f"reversal is read at {readers}; it is RESERVED (§10.1, OPEN-12)"


# ======================================================================================
# A posting reference that cannot be recorded is not a posting reference
# ======================================================================================


def test_the_reference_width_matches_the_column_that_stores_it() -> None:
    """The constant and the column are asserted equal, so they cannot drift apart quietly.

    A hand-copied width is the kind of thing that stays right until somebody widens the column and
    the validation silently becomes stricter than the database — or, far worse, narrows it and the
    validation silently becomes weaker than the database, which puts the failure back after the
    send where it started.
    """
    for entity in (Adjustment, PostingAttempt):
        column_type = entity.__table__.columns["posting_ref"].type
        assert isinstance(column_type, String), "posting_ref is no longer a bounded string column"
        assert column_type.length == MAX_POSTING_REF


@pytest.mark.parametrize("unusable", ["", "   ", "\t"])
def test_a_confirmed_without_a_reference_is_refused(unusable: str) -> None:
    """**An empty reference stores cleanly and then looks exactly like evidence of a posting.**

    That is the failure mode worth refusing: it is not an error anywhere, it is a row that answers
    "where was this applied" with nothing while satisfying every constraint on the table.
    """
    with pytest.raises(ValueError, match="empty one is not evidence"):
        Confirmed(posting_ref=unusable)


def test_a_reference_too_wide_for_the_column_is_refused_before_the_send() -> None:
    """**And this one is refused because of *when* it would otherwise fail.**

    A 400-character reference raises a driver error in the transaction that records the outcome —
    after the ledger has applied an irreversible write. The attempt is left ``IN_FLIGHT``, the
    reference is lost, and a human has to reconstruct which posting it was. Refusing at
    construction moves the same failure to before the socket write, where the write-ahead record's
    ordinary ``UNKNOWN`` semantics already cover it.
    """
    with pytest.raises(ValueError, match="refusing before the send"):
        Confirmed(posting_ref="X" * (MAX_POSTING_REF + 1))

    Confirmed(posting_ref="X" * MAX_POSTING_REF)


def test_the_widest_admissible_reference_is_exactly_the_column_width() -> None:
    """The boundary, both sides, because an off-by-one here is invisible until it is not."""
    assert Confirmed(posting_ref="X" * MAX_POSTING_REF).posting_ref
    with pytest.raises(ValueError):
        Confirmed(posting_ref="X" * (MAX_POSTING_REF + 1))


def test_a_partially_applied_outcome_validates_every_reference_it_carries() -> None:
    """§14 routes these to manual recovery **by reference**, so an unusable one breaks the route."""
    PartiallyApplied(applied_legs=1, posting_refs=("SIM-1",))

    with pytest.raises(ValueError, match="unusable posting reference"):
        PartiallyApplied(applied_legs=1, posting_refs=("SIM-1", ""))
    with pytest.raises(ValueError, match="unusable posting reference"):
        PartiallyApplied(applied_legs=1, posting_refs=("X" * (MAX_POSTING_REF + 1),))
    with pytest.raises(ValueError, match="applied_legs cannot be negative"):
        PartiallyApplied(applied_legs=-1, posting_refs=())


def test_a_found_query_result_carries_a_usable_reference() -> None:
    """§13.5 acts on a positive hit, so a hit with nothing in it is not a positive hit."""
    Found(posting_ref="SIM-1")
    with pytest.raises(ValueError, match="positive hit"):
        Found(posting_ref="")


def test_the_reference_the_simulated_ledger_produces_fits_the_column() -> None:
    """The control: the validation must not be stricter than the reference adapter it ships with."""
    reference = Confirmed(posting_ref=f"SIM-{'a' * 16}").posting_ref
    assert 0 < len(reference) <= MAX_POSTING_REF


# ======================================================================================
# §13.5's capability branch, as a pure function of the effective capability record
# ======================================================================================


def test_a_proven_enforces_key_permits_an_automatic_resend() -> None:
    """§13.5: *"Automatic retry from `UNKNOWN` is permitted only where capability allows the
    duplicate to be suppressed or detected."* Suppression is the branch that permits a send."""
    permitted = LedgerAdapterCapabilities(idempotency=IdempotencyMode.ENFORCES_KEY)
    assert resend_decision(permitted) is ResendDecision.PERMITTED


def test_a_query_capability_without_suppression_reconciles_instead_of_sending() -> None:
    """**The middle branch, and the only place it can be exercised honestly.**

    §13.5's table answers this row by *querying*, not by sending: "no re-send; reconcile by querying
    X". Reaching it through a live adapter would need one whose conformance run proved the query and
    not the suppression, and this repository ships no such adapter — so it is exercised here, where
    the input is the capability record itself and nothing has to be faked to produce it.
    """
    queryable = LedgerAdapterCapabilities(posting_identity_query=PostingQueryMode.BY_OPERATION_ID)
    assert resend_decision(queryable) is ResendDecision.RECONCILE_FIRST


def test_neither_capability_routes_to_manual_recovery() -> None:
    """*"Otherwise route to manual recovery … the automatic path stops."*"""
    assert resend_decision(LedgerAdapterCapabilities()) is ResendDecision.MANUAL_RECOVERY


def test_accepting_a_key_is_not_enforcing_one() -> None:
    """**The distinction the whole capability table exists to preserve.**

    §13.4: a provider that accepts an idempotency header and may ignore it is indistinguishable
    from one that has none. Treating ``ACCEPTS_KEY`` as permission to re-send would authorise a
    duplicate financial write on the strength of a header nobody promised to honour.
    """
    accepts = LedgerAdapterCapabilities(idempotency=IdempotencyMode.ACCEPTS_KEY)
    assert accepts.suppresses_duplicates is False
    assert resend_decision(accepts) is ResendDecision.MANUAL_RECOVERY


def test_suppression_outranks_the_query_when_an_adapter_has_both() -> None:
    """Both capabilities is the reference adapter's case, and a send is cheaper than a query."""
    both = LedgerAdapterCapabilities(
        idempotency=IdempotencyMode.ENFORCES_KEY,
        posting_identity_query=PostingQueryMode.BY_OPERATION_ID,
    )
    assert resend_decision(both) is ResendDecision.PERMITTED


def test_the_branch_reads_the_record_and_never_the_adapter() -> None:
    """§13.4: *"Capability is data the system reads and branches on, never an assumption baked into
    the dispatcher."* The signature is the proof: there is nowhere to pass an adapter."""
    parameters = inspect.signature(resend_decision).parameters
    assert list(parameters) == ["capabilities"]
    annotation = parameters["capabilities"].annotation
    assert annotation in (LedgerAdapterCapabilities, "LedgerAdapterCapabilities")


# ======================================================================================
# The attribution proxy must be transparent to the conformance record — and only it
# ======================================================================================


def test_wrapping_an_adapter_for_attribution_preserves_its_proven_capabilities() -> None:
    """**A regression for a defect the fix for another defect introduced.**

    4.3 dispatches through :class:`AttributedAdapter`, whose only job is to label the exceptions the
    adapter raises so a *database* failure cannot be mistaken for a *ledger* one. Because evidence
    is keyed on the implementation class, the first version of that wrapper produced an
    unrecognised class — so the reference ledger's proven ``ENFORCES_KEY`` and ``BY_OPERATION_ID``
    were both downgraded to ``NONE``, and a re-send §13.5 explicitly permits was refused.

    A silent withdrawal of the exact claim the conformance suite exists to establish, caught only
    because one integration test asserted the *permitted* branch rather than the refusal.
    """
    from ledger_exception_control_plane.ledger.transport import AttributedAdapter

    direct = capabilities_for(SimulatedLedger())
    wrapped = capabilities_for(AttributedAdapter(SimulatedLedger()))

    assert wrapped == direct
    assert wrapped.permits_effectively_once_claim is True
    assert wrapped.suppresses_duplicates is True


def test_wrapping_an_adapter_preserves_its_declared_endpoint_and_its_absence() -> None:
    """**The same defect as above, in a new place, found the same way — by a test, not a review.**

    4.4 bounds a re-send by the endpoint the *original* send recorded, and the dispatcher records
    ``declared_endpoint(adapter)`` on the write-ahead attempt row. The proxy forwarded ``name`` and
    ``capabilities`` and swallowed this one, so every send made through it recorded no endpoint at
    all — and the bound then refused a re-send this adapter's *verified* ``ENFORCES_KEY`` permits.
    A silent withdrawal of a permitted branch, exactly as before, surfaced by a replay test failing.

    **Both directions matter, and the second is the one an implementation gets wrong.** Forwarding
    a declared endpoint is the obvious half. Preserving its *absence* is not: a property that raised
    ``AttributeError`` looked correct and was not, because Python 3.12 resolves runtime protocol
    checks with ``inspect.getattr_static`` — so the wrapper satisfied
    :class:`EndpointDeclaringAdapter` while being unable to answer, which is a worse state than
    either honest one.
    """
    from ledger_exception_control_plane.ledger.port import (
        EndpointDeclaringAdapter,
        declared_endpoint,
    )
    from ledger_exception_control_plane.ledger.transport import AttributedAdapter

    class _DeclaresNoEndpoint:
        name = "silent"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(
            self, operation_id: str, instruction: PostingInstruction
        ) -> PostingOutcome:  # pragma: no cover - never called
            return Confirmed(posting_ref="unused")

    inner = SimulatedLedger(endpoint="sim://somewhere/postings")
    wrapped = AttributedAdapter(inner)
    assert declared_endpoint(wrapped) == "sim://somewhere/postings" == declared_endpoint(inner)
    assert isinstance(wrapped, EndpointDeclaringAdapter)

    silent = AttributedAdapter(_DeclaresNoEndpoint())
    assert declared_endpoint(silent) is None
    assert not isinstance(silent, EndpointDeclaringAdapter), (
        "the wrapper must be invisible to the question, not merely usually invisible"
    )
    assert not hasattr(silent, "endpoint")


def test_only_that_exact_wrapper_is_unwrapped_and_a_subclass_is_not() -> None:
    """**The unwrapping is an exact type check, and this is why.**

    ``isinstance`` would reopen the forgery the implementation keying closed: a subclass could
    override ``post``, stop delegating to the adapter it holds, and still inherit that adapter's
    conformance record. The exact check makes the delegation and the evidence inseparable — you
    cannot have the second without the first.
    """
    from ledger_exception_control_plane.ledger.transport import AttributedAdapter

    class _PretendsToDelegate(AttributedAdapter):
        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            return Confirmed(posting_ref="never-went-anywhere")

    forger = _PretendsToDelegate(SimulatedLedger())
    effective = capabilities_for(forger)

    assert effective.idempotency is IdempotencyMode.NONE
    assert effective.posting_identity_query is PostingQueryMode.NONE
    assert effective.permits_effectively_once_claim is False


def test_an_unrelated_object_with_a_wrapped_attribute_inherits_nothing() -> None:
    """A plain attribute named ``wrapped`` is not a claim on anything."""
    from ledger_exception_control_plane.ledger.transport import AttributedAdapter

    class _LooksLikeAWrapper:
        name = "looks-like-a-wrapper"

        def __init__(self) -> None:
            self.wrapped = SimulatedLedger()

        def capabilities(self) -> LedgerAdapterCapabilities:
            return self.wrapped.capabilities()

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            return Confirmed(posting_ref="pretend")

    assert capabilities_for(_LooksLikeAWrapper()).permits_effectively_once_claim is False
    assert capabilities_for(AttributedAdapter(SimulatedLedger())).permits_effectively_once_claim


@pytest.mark.asyncio
async def test_the_proxy_labels_only_the_adapters_own_failure() -> None:
    """It forwards a normal answer untouched and labels a raise. Nothing else changes."""
    from ledger_exception_control_plane.ledger.transport import (
        AdapterCallError,
        AttributedAdapter,
    )

    ledger = SimulatedLedger()
    proxy = AttributedAdapter(ledger)

    outcome = await proxy.post(PROBE, INSTRUCTION)
    assert isinstance(outcome, Confirmed)
    assert ledger.applied_count(PROBE) == 1
    assert proxy.name == ledger.name

    class _Raises:
        name = "raises"

        def capabilities(self) -> LedgerAdapterCapabilities:
            return LedgerAdapterCapabilities()

        async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
            raise ConnectionRefusedError(111, "refused")

    with pytest.raises(AdapterCallError) as raised:
        await AttributedAdapter(_Raises()).post(PROBE, INSTRUCTION)

    assert isinstance(raised.value.error, ConnectionRefusedError)
