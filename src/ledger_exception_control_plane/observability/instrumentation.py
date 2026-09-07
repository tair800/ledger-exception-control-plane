"""How a stage is instrumented: one context manager, one decorator, one attribute gate.

Two entry points, because adoption has two shapes.

* :func:`instrumented` is a decorator, so a stage that needs nothing but a span and the ambient
  correlation id is instrumented by adding **one line** above its definition. That matters more than
  it sounds: this package may not edit the financial core, and a hook that requires re-indenting a
  function body is a hook that arrives as a diff somebody has to review line by line.
* :func:`instrument` is the context manager, for the stages that know which settlement line they
  are about — those need the artefact-derived correlation id and usually want to record an outcome
  attribute from inside the body.

**Every attribute goes through redaction, and there is no other route.** ``instrument`` is the only
caller of ``sink.start_span`` in this package, and :class:`SpanRecorder` is the only thing that can
set an attribute on an open span. A test walks the package and asserts both, because a redaction
gate with a bypass is a comment.

**A span never changes what the wrapped code does.** The exception is re-raised unchanged, the
return value is returned unchanged, and no failure inside the telemetry path is allowed to become a
failure of a financial operation — which is why the correlation-id check sanitises rather than
raises and why a denied attribute is dropped rather than refused.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final, ParamSpec, TypeVar, cast

from ledger_exception_control_plane.log import correlation_id_scope
from ledger_exception_control_plane.observability.context import TelemetryContext
from ledger_exception_control_plane.observability.conventions import (
    GEN_AI_ATTRIBUTES,
    UNRECORDED_REASONS,
    Attr,
    Metric,
    ModelCallOrigin,
    Span,
)
from ledger_exception_control_plane.observability.redaction import redact_attributes
from ledger_exception_control_plane.observability.runtime import (
    SpanScope,
    TelemetrySink,
    active_sink,
)

__all__ = [
    "OUTCOME_ABSTAINED",
    "OUTCOME_FAILURE",
    "OUTCOME_QUARANTINED",
    "OUTCOME_SUCCESS",
    "PROPOSAL_OPERATION",
    "SpanRecorder",
    "instrument",
    "instrumented",
    "proposal_attributes",
]

#: The audit outcome vocabulary, reused rather than reinvented.
#:
#: §11 fixes four outcomes and ``db.control.AuditOutcome`` holds them. They are spelled here as
#: constants rather than imported, because this package must import with no ORM in its graph — a
#: test asserts the two vocabularies are identical, so the duplication cannot drift.
OUTCOME_SUCCESS: Final = "success"
OUTCOME_FAILURE: Final = "failure"
OUTCOME_ABSTAINED: Final = "abstained"
OUTCOME_QUARANTINED: Final = "quarantined"

#: ``gen_ai.operation.name`` for the proposal span: a structured-output chat completion.
#:
#: It names the request shape the adapter builds. It is *not* a claim that a request was sent — that
#: fact is ``lecp.model_call.origin``, and in this repository it is always ``replayed``.
PROPOSAL_OPERATION: Final = "chat"


class SpanRecorder:
    """The only thing that can write to an open span. Redacts on the way through.

    Handed to the body of an :func:`instrument` block so a stage can record what it learned — the
    posting outcome, the treatment code, the query answer — without reaching the sink itself.
    """

    def __init__(self, scope: SpanScope, *, dropped: tuple[str, ...] = ()) -> None:
        self._scope = scope
        self._outcome_set = False
        # Accumulated rather than replaced. The first version wrote the whole ``lecp.redacted``
        # value on each call, so the second denied attribute erased the record of the first — the
        # span said one key had been dropped while three had, which is the one thing this attribute
        # exists to prevent.
        self._dropped: set[str] = set(dropped)

    def set(self, key: Attr | str, value: object) -> None:
        """Record one attribute, after redaction. Silently drops anything that may not be recorded.

        Dropped rather than refused, deliberately: this runs inside a financial operation, and an
        exception raised here because somebody passed a ``Decimal`` would turn a telemetry mistake
        into a failed posting. The drop is visible — the key is added to ``lecp.redacted``.
        """
        name = key.value if isinstance(key, Attr) else key
        self.set_many({name: value})

    def set_many(self, attributes: Mapping[str, object]) -> None:
        """Record several attributes at once."""
        redacted = redact_attributes(attributes)
        for attribute, value in redacted.attributes.items():
            self._scope.set_attribute(attribute, value)
        self._note_dropped(redacted.dropped)

    def _note_dropped(self, dropped: tuple[str, ...]) -> None:
        """Add to the span's record of what was removed, keeping every earlier entry."""
        if not dropped:
            return
        self._dropped |= set(dropped)
        self._scope.set_attribute(Attr.REDACTED.value, tuple(sorted(self._dropped)))

    def outcome(self, outcome: str) -> None:
        """Record the stage's outcome, using §11's four-value vocabulary.

        Suppresses the automatic ``success`` this class would otherwise record: a stage that
        abstained or quarantined did not succeed, and letting the default win would put the wrong
        word on the span in exactly the cases §13.5 and §6 care about most.
        """
        self._outcome_set = True
        self._scope.set_attribute(Attr.OUTCOME.value, outcome)

    def mark_unrecorded(self, *fields: str) -> None:
        """Declare, on the span, the fields this system cannot know — by name.

        Raises for a field with no reason on record. That is deliberate and matches
        ``audit.emit``'s refusal of an unknown scope: the point of this mechanism is that every
        absence has a *stated* reason, and a call site allowed to invent an absence would give the
        list the one property it exists to prevent.
        """
        unknown = [field for field in fields if field not in UNRECORDED_REASONS]
        if unknown:
            raise ValueError(
                f"{unknown} have no reason on record. Add an entry to UNRECORDED_REASONS saying "
                "why the value cannot be known, or record the value."
            )
        self._scope.set_attribute(Attr.NOT_RECORDED.value, tuple(fields))

    def _finish(self, outcome: str) -> None:
        """Apply the default outcome if the body did not set one."""
        if not self._outcome_set:
            self._scope.set_attribute(Attr.OUTCOME.value, outcome)


