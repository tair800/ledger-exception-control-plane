"""§18's five counters and three histograms, each behind one named function.

One function per metric rather than a generic ``increment(name, labels)``, for the reason every
other vocabulary in this project is closed: the call sites are copied into two later projects, and a
free-form emitter is how a metric ends up with three label sets and no dashboard that can read all
three.

**No metric carries a high-cardinality label, and that is a deliberate exclusion.** Not
``correlation_id``, not ``exception_id``, not ``adjustment_id``, not ``operation_id``. Those belong
on spans, where one value per trace is the point; on a metric they create one time series per
settlement line, which costs money at the backend, makes a query slow enough to be useless, and is
the single most common way a first observability increment has to be rolled back. A test asserts no
measurement this module emits carries one.

**No function here reads a clock.** The two latencies that span process boundaries take their
durations from timestamps the caller already holds — the same discipline every business path in this
repository follows, where ``received_at``, ``matched_at`` and ``sent_at`` are parameters rather than
clock reads, because on a replay the correct value is not now. The third latency is the measured
duration of the ``lecp.post`` span and is recorded by
:func:`~ledger_exception_control_plane.observability.instrumentation.instrument`.

**A negative duration is not recorded.** Two persisted timestamps can be ordered backwards by clock
skew between the process that wrote one and the process that wrote the other, and a histogram with a
negative observation in it reports a p95 nobody can explain. The observation is dropped rather than
clamped to zero: a zero is a measurement claiming the stage was instantaneous.
"""

from __future__ import annotations

import datetime as dt
import math

from ledger_exception_control_plane.observability.conventions import (
    Attr,
    AttributeValue,
    Metric,
)
from ledger_exception_control_plane.observability.redaction import redact_attributes
from ledger_exception_control_plane.observability.runtime import TelemetrySink, active_sink

__all__ = [
    "record_abstention",
    "record_approval",
    "record_approval_latency",
    "record_dead_letter",
    "record_dlq_depth",
    "record_exception_latency",
    "record_quarantine",
    "record_retry",
    "seconds_between",
]


def _emit(
    metric: Metric,
    value: float,
    labels: dict[str, object],
    sink: TelemetrySink | None,
) -> None:
    """The one route to the sink for a measurement. Redacts, like the span path does."""
    target = sink if sink is not None else active_sink()
    redacted = redact_attributes(labels)
    target.record(metric, value, redacted.attributes)


def _is_a_usable_duration(seconds: float) -> bool:
    """Whether a duration may be recorded. Finite and not negative; see the module docstring."""
    return math.isfinite(seconds) and seconds >= 0.0


def seconds_between(start: dt.datetime, end: dt.datetime) -> float:
    """The interval between two persisted timestamps, in seconds.

    A helper rather than an inline subtraction at each call site, so every latency in this system is
    computed the same way and the negative case is handled in one place — this returns the raw
    signed value, and the recorders below refuse it.
    """
    return (end - start).total_seconds()


# ======================================================================================
# Counters and the gauge
# ======================================================================================


def record_retry(
    *,
    idempotency_mode: str,
    query_mode: str,
    posting_outcome: str,
    sink: TelemetrySink | None = None,
) -> None:
    """One further attempt scheduled after an allowlisted transport failure (§15, §18).

    Split by both adapter capability modes because §19 requires every dispatch question to be
    readable per capability: a retry count that cannot be split by capability cannot show that the
    ``NONE``/``NONE`` configuration is not being retried into a double posting.
    """
    _emit(
        Metric.RETRIES,
        1.0,
        {
            Attr.ADAPTER_IDEMPOTENCY_MODE.value: idempotency_mode,
            Attr.ADAPTER_QUERY_MODE.value: query_mode,
            Attr.POSTING_OUTCOME.value: posting_outcome,
        },
        sink,
    )


def record_dead_letter(*, sink: TelemetrySink | None = None) -> None:
    """One dead letter written after a bounded retry ran out. Monotonic."""
    _emit(Metric.DLQ_ENTRIES, 1.0, {}, sink)


def record_dlq_depth(depth: int, *, sink: TelemetrySink | None = None) -> None:
    """How many dead letters are awaiting replay, from a count the caller already holds.

    A gauge, not a counter, because a depth falls when the queue drains — §18 lists it under
    "counters" and a counter named "depth" can only ever go up. Never emitted by a caller that did
    not look: there is no default and no zero, because a zero from a pass that did not query is
    indistinguishable from an empty queue.
    """
    if depth < 0:
        return
    _emit(Metric.DLQ_DEPTH, float(depth), {}, sink)


def record_approval(
    *,
    decision: str,
    role: str,
    sink: TelemetrySink | None = None,
) -> None:
    """One human approval decision (§18).

    Split by role as well as decision, because §16's control is role separation and a count that
    cannot say which kind of principal decided cannot show that an operator never approved anything.
    """
    _emit(
        Metric.APPROVALS,
        1.0,
        {Attr.APPROVAL_DECISION.value: decision, Attr.APPROVAL_ROLE.value: role},
        sink,
    )


def record_abstention(*, sink: TelemetrySink | None = None) -> None:
    """One proposal where the model declined to propose a treatment (§18).

    Counted separately from a failure, because an abstention is a first-class answer: §6 makes
    escalation the correct outcome where a treatment cannot be determined, so a rising abstention
    rate is a signal about the corpus, not an error rate.
    """
    _emit(Metric.ABSTENTIONS, 1.0, {}, sink)


def record_quarantine(*, posting_outcome: str, sink: TelemetrySink | None = None) -> None:
    """One posting held aside for a decision rather than decided (§18, §13.5).

    Labelled with the posting outcome, because the audit reading is lossy by construction — both
    ``unknown`` and ``partially_applied`` read as quarantined — and the difference between them is
    the whole subject of §13.5.
    """
    _emit(Metric.QUARANTINES, 1.0, {Attr.POSTING_OUTCOME.value: posting_outcome}, sink)


# ======================================================================================
# Histograms whose interval spans a process boundary
# ======================================================================================


def record_approval_latency(seconds: float, *, sink: TelemetrySink | None = None) -> None:
    """Exception raised to approval recorded (§18).

    Takes a duration rather than two timestamps so the caller decides which two facts it is the
    interval between — and so this module reads no clock. Use :func:`seconds_between`.
    """
    if not _is_a_usable_duration(seconds):
        return
    _emit(Metric.APPROVAL_LATENCY, seconds, {}, sink)


def record_exception_latency(seconds: float, *, sink: TelemetrySink | None = None) -> None:
    """Payload received to posting resolved — §18's end-to-end exception latency.

    Cannot come from a span duration: the interval crosses process boundaries, can span hours, and
    includes the time an exception sat waiting for a human. Only persisted timestamps can measure
    it.
    """
    if not _is_a_usable_duration(seconds):
        return
    _emit(Metric.EXCEPTION_LATENCY, seconds, {}, sink)


def dispatch_latency_labels(stage: str) -> dict[str, AttributeValue]:
    """The label set the dispatch histogram is recorded with.

    Exported so the test that asserts metric labels are low-cardinality can name the one label set
    produced outside this module — ``instrument`` records the dispatch histogram itself, from the
    span's own measured duration, and this is what it labels it with.
    """
    return {Attr.STAGE.value: stage}
