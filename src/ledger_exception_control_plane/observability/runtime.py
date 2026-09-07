"""Where telemetry goes. Three sinks, and a no-op is the default.

The OpenTelemetry SDK is **not a declared dependency of this project**, and this package does not
add one — a dependency is added by the increment that needs it, after review, in
``pyproject.toml``. So this module is built the way every other external boundary in this repository
is built: an injected handle behind a narrow protocol, with a null implementation that is the
default and a recording implementation the tests assert against.

That is not a workaround. It is the same seam ``llm/port.py`` uses for a provider and
``ledger/port.py`` for a ledger, and it buys the same three things: the package imports and its
tests pass with no SDK installed, instrumenting a module cannot fail because a library is missing,
and the adapter's own logic is exercised in CI against a stand-in for the SDK's API rather than
being unexercised code that claims to work.

**What is and is not proven here.** :class:`OpenTelemetrySink` is tested against a hand-written
double implementing the API surface it calls. That proves the adapter's logic — instrument caching,
unit and description propagation, attribute forwarding, the gauge-versus-counter split. It does
**not** prove interoperability with the real SDK, and nothing in this repository can until the
dependency is declared and a run reaches a collector. ``docs/observability.md`` says so in those
words.

**Provider configuration is deliberately not here.** Installing a ``TracerProvider``, a
``MeterProvider`` and an OTLP exporter is application bootstrap: it belongs where the app is built,
runs once, and needs the settings object. This module reads whatever provider the application
installed, through the API's ``get_tracer`` / ``get_meter``. Configuring a global provider from
inside a library is how two of them end up installed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import importlib.util
from collections.abc import Iterator, Mapping
from typing import Any, Final, Protocol, runtime_checkable

from ledger_exception_control_plane.observability.conventions import (
    AttributeValue,
    Metric,
    MetricKind,
    spec_for,
)

__all__ = [
    "NullSink",
    "OpenTelemetrySink",
    "RecordedMeasurement",
    "RecordedSpan",
    "RecordingSink",
    "SpanScope",
    "TelemetrySink",
    "active_sink",
    "configure_telemetry",
    "install_sink",
    "opentelemetry_api_is_installed",
    "sink_from_installed_api",
    "use_sink",
]


@runtime_checkable
class SpanScope(Protocol):
    """One open span. Closed exactly once, by the instrumentation that opened it."""

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """Record one already-redacted attribute."""

    def note_failure(self) -> None:
        """Mark the span as having failed, in whatever way the backend expresses that."""

    def end(self) -> None:
        """Close the span."""


@runtime_checkable
class TelemetrySink(Protocol):
    """Where spans and measurements go."""

    def start_span(self, name: str, attributes: Mapping[str, AttributeValue]) -> SpanScope:
        """Open a span. ``attributes`` have already passed redaction."""
        ...

    def record(
        self, metric: Metric, value: float, attributes: Mapping[str, AttributeValue]
    ) -> None:
        """Record one measurement. ``attributes`` have already passed redaction."""


# ======================================================================================
# The default: nothing is emitted
# ======================================================================================


class _NullScope:
    """A span that goes nowhere."""

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """Discard the attribute."""

    def note_failure(self) -> None:
        """Discard the failure mark."""

    def end(self) -> None:
        """Nothing to close."""


class NullSink:
    """Emits nothing, and is what an unconfigured process gets.

    The default on purpose. An instrumented financial path must behave identically whether or not
    a collector is reachable, and the way to guarantee that is for the unconfigured case to be the
    one every test of business behaviour runs through.
    """

    def start_span(self, name: str, attributes: Mapping[str, AttributeValue]) -> SpanScope:
        """Return a scope that records nothing."""
        return _NullScope()

    def record(
        self, metric: Metric, value: float, attributes: Mapping[str, AttributeValue]
    ) -> None:
        """Discard the measurement."""


# ======================================================================================
# The sink the tests assert against
# ======================================================================================


@dataclasses.dataclass(eq=False)
class RecordedSpan:
    """One span as it was emitted. Mutable while open, because a span is."""

    name: str
    attributes: dict[str, AttributeValue]
    failed: bool = False
    ended: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class RecordedMeasurement:
    """One measurement as it was emitted."""

    metric: Metric
    value: float
    attributes: dict[str, AttributeValue]


class _RecordingScope:
    def __init__(self, span: RecordedSpan) -> None:
        self._span = span

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self._span.attributes[key] = value

    def note_failure(self) -> None:
        self._span.failed = True

    def end(self) -> None:
        self._span.ended = True


class RecordingSink:
    """Keeps everything in memory, so a test can assert on what was actually emitted.

    **This is what makes the redaction guarantee falsifiable.** A test that only asserts a secret is
    absent passes trivially against a sink that emitted nothing at all, which is the failure mode
    that made an earlier secret-leak test in this repository pass against an empty record list. With
    a recording sink a test asserts both halves: the secret is gone *and* the non-sensitive
    attributes are present.
    """

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self.measurements: list[RecordedMeasurement] = []

    def start_span(self, name: str, attributes: Mapping[str, AttributeValue]) -> SpanScope:
        span = RecordedSpan(name=name, attributes=dict(attributes))
        self.spans.append(span)
        return _RecordingScope(span)

    def record(
        self, metric: Metric, value: float, attributes: Mapping[str, AttributeValue]
    ) -> None:
        self.measurements.append(
            RecordedMeasurement(metric=metric, value=value, attributes=dict(attributes))
        )

    def span_named(self, name: str) -> RecordedSpan:
        """The single span with this name. Raises if there is not exactly one.

        Exact rather than "the last one": a test asserting on "the" span while two were emitted is
        a test that would keep passing after a duplicate instrumentation hook appeared.
        """
        found = [span for span in self.spans if span.name == name]
        if len(found) != 1:
            raise AssertionError(f"expected exactly one {name!r} span, recorded {len(found)}")
        return found[0]

    def values_of(self, metric: Metric) -> list[float]:
        """Every value recorded for one metric, in order."""
        return [
            measurement.value for measurement in self.measurements if measurement.metric is metric
        ]


# ======================================================================================
# The OpenTelemetry adapter
# ======================================================================================


class _OtelSpan(Protocol):
    """The subset of the API's ``Span`` this adapter calls."""

    def set_attribute(self, key: str, value: object) -> None: ...

    def set_status(self, status: object) -> None: ...