@contextlib.contextmanager
def instrument(
    span: Span,
    context: TelemetryContext,
    *,
    attributes: Mapping[str, object] | None = None,
    duration_histogram: Metric | None = None,
    sink: TelemetrySink | None = None,
) -> Iterator[SpanRecorder]:
    """Open one span, bind its correlation id for every log line inside it, and close it.

    ``duration_histogram`` records the span's own measured wall time into a §18 histogram. Only
    ``lecp.dispatch.latency`` uses it, and only on the ``lecp.post`` span, because there the span's
    duration *is* the interval §18 names — send to outcome recorded. The other two histograms span
    process boundaries and take their durations from persisted timestamps; see
    :mod:`~ledger_exception_control_plane.observability.metrics`.

    **Binding the correlation id is half the value of this function.** ``log.py`` renders
    ``correlation_id`` on every line from a context variable that only ``api.py``'s HTTP middleware
    binds. A stage driven from a CLI — a matching pass, a retry pass, the replay command — therefore
    logs ``correlation_id: null`` today, which is useless exactly where a batch failure has to be
    followed. Wrapping the stage in a span fixes the log line at the same time.
    """
    target = sink if sink is not None else active_sink()

    opening: dict[str, object] = dict(context.attributes())
    opening[Attr.STAGE.value] = span.value
    if attributes:
        opening.update(attributes)

    redacted = redact_attributes(opening)
    if redacted.dropped:
        redacted.attributes[Attr.REDACTED.value] = redacted.dropped

    started = time.perf_counter()
    with correlation_id_scope(context.correlation_id):
        scope = target.start_span(span.value, redacted.attributes)
        recorder = SpanRecorder(scope, dropped=redacted.dropped)
        try:
            yield recorder
        except BaseException as error:
            # The type, never the message: a client library's exception text routinely embeds a
            # request URL, and an authorisation header or a key in a query string travels with it.
            # `log.py` omits tracebacks for the same reason.
            recorder.outcome(OUTCOME_FAILURE)
            scope.set_attribute(Attr.ERROR_TYPE.value, type(error).__name__)
            scope.note_failure()
            raise
        finally:
            recorder._finish(OUTCOME_SUCCESS)
            elapsed = time.perf_counter() - started
            if duration_histogram is not None:
                target.record(duration_histogram, elapsed, {Attr.STAGE.value: span.value})
            scope.end()


