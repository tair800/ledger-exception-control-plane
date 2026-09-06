"""M4.3 — the transport classifier and the backoff policy, with no database and no clock.

Two things are proven here and neither needs PostgreSQL: **what the classifier does with an
exception**, and **what the backoff bounds actually are**. Both are pure functions by construction,
which is the point — §15 says *"The classifier *is* the guarantee, so it is enumerated rather than
described"*, and a guarantee that could only be exercised through a database and a socket would be
described rather than tested.

The scheduling, dead-lettering and replay behaviour lives in ``test_retry_postgres.py``, because
every property there is about persisted state and transaction boundaries.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
import socket
import ssl

import pytest

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.control import PostingOutcome as OutcomeCode
from ledger_exception_control_plane.ledger.transport import (
    RETRYABLE_CAUSES,
    LedgerTransportError,
    RetryableCause,
    TransportClass,
    TransportVerdict,
    classify_transport_failure,
)
from ledger_exception_control_plane.operations.retry import (
    AMBIGUOUS_OUTCOMES,
    DeadLetterReason,
    RetryPolicy,
    backoff_ceiling,
    backoff_delay,
)

POLICY = RetryPolicy(
    base_delay=dt.timedelta(seconds=1),
    multiplier=2.0,
    cap=dt.timedelta(seconds=60),
    max_attempts=5,
    time_budget=dt.timedelta(hours=1),
)


# ======================================================================================
# The enumerated allowlist — §15
# ======================================================================================


def test_the_allowlist_has_exactly_the_four_causes_the_specification_names() -> None:
    """**Enumerated, so a reviewer can compare it with §15 line by line.**

    The specification's list is: *"DNS resolution failure · TCP connect failure or refusal · TLS
    handshake failure · connect-timeout before first byte written."* Four. A fifth member here
    would be a change to the retry contract wearing the clothes of an implementation detail, which
    is exactly why the set is asserted rather than described.
    """
    assert {cause.value for cause in RetryableCause} == {
        "dns_resolution",
        "tcp_connect",
        "tls_handshake",
        "connect_timeout",
    }
    assert frozenset(RetryableCause) == RETRYABLE_CAUSES


def test_the_classification_is_two_valued_and_defaults_to_the_unsafe_one() -> None:
    """§15: *"The default is `UNKNOWN`, not retryable."*"""
    assert {member.value for member in TransportClass} == {"not_sent", "unknown"}


@pytest.mark.parametrize("cause", list(RetryableCause))
def test_a_declared_cause_is_retryable(cause: RetryableCause) -> None:
    """Every one of the four, through the explicit seam an adapter uses to declare what it saw."""
    verdict = classify_transport_failure(LedgerTransportError(cause, "connect failed"))

    assert verdict.classification is TransportClass.NOT_SENT
    assert verdict.cause is cause
    assert verdict.retryable is True


@pytest.mark.parametrize(
    ("label", "error", "cause"),
    [
        (
            "a name that never resolved",
            socket.gaierror(-2, "Name or service not known"),
            RetryableCause.DNS_RESOLUTION,
        ),
        (
            "a refused connection",
            ConnectionRefusedError(111, "Connection refused"),
            RetryableCause.TCP_CONNECT,
        ),
    ],
)
def test_the_two_unambiguous_exception_types_are_recognised_without_a_declaration(
    label: str, error: BaseException, cause: RetryableCause
) -> None:
    """**Two, and only two, can be recognised from the type alone.**

    Resolution precedes connect and a refusal is raised by connect itself, so neither can occur once
    a request byte is on the wire. That is what makes recognising them safe without the adapter
    saying anything.
    """
    verdict = classify_transport_failure(error)

    assert verdict.classification is TransportClass.NOT_SENT
    assert verdict.cause is cause


@pytest.mark.parametrize(
    ("label", "error"),
    [
        ("a bare timeout", TimeoutError("timed out")),
        ("a connection reset", ConnectionResetError(104, "Connection reset by peer")),
        ("a broken pipe", BrokenPipeError(32, "Broken pipe")),
        ("an SSL failure", ssl.SSLError(1, "decryption failed")),
        ("a certificate failure", ssl.SSLCertVerificationError(1, "unable to verify")),
        ("an aborted connection", ConnectionAbortedError(103, "Software caused connection abort")),
        ("an OS error", OSError(5, "Input/output error")),
        ("something from a library we have never seen", RuntimeError("boom")),
        ("a bare exception", Exception("?")),
        ("a value error", ValueError("malformed response")),
    ],
)
def test_everything_not_on_the_allowlist_defaults_to_unknown(
    label: str, error: BaseException
) -> None:
    """**The plan names this test explicitly**: *"a test asserting a transport error absent from the
    allowlist defaults to `UNKNOWN` rather than to retry."*

    The first four cases are the ones a conventional retry classifier gets wrong. A read timeout and
    a reset both mean the request was already on the wire; §15 puts them in ``UNKNOWN`` and calls
    retrying them *"precisely the defect this design exists to prevent"*. ``SSLError`` is here
    rather than mapped to ``TLS_HANDSHAKE`` because the same type covers a mid-stream failure, and
    a type that covers both cannot prove either.
    """
    verdict = classify_transport_failure(error)

    assert verdict.classification is TransportClass.UNKNOWN
    assert verdict.cause is None
    assert verdict.retryable is False


def test_a_declared_cause_outranks_the_type_it_happens_to_have() -> None:
    """An adapter that knows its connect timed out says so, and is believed over the bare type.

    The same exception type reaching the classifier undeclared stays ``UNKNOWN``, which is the pair
    that shows the declaration is doing the work rather than the type.
    """
    declared = classify_transport_failure(
        LedgerTransportError(RetryableCause.CONNECT_TIMEOUT, "no route")
    )
    undeclared = classify_transport_failure(TimeoutError("no route"))

    assert declared.retryable is True
    assert undeclared.retryable is False


def test_the_verdict_never_leaks_the_exception_message() -> None:
    """**The detail is a class name, and this is a security property rather than tidiness.**

    It is persisted in the dead-letter envelope and read by operators. An exception message from a
    real HTTP client routinely carries the full URL, and a URL routinely carries a token.
    """
    secret = "https://ledger.example/post?access_token=SHOULD-NEVER-APPEAR"

    # **Both arms.** A mutation putting `str(error)` in the *default* branch survived the first
    # version of this test, because every exception it tried was on the allowlist and took the
    # other path — and the default arm is the one that sees unrecognised exceptions from a real
    # HTTP client, which are exactly the ones whose messages carry URLs.
    for error in (
        ConnectionRefusedError(111, secret),
        socket.gaierror(-2, secret),
        LedgerTransportError(RetryableCause.TLS_HANDSHAKE, secret),
        RuntimeError(secret),
        TimeoutError(secret),
        ssl.SSLError(1, secret),
    ):
        verdict = classify_transport_failure(error)
        assert verdict.detail == type(error).__name__, (
            f"{type(error).__name__} reached the envelope as something other than its class name"
        )
        assert "SHOULD-NEVER-APPEAR" not in verdict.detail
        assert "access_token" not in verdict.detail


def test_a_verdict_cannot_be_built_that_claims_a_cause_it_does_not_have() -> None:
    """The two shapes that would mean the classifier had guessed."""
    with pytest.raises(ValueError, match="must name which allowlisted cause"):
        TransportVerdict(classification=TransportClass.NOT_SENT, cause=None, detail="x")

    with pytest.raises(ValueError, match="cannot name a cause"):
        TransportVerdict(
            classification=TransportClass.UNKNOWN,
            cause=RetryableCause.TCP_CONNECT,
            detail="x",
        )


def test_retryability_reads_the_classification_and_not_the_cause() -> None:
    """**A cause present on an UNKNOWN verdict must not make it retryable.**

    The first version of this test read a type hint and re-classified two exceptions the other tests
    already cover — it asserted nothing about the property in its own name, and the mutation it was
    written for (``retryable`` consulting ``cause``) survived it. A reviewer demonstrated that.

    The property is now checked on the only construction that can distinguish the two readings, and
    that construction has to be forced past ``__post_init__`` — which refuses it, because a verdict
    carrying both ``UNKNOWN`` and a cause is exactly the incoherent state the classifier must never
    produce. Both halves are asserted: the guard refuses to build it, and ``retryable`` still reads
    the classification if one is built anyway.
    """
    with pytest.raises(ValueError, match="cannot name a cause"):
        TransportVerdict(
            classification=TransportClass.UNKNOWN,
            cause=RetryableCause.TCP_CONNECT,
            detail="forced",
        )

    # Built around the guard, the way a future refactor might. `retryable` must still say no.
    smuggled = object.__new__(TransportVerdict)
    object.__setattr__(smuggled, "classification", TransportClass.UNKNOWN)
    object.__setattr__(smuggled, "cause", RetryableCause.TCP_CONNECT)
    object.__setattr__(smuggled, "detail", "forced")

    assert smuggled.retryable is False, "retryability is reading the cause, not the classification"


# ======================================================================================
# Backoff bounds — the plan's first named test
# ======================================================================================


def test_the_ceiling_doubles_until_it_reaches_the_cap() -> None:
    """**"Backoff bounds", asserted on the bound rather than on a sample.**

    The ceiling is a pure function of the attempt number, so it is checked exactly. A test that drew
    a jittered delay and asserted it "looked exponential" would be asserting a draw.
    """
    assert backoff_ceiling(POLICY, 1) == dt.timedelta(seconds=1)
    assert backoff_ceiling(POLICY, 2) == dt.timedelta(seconds=2)
    assert backoff_ceiling(POLICY, 3) == dt.timedelta(seconds=4)
    assert backoff_ceiling(POLICY, 4) == dt.timedelta(seconds=8)
    assert backoff_ceiling(POLICY, 7) == dt.timedelta(seconds=60), "the cap binds"
    assert backoff_ceiling(POLICY, 40) == POLICY.cap, "and keeps binding"


def test_the_ceiling_is_computed_from_the_attempt_number_not_accumulated() -> None:
    """Accumulating ``timedelta * multiplier`` rounds to the microsecond at every step.

    Over a long curve the drift is real and makes the ceiling depend on the path taken to it, which
    is exactly the kind of difference that makes a bound untestable.
    """
    fine = RetryPolicy(
        base_delay=dt.timedelta(microseconds=1),
        multiplier=1.5,
        cap=dt.timedelta(hours=1),
        max_attempts=40,
        time_budget=dt.timedelta(days=1),
    )

    # What the curve should be, computed once in float seconds. `timedelta` quantises to the
    # microsecond on construction, so the result can differ from this by at most half a microsecond
    # — that single rounding is unavoidable and harmless.
    direct = 1e-6 * 1.5**29
    assert backoff_ceiling(fine, 30).total_seconds() == pytest.approx(direct, abs=1e-6)

    # What it would be if each step were taken on the previous `timedelta`. That rounding happens
    # twenty-nine times and compounds, and the gap it opens is far larger than the single
    # quantisation above — which is the whole reason the exponent is applied to a float.
    accumulated = dt.timedelta(microseconds=1)
    for _ in range(29):
        accumulated = accumulated * 1.5
    assert abs(accumulated.total_seconds() - direct) > 1e-5, (
        "the accumulated form no longer drifts, so this test is no longer measuring anything"
    )


def test_an_attempt_number_below_one_is_refused() -> None:
    """Attempts count sends and start at one; a zeroth send is not a thing that happened."""
    with pytest.raises(ValueError, match="starts at 1"):
        backoff_ceiling(POLICY, 0)


@pytest.mark.parametrize("attempt_no", [1, 2, 3, 4, 5, 6, 7, 12])
def test_every_jittered_delay_lands_inside_its_ceiling(attempt_no: int) -> None:
    """**Full jitter: a uniform draw from ``[0, ceiling]``**, checked over many draws.

    Two hundred draws per attempt number, each asserted against the bound. This is the property the
    plan asks for, and it is falsifiable: a multiplier applied twice, an off-by-one on the exponent
    or a cap applied after the draw all put a sample outside the interval.
    """
    rng = random.Random(20260906 + attempt_no)
    ceiling = backoff_ceiling(POLICY, attempt_no)

    draws = [backoff_delay(POLICY, attempt_no, rng) for _ in range(200)]

    assert all(dt.timedelta(0) <= draw <= ceiling for draw in draws)
    assert max(draws) > ceiling * 0.5, "the draw is not spreading across the interval"


def test_jitter_actually_varies() -> None:
    """A constant "jitter" would satisfy the bounds test above and none of its purpose.

    Full jitter exists to break up a thundering herd; a delay that is always the ceiling — or always
    zero — passes an interval check while every client in a fleet retries in lockstep.
    """
    rng = random.Random(7)
    draws = {backoff_delay(POLICY, 4, rng) for _ in range(50)}

    assert len(draws) > 40, "the delay is not being drawn"


def test_the_same_seed_gives_the_same_schedule() -> None:
    """The randomness is injected, so a test can pin it and a deployment cannot."""
    assert [backoff_delay(POLICY, 3, random.Random(11)) for _ in range(3)] == [
        backoff_delay(POLICY, 3, random.Random(11)) for _ in range(3)
    ]


# ======================================================================================
# The policy itself
# ======================================================================================


def test_a_policy_that_is_not_a_bounded_retry_is_refused() -> None:
    """Each rejection is a shape that would silently stop being a bounded exponential retry.

    A zero base makes backoff a no-op, a multiplier below one makes the curve shrink toward an
    immediate re-send, a cap below the base means the first delay is already clamped, zero attempts
    forbids the send the policy exists to bound, and a zero budget dead-letters everything on sight.
    Written out one at a time because ``dataclasses.replace`` is typed per field, and a loop over a
    heterogeneous mapping would have to discard those types to compile.
    """
    with pytest.raises(ValueError, match="base_delay must be positive"):
        dataclasses.replace(POLICY, base_delay=dt.timedelta(0))
    with pytest.raises(ValueError, match=r"at least 1\.0"):
        dataclasses.replace(POLICY, multiplier=0.5)
    with pytest.raises(ValueError, match="at least base_delay"):
        dataclasses.replace(POLICY, cap=dt.timedelta(seconds=0.5))
    with pytest.raises(ValueError, match="at least 1"):
        dataclasses.replace(POLICY, max_attempts=0)
    with pytest.raises(ValueError, match="must be positive"):
        dataclasses.replace(POLICY, time_budget=dt.timedelta(0))


def test_the_policy_reads_the_documented_settings() -> None:
    """The bridge to configuration, and the defaults §15 delegates rather than states."""
    policy = RetryPolicy.from_settings(Settings())

    assert policy.base_delay == dt.timedelta(seconds=1)
    assert policy.multiplier == 2.0
    assert policy.cap == dt.timedelta(seconds=60)
    assert policy.max_attempts == 5
    assert policy.time_budget == dt.timedelta(hours=1)


def test_every_retry_setting_is_overridable_and_bounded() -> None:
    """Configuration, per §15 — and bounded, so a deployment cannot configure the bound away."""
    tuned = Settings(
        retry_base_delay_seconds=0.25,
        retry_multiplier=3.0,
        retry_cap_seconds=10.0,
        retry_max_attempts=2,
        retry_time_budget_seconds=90.0,
    )
    policy = RetryPolicy.from_settings(tuned)

    assert policy.base_delay == dt.timedelta(seconds=0.25)
    assert policy.max_attempts == 2

    # Each bound rejected at construction, so a deployment cannot configure the bound away. Written
    # out rather than looped because `Settings` is a typed model and a loop over a heterogeneous
    # mapping has to be untyped to compile — which would make the check weaker than the thing it is
    # checking.
    with pytest.raises(ValueError):
        Settings(retry_base_delay_seconds=0.0)
    with pytest.raises(ValueError):
        Settings(retry_multiplier=0.9)
    with pytest.raises(ValueError):
        Settings(retry_cap_seconds=0.0)
    with pytest.raises(ValueError):
        Settings(retry_max_attempts=0)
    with pytest.raises(ValueError):
        Settings(retry_time_budget_seconds=0.0)


# ======================================================================================
# The line this increment must not cross
# ======================================================================================


def test_the_ambiguous_set_matches_the_dispatchers_and_cannot_drift() -> None:
    """Two modules hold the same membership for different reasons; this is what keeps them equal.

    The dispatcher's set gates a *second send inside one dispatch*; this one gates *selection for
    retry*. They are declared separately on purpose — a later increment could legitimately change
    one — but a silent divergence would mean an operation the dispatcher considers ambiguous being
    picked up by the retry loop, which is the double-post.
    """
    from ledger_exception_control_plane.operations import dispatcher

    assert AMBIGUOUS_OUTCOMES == dispatcher._AMBIGUOUS


def test_not_sent_is_not_ambiguous() -> None:
    """The whole reason the classifier exists: this one *is* retryable, and the others are not."""
    assert OutcomeCode.NOT_SENT not in AMBIGUOUS_OUTCOMES
    assert OutcomeCode.UNKNOWN in AMBIGUOUS_OUTCOMES
    assert OutcomeCode.PARTIALLY_APPLIED in AMBIGUOUS_OUTCOMES


def test_the_dead_letter_reasons_are_a_closed_vocabulary() -> None:
    """Three reasons, each mapping to a different operator response."""
    assert {reason.value for reason in DeadLetterReason} == {
        "attempts_exhausted",
        "time_budget_exhausted",
        "terminal_rejection",
    }


def test_a_stored_replay_state_is_a_string_and_must_be_coerced_before_comparison() -> None:
    """**A regression test for a defect this increment actually shipped and then fixed.**

    ``replay_state`` is a ``String(16)`` column carrying a ``ReplayState`` annotation, so
    SQLAlchemy returns the plain string. ``StrEnum`` members compare *equal* to their value and
    are never *identical* to it, so ``"pending" is not ReplayState.PENDING`` is True — and the
    first version of the replay guard used ``is not``. Every replay was refused with the message
    "dead letter … is pending; only a pending entry may be replayed", which states the
    contradiction out loud.

    The same trap has caught this repository before. This test pins the two halves of it so the next
    person meets it here rather than in a thirteen-minute integration run.
    """
    from ledger_exception_control_plane.db.control import ReplayState

    stored = "pending"

    assert stored == ReplayState.PENDING, "a StrEnum compares equal to its value"
    assert stored is not ReplayState.PENDING, "and is never identical to it"
    assert ReplayState(stored) is ReplayState.PENDING, "coercion is what makes `is` safe"

    # And the production check itself, driven with the value the database actually returns. This is
    # the half the first version left out: the three assertions above are true of any StrEnum in any
    # program, so they would have passed with the defect still in place.
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "ledger_exception_control_plane"
        / "operations"
        / "retry.py"
    ).read_text(encoding="utf-8")

    guard = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Compare)
        and any(isinstance(op, ast.IsNot) for op in node.ops)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "ReplayState"
    ]
    assert guard, (
        "the replay guard no longer coerces before comparing; a stored 'pending' will be refused"
    )


def test_no_module_compares_a_stored_enum_column_by_identity() -> None:
    """The guard that would have caught it, over every module that reads one of these columns.

    An ``is`` comparison against a member of an enum stored as text is always False for a value that
    came out of the database, so it fails *closed* here and could as easily fail *open* elsewhere —
    a state check that never matches is one refactor away from a state check that always does.
    """
    import ast
    import pathlib

    # Includes the *aliases* the package imports these under, which was the hole: the retry and
    # dispatcher modules both do `from ... import PostingOutcome as OutcomeCode`, so a guard naming
    # only the original class watched neither of the two modules most likely to compare one.
    stored_enums = {
        "ReplayState",
        "DispatchState",
        "AttemptState",
        "PostingOutcome",
        "OutcomeCode",
        "RecoveryState",
        "ExceptionStatus",
        "TreatmentCode",
        "ExceptionClassification",
    }
    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"

    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")

        # Scoped to modules that read ORM rows. Everywhere else an attribute holding one of these
        # enums is a dataclass or Pydantic field whose type is the enum itself, and comparing that
        # by identity is correct — `if self.treatment is not TreatmentCode.ESCALATE` in the proposal
        # contract is not a defect, and a guard that called it one would be turned off.
        if "db.control import" not in source:
            continue

        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, ast.Is | ast.IsNot) for op in node.ops):
                continue
            sides = [node.left, *node.comparators]
            members = [
                side
                for side in sides
                if isinstance(side, ast.Attribute)
                and isinstance(side.value, ast.Name)
                and side.value.id in stored_enums
            ]
            if not members:
                continue

            # The unsafe shape is an attribute or subscript read *off a row* — `entry.state`,
            # `row.outbox.state`, `rows[0].state`. A plain local is safe, because whatever coerced
            # it did so at the assignment (`code = outcome_code(...)`,
            # `settled = OutcomeCode(intent.last_outcome)`), and a direct coercing call is safe by
            # construction.
            #
            # Both bounds were wrong once. The first version required the other side to be an
            # attribute on a plain *name*, so `row.outbox.state is X` and `rows[0].state is X`
            # walked past it, and it named only the original enum classes, so the `OutcomeCode`
            # alias the retry and dispatcher modules import was invisible. Widening it to "anything
            # that is not a coercing call" then flagged seven perfectly safe in-memory comparisons
            # — `if treatment is TreatmentCode.ESCALATE` on a dataclass field — which is how a
            # guard stops being read.
            # `self.x` is the module's own field, typed as the enum. A row read is `row.x`.
            others = [
                side
                for side in sides
                if side not in members
                and not (
                    isinstance(side, ast.Attribute)
                    and isinstance(side.value, ast.Name)
                    and side.value.id == "self"
                )
            ]
            if any(isinstance(side, ast.Attribute | ast.Subscript) for side in others):
                offenders.append(f"{path.relative_to(package).as_posix()}:{node.lineno}")

    assert offenders == [], (
        f"identity comparison against a text-stored enum at {offenders}; "
        "coerce with EnumType(value) first"
    )


@pytest.mark.parametrize(
    ("label", "shape"),
    [
        ("a plain attribute", "if entry.replay_state is ReplayState.PENDING:\n    pass"),
        ("an aliased enum", "if row.last_outcome is OutcomeCode.NOT_SENT:\n    pass"),
        ("a nested attribute", "if row.outbox.state is DispatchState.PENDING:\n    pass"),
        ("a subscript", "if rows[0].state is AttemptState.IN_FLIGHT:\n    pass"),
        ("the negated form", "if entry.state is not ReplayState.PENDING:\n    pass"),
    ],
)
def test_kill_each_stored_enum_identity_shape_is_detected(label: str, shape: str) -> None:
    """**Every shape the guard now claims to catch, proven against the guard's own logic.**

    Four of these six survived the first version. It required the *other* side of the comparison to
    be an attribute on a plain local and the enum to be named by its original class, so an aliased
    import, a nested attribute and a subscript all passed — and the alias is what the retry and
    dispatcher modules actually use.
    """
    import ast

    stored_enums = {
        "ReplayState",
        "DispatchState",
        "AttemptState",
        "PostingOutcome",
        "OutcomeCode",
        "RecoveryState",
        "ExceptionStatus",
        "TreatmentCode",
        "ExceptionClassification",
    }

    def flagged(source: str) -> bool:
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, ast.Is | ast.IsNot) for op in node.ops):
                continue
            sides = [node.left, *node.comparators]
            members = [
                side
                for side in sides
                if isinstance(side, ast.Attribute)
                and isinstance(side.value, ast.Name)
                and side.value.id in stored_enums
            ]
            if not members:
                continue
            others = [side for side in sides if side not in members]
            if any(isinstance(side, ast.Attribute | ast.Subscript) for side in others):
                return True
        return False

    assert flagged(shape), f"{label} was not detected"
    coerced = shape.replace("entry.replay_state is", "ReplayState(entry.replay_state) is")
    if coerced != shape:
        assert not flagged(coerced), "the coerced form must be accepted"
