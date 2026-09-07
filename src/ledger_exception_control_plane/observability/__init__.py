"""Telemetry conventions and instrumentation. `PROJECT_SPEC.md` §18, increment 8.1.

**What this package is for.** §18 asks for correlation-id propagation across every log line, span
and audit event, structured JSON logs, five counters, three histograms, and OpenTelemetry spans
using GenAI semantic conventions exported to self-hosted Langfuse. `CLAUDE.md` records that the
conventions established here are one of the five things this repository owes the rest of the
portfolio, and that projects 4 and 6 copy them. So the deliverable is a *vocabulary* — closed
enumerations of span names, attribute keys and metric names, with the reasoning attached — and the
smallest amount of machinery that makes the vocabulary usable in one line per call site.

**What it is not.** Not an observability platform. It configures no provider, starts no exporter
thread, opens no socket, adds no dependency, and holds no state beyond the installed sink. There is
no dashboard code, no sampling policy and no trace store, because none of those is what §18 asks
for and every one of them would be complexity justified by how advanced it looks rather than by the
problem — which `CLAUDE.md` rule 10 forbids in as many words.

**Three properties are load-bearing, and each has a test that can fail.**

1. *Every span carries a correlation id.* There is no constructor for a span context without one,
   and the context records whether the id is the artefact-derived one that joins to ``audit_event``
   or the ambient request-scoped one that does not.
2. *No secret and no merchant identifier reaches telemetry.* Every attribute passes one redaction
   gate, and the test asserts both directions — the DSN password, the bearer token and the merchant
   reference are gone, and the non-sensitive attributes are still there. A test that only checked
   for absence would pass against a sink that emitted nothing, which is how a secret-leak test in
   this repository once passed against an empty record list.
3. *Nothing this system cannot know is recorded.* §18 asks for token usage, estimated cost and
   processing region on every model call. No model call is made anywhere in this repository, so
   those three are declared absent by name with their reasons, exactly as ``provenance.py`` declares
   §11's two null fields. A zero would read as a measurement.

**The OpenTelemetry SDK is not a dependency of this project**, so the default sink emits nothing and
the whole package imports and passes its tests with nothing installed. ``docs/observability.md``
names the dependencies to add, the Compose service block for self-hosted Langfuse, and exactly which
one-line hooks the rest of the package adopted.
"""

from __future__ import annotations

from ledger_exception_control_plane.observability.context import (
    INVALID_CORRELATION_ID,
    TelemetryContext,
)
from ledger_exception_control_plane.observability.conventions import (
    ATTRIBUTE_DENY_LIST,
    GEN_AI_ATTRIBUTES,
    METRIC_SPECS,
    SPAN_ATTRIBUTE_MAX_LENGTH,
    UNRECORDED_REASONS,
    Attr,
    AttributeValue,
    CorrelationSource,
    Metric,
    MetricKind,
    MetricSpec,
    ModelCallOrigin,
    Span,
    spec_for,
)
from ledger_exception_control_plane.observability.instrumentation import (
    OUTCOME_ABSTAINED,
    OUTCOME_FAILURE,
    OUTCOME_QUARANTINED,
    OUTCOME_SUCCESS,
    PROPOSAL_OPERATION,
    SpanRecorder,
    instrument,
    instrumented,
    proposal_attributes,
)
from ledger_exception_control_plane.observability.langfuse import (
    ENVIRONMENT_VARIABLES,
    LangfuseConfigurationError,
    LangfuseExportTarget,
    langfuse_authorization_header,
    langfuse_otlp_endpoint,
    langfuse_target_from_environment,
)
from ledger_exception_control_plane.observability.metrics import (
    record_abstention,
    record_approval,
    record_approval_latency,
    record_dead_letter,
    record_dlq_depth,
    record_exception_latency,
    record_quarantine,
    record_retry,
    seconds_between,
)
from ledger_exception_control_plane.observability.redaction import (
    REDACTED,
    Redacted,
    key_is_denied,
    redact_attributes,
    redact_text,
)
from ledger_exception_control_plane.observability.runtime import (
    NullSink,
    OpenTelemetrySink,
    RecordedMeasurement,
    RecordedSpan,
    RecordingSink,
    SpanScope,
    TelemetrySink,
    active_sink,
    configure_telemetry,
    install_sink,
    opentelemetry_api_is_installed,
    sink_from_installed_api,
    use_sink,
)

__all__ = [
    "ATTRIBUTE_DENY_LIST",
    "ENVIRONMENT_VARIABLES",
    "GEN_AI_ATTRIBUTES",
    "INVALID_CORRELATION_ID",
    "METRIC_SPECS",
    "OUTCOME_ABSTAINED",
    "OUTCOME_FAILURE",
    "OUTCOME_QUARANTINED",
    "OUTCOME_SUCCESS",
    "PROPOSAL_OPERATION",
    "REDACTED",
    "SPAN_ATTRIBUTE_MAX_LENGTH",
    "UNRECORDED_REASONS",
    "Attr",
    "AttributeValue",
    "CorrelationSource",
    "LangfuseConfigurationError",
    "LangfuseExportTarget",
    "Metric",
    "MetricKind",
    "MetricSpec",
    "ModelCallOrigin",
    "NullSink",
    "OpenTelemetrySink",
    "RecordedMeasurement",
    "RecordedSpan",
    "RecordingSink",
    "Redacted",
    "Span",
    "SpanRecorder",
    "SpanScope",
    "TelemetryContext",
    "TelemetrySink",
    "active_sink",
    "configure_telemetry",
    "install_sink",
    "instrument",
    "instrumented",
    "key_is_denied",
    "langfuse_authorization_header",
    "langfuse_otlp_endpoint",
    "langfuse_target_from_environment",
    "opentelemetry_api_is_installed",
    "proposal_attributes",
    "record_abstention",
    "record_approval",
    "record_approval_latency",
    "record_dead_letter",
    "record_dlq_depth",
    "record_exception_latency",
    "record_quarantine",
    "record_retry",
    "redact_attributes",
    "redact_text",
    "seconds_between",
    "sink_from_installed_api",
    "spec_for",
    "use_sink",
]