class _OtelTracer(Protocol):
    """The subset of the API's ``Tracer`` this adapter calls."""

    def start_as_current_span(self, name: str) -> contextlib.AbstractContextManager[_OtelSpan]: ...


class _OtelCounter(Protocol):
    def add(self, amount: float, attributes: Mapping[str, object] | None = None) -> None: ...


class _OtelGauge(Protocol):
    def set(self, amount: float, attributes: Mapping[str, object] | None = None) -> None: ...


class _OtelHistogram(Protocol):
    def record(self, amount: float, attributes: Mapping[str, object] | None = None) -> None: ...


class _OtelMeter(Protocol):
    """The subset of the API's ``Meter`` this adapter calls."""

    def create_counter(self, name: str, unit: str = "", description: str = "") -> _OtelCounter: ...

    def create_gauge(self, name: str, unit: str = "", description: str = "") -> _OtelGauge: ...

    def create_histogram(
        self, name: str, unit: str = "", description: str = ""
    ) -> _OtelHistogram: ...


class _OtelScope:
    """One OpenTelemetry span, held open through an exit stack.

    ``start_as_current_span`` is used rather than ``start_span`` so the SDK establishes parenting:
    a ``lecp.post`` span opened inside a ``lecp.retry`` span should be its child, and that is what
    makes a single exception traceable end to end rather than as a pile of unrelated spans.

    **``record_exception`` is deliberately never called.** It writes the formatted traceback into a
    span event, and a traceback is a routine route for a connection string or an authorisation
    header to reach an aggregator — which is exactly why ``log.py`` records the exception type and
    omits the traceback. The instrumentation records ``lecp.error_type`` instead.
    """

    def __init__(self, stack: contextlib.ExitStack, span: _OtelSpan, error_status: object) -> None:
        self._stack = stack
        self._span = span
        self._error_status = error_status

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self._span.set_attribute(key, value)

    def note_failure(self) -> None:
        if self._error_status is not None:
            self._span.set_status(self._error_status)

    def end(self) -> None:
        self._stack.close()


class OpenTelemetrySink:
    """Emits through an injected OpenTelemetry tracer and meter.

    Instruments are created once per metric and cached: creating one per measurement is a documented
    way to end up with duplicate instrument registrations, and the unit and description would then
    be re-declared on every emission.

    ``error_status`` is injected rather than imported so this class needs no SDK type. The bootstrap
    in :func:`sink_from_installed_api` passes ``opentelemetry.trace.StatusCode.ERROR`` when the API
    is installed; without it a failed span still carries ``lecp.outcome=failure`` as an attribute
    and simply does not set the backend's own status field.
    """

    def __init__(
        self,
        *,
        tracer: _OtelTracer,
        meter: _OtelMeter,
        error_status: object = None,
    ) -> None:
        self._tracer = tracer
        self._meter = meter
        self._error_status = error_status
        self._counters: dict[Metric, _OtelCounter] = {}
        self._gauges: dict[Metric, _OtelGauge] = {}
        self._histograms: dict[Metric, _OtelHistogram] = {}

    def start_span(self, name: str, attributes: Mapping[str, AttributeValue]) -> SpanScope:
        stack = contextlib.ExitStack()
        span = stack.enter_context(self._tracer.start_as_current_span(name))
        for key, value in attributes.items():
            span.set_attribute(key, value)
        return _OtelScope(stack, span, self._error_status)

    def record(
        self, metric: Metric, value: float, attributes: Mapping[str, AttributeValue]
    ) -> None:
        spec = spec_for(metric)
        labels = dict(attributes)
        if spec.kind is MetricKind.COUNTER:
            self._counter(metric).add(value, labels)
        elif spec.kind is MetricKind.GAUGE:
            self._gauge(metric).set(value, labels)
        else:
            self._histogram(metric).record(value, labels)

    def _counter(self, metric: Metric) -> _OtelCounter:
        if metric not in self._counters:
            spec = spec_for(metric)
            self._counters[metric] = self._meter.create_counter(
                metric.value, unit=spec.unit, description=spec.description
            )
        return self._counters[metric]

    def _gauge(self, metric: Metric) -> _OtelGauge:
        if metric not in self._gauges:
            spec = spec_for(metric)
            self._gauges[metric] = self._meter.create_gauge(
                metric.value, unit=spec.unit, description=spec.description
            )
        return self._gauges[metric]

    def _histogram(self, metric: Metric) -> _OtelHistogram:
        if metric not in self._histograms:
            spec = spec_for(metric)
            self._histograms[metric] = self._meter.create_histogram(
                metric.value, unit=spec.unit, description=spec.description
            )
        return self._histograms[metric]