P = ParamSpec("P")
R = TypeVar("R")

#: Parameter names :func:`instrumented` will lift onto the span if the wrapped function has them.
#:
#: A closed set, so the decorator's magic is bounded and greppable. Every name here is an
#: identifier this system already passes by that name — ``exception_id`` in the model layer,
#: ``adjustment_id`` throughout operations, ``batch_id`` in ingestion and matching.
RECOGNISED_IDENTIFIERS: Final[tuple[str, ...]] = (
    "exception_id",
    "operation_id",
    "adjustment_id",
    "batch_id",
)


def instrumented(
    span: Span,
    *,
    duration_histogram: Metric | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a stage in a span, using the ambient correlation id. The one-line hook.

    Identifiers are lifted from the call by parameter name, restricted to
    :data:`RECOGNISED_IDENTIFIERS`. Reading arguments by name is the only way a decorator can be a
    genuinely one-line adoption, and the closed list is what keeps it from being magic: a reader can
    see exactly which four names are looked at, and nothing else about the call is inspected.

    Works on both coroutine functions and ordinary ones. For a coroutine function the span must wrap
    the *await*, not the call that returns the coroutine — otherwise the span closes before the body
    has run, which is a mistake that produces spans of a few microseconds and no correlation to
    anything.
    """

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        def context_for(args: tuple[object, ...], kwargs: dict[str, object]) -> TelemetryContext:
            bound = signature.bind_partial(*args, **kwargs)
            found = {
                name: value
                for name in RECOGNISED_IDENTIFIERS
                if (value := bound.arguments.get(name)) is not None
            }
            return TelemetryContext.ambient(
                exception_id=_as_identifier(found.get("exception_id")),
                operation_id=_as_optional_string(found.get("operation_id")),
                adjustment_id=_as_identifier(found.get("adjustment_id")),
                batch_id=_as_identifier(found.get("batch_id")),
            )

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                with instrument(
                    span,
                    context_for(args, kwargs),
                    duration_histogram=duration_histogram,
                ):
                    return await function(*args, **kwargs)

            # The cast is unavoidable: `R` is the coroutine type for an async function, and the
            # wrapper awaits it, so the two agree at runtime and cannot be expressed together.
            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with instrument(
                span,
                context_for(args, kwargs),
                duration_histogram=duration_histogram,
            ):
                return function(*args, **kwargs)

        return wrapper

    return decorate


def _as_identifier(value: object) -> str | None:
    """Coerce a lifted identifier to a string. ``UUID`` and ``str`` are the only shapes it takes."""
    return None if value is None else str(value)


def _as_optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def proposal_attributes(
    *,
    provider: str,
    model_id: str,
    model_version: str,
    origin: ModelCallOrigin = ModelCallOrigin.REPLAYED,
) -> dict[str, object]:
    """The GenAI attributes for the ``lecp.propose_treatment`` span, and nothing untrue.

    Three published semantic-convention keys, both halves of §11's model identity, and the origin of
    the answer. The convention names more attributes than these and §18 asks for three more still —
    token usage, estimated cost, processing region — and every one of those describes a network call
    this repository never makes. Those are declared absent through
    :meth:`SpanRecorder.mark_unrecorded`, which is why this helper does not silently omit them: the
    caller passes the list, and the span carries it.

    Returns a plain mapping rather than setting anything, so a call site can see exactly what it is
    about to record.
    """
    return {
        Attr.GEN_AI_PROVIDER_NAME.value: provider,
        Attr.GEN_AI_REQUEST_MODEL.value: model_id,
        Attr.GEN_AI_OPERATION_NAME.value: PROPOSAL_OPERATION,
        Attr.MODEL_VERSION.value: model_version,
        Attr.MODEL_CALL_ORIGIN.value: origin.value,
    }


#: The GenAI attribute keys :func:`proposal_attributes` fills. Exported for the test that asserts
#: the two lists agree, so a key added to the conventions cannot be silently left unfilled.
FILLED_GEN_AI_ATTRIBUTES: Final[tuple[str, ...]] = tuple(
    attribute.value for attribute in GEN_AI_ATTRIBUTES
)