#: The API modules the bootstrap needs. The API package, not the SDK: a library reads the provider
#: the application installed, and installing one from here would be a second global provider.
_API_MODULES: Final[tuple[str, ...]] = ("opentelemetry.trace", "opentelemetry.metrics")


def _module_is_installed(name: str) -> bool:
    """Whether a module can be found, without importing it and without raising.

    ``find_spec`` on a dotted name imports the *parent* package to look inside it, so
    ``find_spec("opentelemetry.trace")`` raises ``ModuleNotFoundError`` when ``opentelemetry``
    itself is absent rather than returning ``None`` — which is the normal state of this repository.
    A probe that raises in the case it exists to detect is not a probe, so every failure to locate
    is treated as "not installed".
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _optional_module(name: str) -> Any:
    """Import a module if it is installed, otherwise ``None``.

    Through ``importlib`` rather than a literal ``import opentelemetry`` statement, because the
    package is not a declared dependency: a literal import would make this module fail type
    checking and would make the failure mode a static one rather than a runtime degradation.
    """
    if not _module_is_installed(name):
        return None
    return importlib.import_module(name)


def opentelemetry_api_is_installed() -> bool:
    """Whether the OpenTelemetry API is importable in this process."""
    return all(_module_is_installed(name) for name in _API_MODULES)


def sink_from_installed_api(service_name: str) -> TelemetrySink | None:
    """Build an :class:`OpenTelemetrySink` from the installed API, or ``None`` if it is absent.

    Unexercised in CI, and the only function in this package that is: it can only run where the
    dependency exists, and the dependency is not declared. Its three statements are the reason the
    rest of the adapter *is* exercised — everything with logic in it takes the tracer and meter as
    arguments.
    """
    trace = _optional_module("opentelemetry.trace")
    metrics = _optional_module("opentelemetry.metrics")
    if trace is None or metrics is None:  # pragma: no cover - depends on an undeclared dependency
        return None
    return OpenTelemetrySink(  # pragma: no cover - depends on an undeclared dependency
        tracer=trace.get_tracer(service_name),
        meter=metrics.get_meter(service_name),
        error_status=getattr(getattr(trace, "StatusCode", None), "ERROR", None),
    )


# ======================================================================================
# The installed sink
# ======================================================================================

#: A module-level global, deliberately, rather than a ``ContextVar``.
#:
#: The sink is a property of the process — one exporter, one collector — not of a request. A
#: context variable would give each task its own, which is how half a service's spans end up
#: unexported after a refactor moves a call behind ``asyncio.gather``.
_sink: TelemetrySink = NullSink()


def active_sink() -> TelemetrySink:
    """The sink this process is emitting through. A :class:`NullSink` until one is installed."""
    return _sink


def install_sink(sink: TelemetrySink) -> TelemetrySink:
    """Install a sink and return the one it replaced."""
    global _sink
    previous, _sink = _sink, sink
    return previous


@contextlib.contextmanager
def use_sink(sink: TelemetrySink) -> Iterator[TelemetrySink]:
    """Install a sink for the duration of a block, restoring the previous one.

    For tests, and for a command that wants telemetry for one bounded pass without changing what the
    rest of the process does.
    """
    previous = install_sink(sink)
    try:
        yield sink
    finally:
        install_sink(previous)


def configure_telemetry(*, service_name: str) -> TelemetrySink:
    """Install the best available sink and return it. The application's one-line hook.

    Returns a :class:`NullSink` when the OpenTelemetry API is not installed, which is the state of
    this repository today — so calling this changes nothing observable until the dependency is
    declared and a provider is configured, and calling it is still correct now.
    """
    return install_sink(sink_from_installed_api(service_name) or NullSink())
