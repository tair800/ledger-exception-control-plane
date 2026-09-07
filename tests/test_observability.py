"""Telemetry conventions, redaction and instrumentation (M8.1, `PROJECT_SPEC.md` §18).

Four claims carry this increment, and each one has a test here that can fail:

1. **The conventions are data, not literals.** Span names, attribute keys and metric names live in
   one closed enumeration, the span set is checked against the audit verbs so the two signals cannot
   drift, and a guard scans the package for a hand-typed ``"lecp.…"`` string.
2. **Every span and every log line carries a correlation id.** Including the log lines of a batch
   stage driven from a CLI, which carried ``correlation_id: null`` before this increment — a test
   shows both states, because a fix nobody can see the absence of is a fix nobody can review.
3. **No secret and no merchant identifier reaches telemetry.** Asserted in both directions, and
   with a kill test: the same sink is handed the same secret without the gate, and the secret
   appears. Without that, the passing assertion would be equally consistent with a sink that emits
   nothing.
4. **Nothing the system cannot know is recorded.** §18 asks for token usage, estimated cost and a
   processing region on every model call, and no model call is made in this repository. Those are
   declared absent by name with a reason attached, and a test asserts no attribute key could ever
   carry one.

No database, no network, no provider and no OpenTelemetry SDK. The adapter for the SDK is exercised
against a hand-written double of the API surface it calls, which is stated as such in
``docs/observability.md``: it proves the adapter's logic, not interoperability with the real SDK.
"""

from __future__ import annotations

import ast
import asyncio
import decimal
import io
import json
import logging
import pathlib
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping
from typing import Final

import pytest
from pydantic import SecretStr

from ledger_exception_control_plane import audit
from ledger_exception_control_plane.db.control import (
    ApprovalDecision,
    AuditEvent,
    AuditOutcome,
    AuditTool,
    PostingOutcome,
)
from ledger_exception_control_plane.ledger.port import IdempotencyMode, PostingQueryMode
from ledger_exception_control_plane.log import (
    JsonLogFormatter,
    correlation_id_scope,
    new_correlation_id,
)
from ledger_exception_control_plane.observability import (
    ATTRIBUTE_DENY_LIST,
    ENVIRONMENT_VARIABLES,
    GEN_AI_ATTRIBUTES,
    INVALID_CORRELATION_ID,
    METRIC_SPECS,
    OUTCOME_ABSTAINED,
    OUTCOME_FAILURE,
    OUTCOME_QUARANTINED,
    OUTCOME_SUCCESS,
    PROPOSAL_OPERATION,
    REDACTED,
    SPAN_ATTRIBUTE_MAX_LENGTH,
    UNRECORDED_REASONS,
    Attr,
    CorrelationSource,
    LangfuseConfigurationError,
    Metric,
    MetricKind,
    ModelCallOrigin,
    NullSink,
    OpenTelemetrySink,
    RecordingSink,
    Span,
    TelemetryContext,
    active_sink,
    configure_telemetry,
    instrument,
    instrumented,
    key_is_denied,
    langfuse_authorization_header,
    langfuse_otlp_endpoint,
    langfuse_target_from_environment,
    opentelemetry_api_is_installed,
    proposal_attributes,
    record_abstention,
    record_approval,
    record_approval_latency,
    record_dead_letter,
    record_dlq_depth,
    record_exception_latency,
    record_quarantine,
    record_retry,
    redact_attributes,
    redact_text,
    seconds_between,
    spec_for,
    use_sink,
)

PACKAGE_ROOT: Final = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ledger_exception_control_plane"
)
OBSERVABILITY_ROOT: Final = PACKAGE_ROOT / "observability"

#: A password that must never appear in a span, a metric label or a log line.
LEAK_PASSWORD: Final = "telemetry-leak-canary-password"
LEAK_DSN: Final = f"postgresql://lecp:{LEAK_PASSWORD}@db:5432/lecp"
LEAK_BEARER: Final = "Bearer sk-ant-api03-NOT-A-REAL-KEY-000000000000"
LEAK_MERCHANT: Final = "MERCH-88213-ACME-RETAIL-GMBH"

#: A convention name as a call site would hand-type it: the namespace *and* something after it.
#: ``redaction.py`` legitimately holds the bare prefix ``"lecp."`` in order to strip it off a key,
#: and a guard that flagged the prefix itself would be pointing at the module that enforces the
#: fence.
_CONVENTION_NAME: Final = re.compile(r"lecp\.\w")


def _sources() -> dict[str, str]:
    """Every module in the observability package, keyed by its path relative to the package."""
    return {
        str(path.relative_to(PACKAGE_ROOT)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in sorted(OBSERVABILITY_ROOT.rglob("*.py"))
    }


def _context() -> TelemetryContext:
    return TelemetryContext.artefact(
        audit.correlation_id_for("a" * 64, 7),
        exception_id=uuid.UUID(int=1),
        operation_id="b" * 64,
    )


# ======================================================================================
# 1. The conventions are data, and they agree with the contracts they sit beside
# ======================================================================================


def test_every_audit_verb_has_a_span_and_the_extra_spans_are_the_two_unaudited_stages() -> None:
    """§11's verbs and §18's flows are one vocabulary, checked rather than asserted.

    Nine of §11's ten verbs name a stage §18 wants a span for; the tenth, ``approve``, does too.
    The two stages that emit *no* audit event — ingestion and classification, deliberately unaudited
    under ADR-058 because neither is ledger-affecting — still need spans, because a span is then
    their only signal outside their own tables.

    Asserted equal rather than as a subset in one direction, so a verb added without a span fails
    here and a span invented for nothing fails here too.
    """
    span_stems = {span.value.removeprefix("lecp.") for span in Span}
    verbs = {tool.value for tool in AuditTool}

    assert verbs <= span_stems, f"audit verbs with no span: {sorted(verbs - span_stems)}"
    assert span_stems - verbs == {"ingest", "classify"}


def test_the_outcome_vocabulary_is_the_audit_contract_s_own() -> None:
    """The four outcome words are §11's, duplicated as constants because the ORM is out of graph.

    Duplication is only safe while something proves the copies identical, which is what this is.
    """
    assert {OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_ABSTAINED, OUTCOME_QUARANTINED} == {
        outcome.value for outcome in AuditOutcome
    }


def test_every_name_is_unique_and_namespaced() -> None:
    """Two names for one thing is the drift the enumerations exist to prevent."""
    spans = [span.value for span in Span]
    metrics = [metric.value for metric in Metric]
    attributes = [attribute.value for attribute in Attr]

    assert len(set(spans)) == len(spans)
    assert len(set(metrics)) == len(metrics)
    assert len(set(attributes)) == len(attributes)
    assert not set(spans) & set(metrics), "a span and a metric share a name"

    assert all(name.startswith("lecp.") for name in spans + metrics)
    for key in attributes:
        assert key.startswith(("lecp.", "gen_ai.")) or key == Attr.CORRELATION_ID.value, key


def test_the_join_key_is_spelled_the_way_the_log_and_the_audit_row_spell_it() -> None:
    """``correlation_id``, unprefixed, in all three signals.

    The one attribute that is deliberately not namespaced. A span that called it
    ``lecp.correlation_id`` while ``log.py`` called it ``correlation_id`` and ``audit_event`` called
    it ``correlation_id`` would make the cross-signal join a string-rewriting exercise, which is how
    correlation quietly stops working.
    """
    formatter = JsonLogFormatter(service_name="lecp", environment="ci")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None)
    with correlation_id_scope("join-key-check"):
        rendered = json.loads(formatter.format(record))

    assert Attr.CORRELATION_ID.value in rendered
    assert "correlation_id" in {column.key for column in AuditEvent.__table__.columns}


def test_the_metric_set_is_exactly_what_section_eighteen_names() -> None:
    """*"Counters: retries, DLQ depth, approvals, abstentions, quarantines"* and three histograms.

    Five counted things, three distributions. The set is asserted by *kind* as well as by name,
    because the interesting failure is not a missing metric — that is obvious — it is an extra one,
    or one exported as the wrong instrument.
    """
    kinds = {metric: spec_for(metric).kind for metric in Metric}

    assert {metric for metric, kind in kinds.items() if kind is MetricKind.COUNTER} == {
        Metric.RETRIES,
        Metric.DLQ_ENTRIES,
        Metric.APPROVALS,
        Metric.ABSTENTIONS,
        Metric.QUARANTINES,
    }
    assert {metric for metric, kind in kinds.items() if kind is MetricKind.HISTOGRAM} == {
        Metric.APPROVAL_LATENCY,
        Metric.DISPATCH_LATENCY,
        Metric.EXCEPTION_LATENCY,
    }
    # DLQ depth is a level, not a monotonic count. §18 lists it under "counters"; a counter named
    # "depth" can only ever rise, which is exactly wrong for a queue that drains.
    assert kinds[Metric.DLQ_DEPTH] is MetricKind.GAUGE
    assert len(Metric) == 9


def test_every_metric_declares_a_kind_a_unit_and_a_description() -> None:
    """Total over the enum. A metric an exporter has to guess at is a metric with no axis."""
    assert set(METRIC_SPECS) == set(Metric)
    for metric in Metric:
        spec = spec_for(metric)
        assert spec.metric is metric
        assert spec.unit
        assert len(spec.description) > 40, f"{metric} has no real description"

    for metric in (Metric.APPROVAL_LATENCY, Metric.DISPATCH_LATENCY, Metric.EXCEPTION_LATENCY):
        assert spec_for(metric).unit == "s", "durations are seconds, per the OTel convention"


def test_no_module_hand_types_a_span_or_metric_name() -> None:
    """The point of the enumerations, enforced.

    ``conventions.py`` is where the strings live. Anywhere else, a literal beginning ``lecp.`` is a
    call site that has stopped using the vocabulary — and the copy that reaches project 4 will be
    whichever spelling that call site invented.
    """
    offenders: list[str] = []
    for name, source in _sources().items():
        if name.endswith("conventions.py"):
            continue
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _CONVENTION_NAME.match(node.value)
                and id(node) not in docstrings
            ):
                offenders.append(f"{name}:{node.lineno} {node.value!r}")

    assert offenders == [], f"hand-typed convention names: {offenders}"


# ======================================================================================
# 2. A correlation id on every span, and on every log line inside one
# ======================================================================================


def test_every_span_carries_a_correlation_id_a_source_and_a_stage() -> None:
    sink = RecordingSink()
    with instrument(Span.MATCH, _context(), sink=sink):
        pass

    span = sink.span_named(Span.MATCH.value)
    assert span.attributes[Attr.CORRELATION_ID.value] == audit.correlation_id_for("a" * 64, 7)
    assert span.attributes[Attr.CORRELATION_SOURCE.value] == CorrelationSource.ARTEFACT.value
    assert span.attributes[Attr.STAGE.value] == Span.MATCH.value
    assert span.ended, "the span was never closed"


def test_the_artefact_derived_id_is_taken_verbatim_from_the_audit_module() -> None:
    """The id that joins to the trail is the audit module's, unaltered.

    ``config.is_valid_correlation_id`` would have rejected it — that policy governs an untrusted
    inbound header and admits only ``[A-Za-z0-9_-]``, and the canonical id contains colons. Reusing
    it here would have replaced the one correlation id that joins to ``audit_event`` with a marker,
    which is the failure this test exists to catch.
    """
    canonical = audit.correlation_id_for("c" * 64, 42)
    assert TelemetryContext.artefact(canonical).correlation_id == canonical
    assert canonical.count(":") == 2, "the shape that a header policy would have rejected"


def test_an_ambient_context_takes_the_bound_id_and_generates_one_when_nothing_is_bound() -> None:
    bound = new_correlation_id()
    with correlation_id_scope(bound):
        assert TelemetryContext.ambient().correlation_id == bound

    generated = TelemetryContext.ambient()
    assert generated.correlation_id
    assert generated.source is CorrelationSource.AMBIENT


@pytest.mark.parametrize(
    "candidate",
    ["", "with a space", "line\nbreak", "tab\tseparated", "x" * 129, "\x00null"],
)
def test_an_unusable_correlation_id_is_replaced_rather_than_recorded(candidate: str) -> None:
    """A malformed id must never reach a span or a log line verbatim.

    Replaced rather than refused, following ``api.py``'s policy for a malformed inbound header:
    telemetry that can fail a financial operation is worse than telemetry that loses an id. The
    marker is fixed and greppable, like ``audit.UNRECORDED_CORRELATION_ID``.
    """
    assert TelemetryContext.artefact(candidate).correlation_id == INVALID_CORRELATION_ID


def test_log_lines_inside_a_span_carry_its_correlation_id_and_outside_one_do_not() -> None:
    """The half of §18 that is about log lines, and the defect this hook closes.

    ``log.py`` renders ``correlation_id`` from a context variable that only ``api.py``'s HTTP
    middleware binds. So a stage driven from a CLI — a matching pass, a retry pass, the replay
    command — logs ``correlation_id: null`` today. Both states are asserted here, because a
    correction whose "before" nobody can see is a correction nobody can review.

    The formatter is installed on the root logger for the duration, rather than formatting captured
    records afterwards: the id is read at *format* time, so formatting outside the scope would test
    the test rather than the code.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter(service_name="lecp", environment="ci"))
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    logger = logging.getLogger("test.observability")
    try:
        logger.info("outside the span")
        with instrument(Span.RETRY, _context(), sink=RecordingSink()):
            logger.info("inside the span")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    emitted = {line["event"]: line["correlation_id"] for line in lines}

    assert emitted["outside the span"] is None
    assert emitted["inside the span"] == audit.correlation_id_for("a" * 64, 7)


def test_identity_attributes_are_omitted_when_absent_rather_than_sent_as_null() -> None:
    """An attribute set to null is not an absent attribute; it is an assertion that it is null."""
    sink = RecordingSink()
    with instrument(Span.INGEST, TelemetryContext.ambient(), sink=sink):
        pass

    attributes = sink.span_named(Span.INGEST.value).attributes
    assert Attr.EXCEPTION_ID.value not in attributes
    assert Attr.ADJUSTMENT_ID.value not in attributes
    assert Attr.OPERATION_ID.value not in attributes


# ======================================================================================
# 3. Redaction — asserted in both directions, and with a kill test
# ======================================================================================


def test_no_secret_and_no_merchant_identifier_survives_into_telemetry() -> None:
    """§16 and §17, on the telemetry signal. **The test this increment turns on.**

    Both directions are asserted deliberately. A test that only checked for absence would pass
    against a sink that emitted nothing at all — which is exactly how a secret-leak test in this
    repository once passed against an empty ``caplog`` record list — so the non-sensitive attributes
    are asserted present in the same breath.
    """
    sink = RecordingSink()
    with instrument(
        Span.POST,
        _context(),
        sink=sink,
        attributes={
            Attr.ADAPTER_NAME.value: "simulated-enforces-key",
            Attr.ATTEMPT_NO.value: 2,
            "lecp.postgres_dsn": LEAK_DSN,
            "lecp.authorization": LEAK_BEARER,
            "lecp.merchant_reference": LEAK_MERCHANT,
        },
    ) as recorder:
        recorder.set(Attr.POSTING_OUTCOME, PostingOutcome.CONFIRMED.value)
        recorder.set("lecp.provider_api_key", "sk-ant-api03-ALSO-NOT-A-REAL-KEY-1111111111")

    span = sink.span_named(Span.POST.value)
    rendered = json.dumps({key: str(value) for key, value in span.attributes.items()})

    # The secrets and the merchant identifier are gone.
    for leaked in (LEAK_PASSWORD, LEAK_BEARER, LEAK_MERCHANT, "sk-ant-api03"):
        assert leaked not in rendered, f"{leaked!r} reached the span"
    assert "lecp.merchant_reference" not in span.attributes
    assert "lecp.authorization" not in span.attributes
    assert "lecp.provider_api_key" not in span.attributes

    # And what the span is *for* is still there.
    assert span.attributes[Attr.ADAPTER_NAME.value] == "simulated-enforces-key"
    assert span.attributes[Attr.ATTEMPT_NO.value] == 2
    assert span.attributes[Attr.POSTING_OUTCOME.value] == PostingOutcome.CONFIRMED.value
    assert span.attributes[Attr.CORRELATION_ID.value] == audit.correlation_id_for("a" * 64, 7)
    assert span.attributes[Attr.OUTCOME.value] == OUTCOME_SUCCESS

    # A key that carried a scrubbed value survives with the marker, so a reader can see the removal.
    assert span.attributes["lecp.postgres_dsn"] == f"postgresql://lecp:{REDACTED}@db:5432/lecp"

    # And every dropped key is named, so absence is visible rather than indistinguishable from
    # "nobody set it".
    dropped = span.attributes[Attr.REDACTED.value]
    assert isinstance(dropped, tuple)
    assert set(dropped) >= {"lecp.authorization", "lecp.merchant_reference"}


def test_the_leak_test_can_fail() -> None:
    """The kill test for the one above. **Without this, that assertion proves nothing.**

    The same sink is handed the same secret with the redaction gate bypassed. It appears. So the
    sink faithfully records whatever it is given, and the absence in the test above is caused by the
    gate rather than by a sink that emits nothing.
    """
    sink = RecordingSink()
    sink.start_span(Span.POST.value, {"lecp.postgres_dsn": LEAK_DSN})

    rendered = json.dumps(sink.span_named(Span.POST.value).attributes)
    assert LEAK_PASSWORD in rendered, "the sink is mute; every leak assertion is vacuous"


def test_a_decimal_never_reaches_telemetry_whatever_key_it_arrives_under() -> None:
    """The only ``Decimal`` in this system is a ledger amount, and it has exactly one owner.

    Refused by type rather than by key, because the key is the part a call site chooses:
    ``lecp.value``, ``lecp.delta`` and ``lecp.total`` are all plausible spellings and none of them
    is in the deny list.
    """
    result = redact_attributes({"lecp.value": decimal.Decimal("326.92")})
    assert result.attributes == {}
    assert result.dropped == ("lecp.value",)


def test_a_secret_wrapper_is_dropped_without_ever_being_read() -> None:
    """``SecretStr`` is recognised and refused, not unwrapped.

    Recognised by duck-typing, and *not* read: relying on another library's ``__str__`` to keep a
    security property is relying on a decision somebody else may revisit.
    """
    reads: list[str] = []

    class Spy:
        def get_secret_value(self) -> str:  # pragma: no cover - calling it is the failure
            reads.append("read")
            return LEAK_PASSWORD

    result = redact_attributes(
        {"lecp.wrapped": SecretStr(LEAK_PASSWORD), "lecp.spied": Spy()},
    )
    assert result.attributes == {}
    assert result.dropped == ("lecp.spied", "lecp.wrapped")
    assert reads == [], "the wrapper was unwrapped"


def test_a_long_value_is_truncated_because_an_attribute_is_a_label_not_a_payload() -> None:
    redacted = redact_text("x" * (SPAN_ATTRIBUTE_MAX_LENGTH + 500))
    assert redacted.startswith("x" * SPAN_ATTRIBUTE_MAX_LENGTH)
    assert redacted.endswith("[truncated]")


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("an uppercase bearer token", "BEARER ABCDEFGHIJKLMNOPQRSTUVWX"),
        ("a Google key", "AIzaSyA0123456789abcdefghijklmnopqrs"),
        ("an AWS access key id", "AKIAIOSFODNN7EXAMPLE"),
        ("a GitHub token", "ghp_0123456789abcdefghijklmnopqrstuvwx"),
        ("a JWT", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4f"),
    ],
)
def test_the_value_scrubber_covers_more_than_the_two_vendors_in_scope(
    label: str, value: str
) -> None:
    """A span carries whatever a developer had in their environment, not only this project's keys.

    The same set the cassette harness guards against, because the trade is the same: a false
    positive costs a redacted word, a miss costs a credential in a stream nobody reviews.
    """
    assert redact_text(f"prefix {value} suffix") == f"prefix {REDACTED} suffix", label


def test_the_value_scrubber_leaves_ordinary_prose_and_identifiers_alone() -> None:
    """The other direction, and the reason the patterns require length rather than a prefix.

    ``sk-`` as a bare substring matches "risk-based"; a bare hex run matches a content hash and an
    operation identifier, both of which must survive because they are what a trace is followed by.
    """
    prose = "A risk-based review of the sk-item and the bearer of record."
    assert redact_text(prose) == prose
    assert redact_text("a" * 64) == "a" * 64, "a content hash is not a credential"
    assert redact_text("lecp:" + "b" * 64 + ":000007") == "lecp:" + "b" * 64 + ":000007"


def test_a_nested_structure_is_not_recorded() -> None:
    """A mapping is a payload. Flatten it at the call site or leave it out."""
    result = redact_attributes({"lecp.nested": {"merchant_reference": LEAK_MERCHANT}})
    assert result.attributes == {}
    assert LEAK_MERCHANT not in json.dumps(result.dropped)


@pytest.mark.parametrize(
    ("key", "denied"),
    [
        ("lecp.amount", True),
        ("lecp.currency", True),
        ("lecp.merchant_ref", True),
        ("lecp.approval.token_sha", True),
        ("lecp.provider_api_key", True),
        ("Authorization", True),
        ("lecp.rationale", True),
        ("lecp.posting_ref", True),
        ("correlation_id", False),
        ("lecp.adapter.name", False),
        ("lecp.attempt_no", False),
        ("gen_ai.request.model", False),
        ("lecp.model.version", False),
    ],
)
def test_the_key_fence_admits_what_it_should_and_refuses_what_it_should(
    key: str, denied: bool
) -> None:
    """Substring matching as well as whole-segment matching, and the control cases beside it.

    Whole-segment matching alone admits ``merchant_ref``, ``token_sha`` and ``provider_api_key``,
    which are precisely what the fence exists to stop. The false-positive cost is an attribute a
    dashboard does not get; the miss cost is a merchant identifier in a trace.
    """
    assert key_is_denied(key) is denied


def test_no_attribute_in_the_vocabulary_could_carry_a_denied_value() -> None:
    """The vocabulary is checked against its own fence.

    This is the strongest honest statement about merchant identifiers. Value-level detection is not
    attempted — a merchant reference is indistinguishable from any other short opaque string — so
    the guarantee is structural: no attribute key this system emits is one that would carry an
    amount, a merchant reference or a credential, and the fence agrees.
    """
    for attribute in Attr:
        assert not key_is_denied(attribute.value), f"{attribute.value} is a denied key"
    assert ATTRIBUTE_DENY_LIST, "the fence is empty"


def test_only_the_langfuse_module_ever_unwraps_a_secret() -> None:
    """One call, in the one place a credential genuinely has to be read.

    Building an HTTP Basic header requires the secret key. Nothing on the emission path does, and a
    single ``get_secret_value`` appearing in ``redaction.py`` or ``instrumentation.py`` would mean a
    credential could be rendered into an attribute.
    """
    callers = {
        name: source.count("get_secret_value(")
        for name, source in _sources().items()
        if "get_secret_value(" in source
    }
    assert callers == {"observability/langfuse.py": 1}, callers


# ======================================================================================
# 4. Nothing the system cannot know
# ======================================================================================


def test_the_proposal_span_carries_only_genai_attributes_with_a_truthful_value() -> None:
    """GenAI semantic conventions, restricted to what is true here.

    ``gen_ai.provider.name``, ``gen_ai.request.model`` and ``gen_ai.operation.name`` are all true:
    the provider family the adapter speaks, the model the deployment configured and the request
    shape it builds. Token usage, cost, a processing region and a response model are not, and none
    of them appears.
    """
    sink = RecordingSink()
    with instrument(
        Span.PROPOSE_TREATMENT,
        _context(),
        sink=sink,
        attributes=proposal_attributes(
            provider="anthropic",
            model_id="claude-sonnet-4-5",
            model_version="2026-02-01",
        ),
    ) as recorder:
        recorder.outcome(OUTCOME_ABSTAINED)
        recorder.mark_unrecorded(*UNRECORDED_REASONS)

    span = sink.span_named(Span.PROPOSE_TREATMENT.value)
    assert span.attributes[Attr.GEN_AI_PROVIDER_NAME.value] == "anthropic"
    assert span.attributes[Attr.GEN_AI_REQUEST_MODEL.value] == "claude-sonnet-4-5"
    assert span.attributes[Attr.GEN_AI_OPERATION_NAME.value] == PROPOSAL_OPERATION
    assert span.attributes[Attr.MODEL_VERSION.value] == "2026-02-01"
    assert span.attributes[Attr.MODEL_CALL_ORIGIN.value] == ModelCallOrigin.REPLAYED.value
    assert span.attributes[Attr.OUTCOME.value] == OUTCOME_ABSTAINED

    for absent in (
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cost",
        "gen_ai.processing.region",
        "gen_ai.response.model",
        "gen_ai.agent.id",
    ):
        assert absent not in span.attributes


def test_the_fields_this_system_cannot_know_are_declared_by_name_with_a_reason() -> None:
    """§18's three model-call attributes, plus §11's agent identity. Named, not blanked.

    The same discipline ``provenance._gaps`` applies to §11's two null fields: a field a contract
    requires and a deployment cannot supply must be named, or its absence reads as "not applicable".
    A zero for token usage would read as "this call was free" and would flow into a published
    figure.
    """
    assert set(UNRECORDED_REASONS) == {
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cost",
        "gen_ai.processing.region",
        "gen_ai.agent.id",
    }
    for field, reason in UNRECORDED_REASONS.items():
        assert len(reason) > 80, f"{field} has a reason too short to be one"

    sink = RecordingSink()
    with instrument(Span.PROPOSE_TREATMENT, _context(), sink=sink) as recorder:
        recorder.mark_unrecorded("gen_ai.usage.input_tokens", "gen_ai.processing.region")

    assert sink.span_named(Span.PROPOSE_TREATMENT.value).attributes[Attr.NOT_RECORDED.value] == (
        "gen_ai.usage.input_tokens",
        "gen_ai.processing.region",
    )


def test_an_absence_with_no_reason_on_record_is_refused() -> None:
    """A call site allowed to invent an absence would give the list the property it prevents."""
    sink = RecordingSink()
    with (
        instrument(Span.PROPOSE_TREATMENT, _context(), sink=sink) as recorder,
        pytest.raises(ValueError, match="no reason on record"),
    ):
        recorder.mark_unrecorded("gen_ai.usage.made_up_field")


def test_no_attribute_key_promises_a_token_count_a_cost_or_a_region() -> None:
    """The vocabulary itself cannot express a fabricated model measurement.

    Stronger than not emitting them: the key does not exist, so no later call site can populate one
    without adding it here and having to justify it.
    """
    for attribute in Attr:
        lowered = attribute.value.lower()
        for forbidden in ("usage", "token", "cost", "region", "jurisdiction"):
            assert forbidden not in lowered, f"{attribute.value} promises {forbidden}"

    assert {attribute.value for attribute in GEN_AI_ATTRIBUTES} == {
        Attr.GEN_AI_PROVIDER_NAME.value,
        Attr.GEN_AI_REQUEST_MODEL.value,
        Attr.GEN_AI_OPERATION_NAME.value,
    }


# ======================================================================================
# 5. The counters, the gauge and the histograms
# ======================================================================================


def test_each_metric_is_emitted_by_its_own_named_function() -> None:
    sink = RecordingSink()
    with use_sink(sink):
        record_retry(
            idempotency_mode=IdempotencyMode.ENFORCES_KEY.value,
            query_mode=PostingQueryMode.NONE.value,
            posting_outcome=PostingOutcome.NOT_SENT.value,
        )
        record_dead_letter()
        record_dlq_depth(4)
        record_approval(decision=ApprovalDecision.EDITED.value, role="controller")
        record_abstention()
        record_quarantine(posting_outcome=PostingOutcome.UNKNOWN.value)
        record_approval_latency(12.5)
        record_exception_latency(3600.0)

    assert sink.values_of(Metric.RETRIES) == [1.0]
    assert sink.values_of(Metric.DLQ_ENTRIES) == [1.0]
    assert sink.values_of(Metric.DLQ_DEPTH) == [4.0]
    assert sink.values_of(Metric.APPROVALS) == [1.0]
    assert sink.values_of(Metric.ABSTENTIONS) == [1.0]
    assert sink.values_of(Metric.QUARANTINES) == [1.0]
    assert sink.values_of(Metric.APPROVAL_LATENCY) == [12.5]
    assert sink.values_of(Metric.EXCEPTION_LATENCY) == [3600.0]

    retry = next(m for m in sink.measurements if m.metric is Metric.RETRIES)
    assert retry.attributes[Attr.ADAPTER_IDEMPOTENCY_MODE.value] == "enforces_key"
    assert retry.attributes[Attr.POSTING_OUTCOME.value] == "not_sent"


def test_no_metric_carries_a_high_cardinality_label() -> None:
    """The exclusion that stops a first observability increment being rolled back.

    One time series per settlement line costs money at the backend, makes every query slow, and is
    the most common reason a metric layer has to be withdrawn. Identifiers belong on spans, where
    one value per trace is the point.
    """
    sink = RecordingSink()
    with use_sink(sink):
        record_retry(idempotency_mode="none", query_mode="none", posting_outcome="not_sent")
        record_approval(decision="approved", role="controller")
        record_quarantine(posting_outcome="unknown")
        record_dlq_depth(0)
        record_approval_latency(1.0)
        record_exception_latency(1.0)
        with instrument(
            Span.POST, _context(), sink=sink, duration_histogram=Metric.DISPATCH_LATENCY
        ):
            pass

    high_cardinality = {
        Attr.CORRELATION_ID.value,
        Attr.EXCEPTION_ID.value,
        Attr.OPERATION_ID.value,
        Attr.ADJUSTMENT_ID.value,
        Attr.BATCH_ID.value,
    }
    assert sink.measurements, "nothing was measured, so nothing was checked"
    for measurement in sink.measurements:
        assert not set(measurement.attributes) & high_cardinality, measurement


@pytest.mark.parametrize("seconds", [-0.001, -3600.0, float("nan"), float("inf")])
def test_a_negative_or_non_finite_latency_is_dropped_rather_than_clamped(seconds: float) -> None:
    """Two persisted timestamps can be ordered backwards by clock skew between two processes.

    Dropped rather than clamped to zero: a zero is a measurement claiming the stage was
    instantaneous, and a p95 computed with one in it is a number nobody can explain.
    """
    sink = RecordingSink()
    with use_sink(sink):
        record_approval_latency(seconds)
        record_exception_latency(seconds)
    assert sink.measurements == []


def test_a_negative_dlq_depth_is_refused() -> None:
    sink = RecordingSink()
    with use_sink(sink):
        record_dlq_depth(-1)
    assert sink.measurements == []


def test_seconds_between_is_the_signed_interval_and_the_recorders_judge_it() -> None:
    """One place computes an interval, one place decides whether it is usable."""
    import datetime as dt

    start = dt.datetime(2026, 6, 15, 9, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 6, 15, 9, 30, tzinfo=dt.UTC)
    assert seconds_between(start, end) == 1800.0
    assert seconds_between(end, start) == -1800.0


def test_the_dispatch_histogram_is_the_measured_duration_of_the_post_span() -> None:
    """§18's dispatch latency is send to outcome, which is exactly what that span brackets.

    Measured rather than supplied, because here the span's own wall time *is* the interval. The
    other two histograms cross process boundaries and cannot be a span duration.
    """
    sink = RecordingSink()
    with instrument(Span.POST, _context(), sink=sink, duration_histogram=Metric.DISPATCH_LATENCY):
        pass

    recorded = sink.values_of(Metric.DISPATCH_LATENCY)
    assert len(recorded) == 1
    assert recorded[0] >= 0.0
    assert sink.measurements[0].attributes == {Attr.STAGE.value: Span.POST.value}


# ======================================================================================
# 6. Instrumentation behaviour
# ======================================================================================


def test_the_decorator_is_a_single_line_and_lifts_the_recognised_identifiers() -> None:
    """One line above a definition, which is the whole adoption cost for a batch stage."""
    sink = RecordingSink()

    @instrumented(Span.MATCH)
    def run_matching(*, batch_id: uuid.UUID, ignored: str = "x") -> str:
        return "done"

    batch = uuid.UUID(int=9)
    with use_sink(sink):
        assert run_matching(batch_id=batch) == "done"

    span = sink.span_named(Span.MATCH.value)
    assert span.attributes[Attr.BATCH_ID.value] == str(batch)
    assert span.attributes[Attr.CORRELATION_SOURCE.value] == CorrelationSource.AMBIENT.value
    assert "ignored" not in json.dumps(span.attributes)


def test_the_decorator_wraps_the_await_of_a_coroutine_function_not_the_call() -> None:
    """A span that closed before the body ran would be microseconds long and correlated to nothing.

    The observable difference is that work done inside the coroutine happens while the span is
    open — asserted by logging from inside it and checking the line carries the span's id.
    """
    sink = RecordingSink()
    seen: list[str | None] = []

    @instrumented(Span.RECONCILE)
    async def reconcile_once(*, adjustment_id: uuid.UUID) -> int:
        await asyncio.sleep(0)
        from ledger_exception_control_plane.log import get_correlation_id

        seen.append(get_correlation_id())
        return 1

    with use_sink(sink):
        assert asyncio.run(reconcile_once(adjustment_id=uuid.UUID(int=3))) == 1

    span = sink.span_named(Span.RECONCILE.value)
    assert span.ended
    assert seen == [span.attributes[Attr.CORRELATION_ID.value]]


def test_a_failure_records_the_type_and_never_the_message() -> None:
    """``log.py`` omits tracebacks because they carry connection strings. A span does the same.

    The exception message here contains a DSN with a password in it — the shape a client library
    routinely produces — and the span must carry the class name only.
    """
    sink = RecordingSink()

    with (
        pytest.raises(ConnectionError),
        instrument(Span.POST, _context(), sink=sink) as recorder,
    ):
        recorder.set(Attr.ADAPTER_NAME, "simulated-none")
        raise ConnectionError(f"could not connect to {LEAK_DSN}")

    span = sink.span_named(Span.POST.value)
    assert span.attributes[Attr.ERROR_TYPE.value] == "ConnectionError"
    assert span.attributes[Attr.OUTCOME.value] == OUTCOME_FAILURE
    assert span.failed
    assert span.ended, "a raising body must still close the span"
    assert LEAK_PASSWORD not in json.dumps(span.attributes)
    assert LEAK_DSN not in json.dumps(span.attributes)


def test_the_outcome_defaults_to_success_and_the_body_can_override_it() -> None:
    """A stage that abstained or quarantined did not succeed, and the default must not win."""
    sink = RecordingSink()
    with instrument(Span.INGEST, _context(), sink=sink):
        pass
    with instrument(Span.RECONCILE, _context(), sink=sink) as recorder:
        recorder.outcome(OUTCOME_QUARANTINED)

    assert sink.span_named(Span.INGEST.value).attributes[Attr.OUTCOME.value] == OUTCOME_SUCCESS
    assert (
        sink.span_named(Span.RECONCILE.value).attributes[Attr.OUTCOME.value] == OUTCOME_QUARANTINED
    )


def test_the_default_sink_emits_nothing_and_an_instrumented_stage_still_runs() -> None:
    """An instrumented financial path must behave identically with no collector reachable."""
    assert isinstance(active_sink(), NullSink)

    ran = False
    with instrument(Span.COMPUTE_AMOUNT, _context()) as recorder:
        recorder.set(Attr.TREATMENT_CODE, "rebook")
        ran = True
    assert ran


def test_installing_a_sink_is_reversible() -> None:
    before = active_sink()
    sink = RecordingSink()
    with use_sink(sink):
        assert active_sink() is sink
    assert active_sink() is before


# ======================================================================================
# 7. The OpenTelemetry adapter, against a double for the API it calls
# ======================================================================================


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.status: object = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status: object) -> None:
        self.status = status


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: dict[str, _FakeSpan] = {}
        self.closed: list[str] = []

    def start_as_current_span(self, name: str) -> _FakeSpanContext:
        span = _FakeSpan()
        self.spans[name] = span
        return _FakeSpanContext(self, name, span)


class _FakeSpanContext:
    def __init__(self, tracer: _FakeTracer, name: str, span: _FakeSpan) -> None:
        self._tracer = tracer
        self._name = name
        self._span = span

    def __enter__(self) -> _FakeSpan:
        return self._span

    def __exit__(self, *_: object) -> None:
        self._tracer.closed.append(self._name)


class _FakeInstrument:
    def __init__(self, name: str, unit: str, description: str) -> None:
        self.name = name
        self.unit = unit
        self.description = description
        self.observations: list[tuple[float, Mapping[str, object]]] = []

    def add(self, amount: float, attributes: Mapping[str, object] | None = None) -> None:
        self.observations.append((amount, attributes or {}))

    def set(self, amount: float, attributes: Mapping[str, object] | None = None) -> None:
        self.observations.append((amount, attributes or {}))

    def record(self, amount: float, attributes: Mapping[str, object] | None = None) -> None:
        self.observations.append((amount, attributes or {}))


class _FakeMeter:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.instruments: dict[str, _FakeInstrument] = {}

    def _make(self, kind: str, name: str, unit: str, description: str) -> _FakeInstrument:
        self.created.append((kind, name))
        instrument_double = _FakeInstrument(name, unit, description)
        self.instruments[name] = instrument_double
        return instrument_double

    def create_counter(self, name: str, unit: str = "", description: str = "") -> _FakeInstrument:
        return self._make("counter", name, unit, description)

    def create_gauge(self, name: str, unit: str = "", description: str = "") -> _FakeInstrument:
        return self._make("gauge", name, unit, description)

    def create_histogram(self, name: str, unit: str = "", description: str = "") -> _FakeInstrument:
        return self._make("histogram", name, unit, description)


def test_the_adapter_forwards_attributes_and_closes_the_span() -> None:
    tracer, meter = _FakeTracer(), _FakeMeter()
    sink = OpenTelemetrySink(tracer=tracer, meter=meter, error_status="ERROR")

    with instrument(Span.APPROVE, _context(), sink=sink) as recorder:
        recorder.set(Attr.APPROVAL_DECISION, ApprovalDecision.APPROVED.value)

    span = tracer.spans[Span.APPROVE.value]
    assert span.attributes[Attr.CORRELATION_ID.value] == audit.correlation_id_for("a" * 64, 7)
    assert span.attributes[Attr.APPROVAL_DECISION.value] == ApprovalDecision.APPROVED.value
    assert tracer.closed == [Span.APPROVE.value]
    assert span.status is None, "a successful span must not be marked as an error"


def test_the_adapter_sets_the_injected_error_status_on_a_failure() -> None:
    tracer, meter = _FakeTracer(), _FakeMeter()
    sink = OpenTelemetrySink(tracer=tracer, meter=meter, error_status="ERROR")

    with pytest.raises(RuntimeError), instrument(Span.DLQ, _context(), sink=sink):
        raise RuntimeError("boom")

    assert tracer.spans[Span.DLQ.value].status == "ERROR"
    assert tracer.closed == [Span.DLQ.value]


def test_the_adapter_routes_each_kind_to_its_instrument_and_caches_it() -> None:
    """Kind, unit and description come from the declaration; instruments are created once.

    Creating an instrument per measurement re-declares the unit on every emission and is a
    documented way to end up with duplicate registrations.
    """
    tracer, meter = _FakeTracer(), _FakeMeter()
    sink = OpenTelemetrySink(tracer=tracer, meter=meter)

    with use_sink(sink):
        record_dead_letter()
        record_dead_letter()
        record_dlq_depth(7)
        record_approval_latency(2.0)

    assert meter.created == [
        ("counter", Metric.DLQ_ENTRIES.value),
        ("gauge", Metric.DLQ_DEPTH.value),
        ("histogram", Metric.APPROVAL_LATENCY.value),
    ]
    assert len(meter.instruments[Metric.DLQ_ENTRIES.value].observations) == 2
    assert meter.instruments[Metric.DLQ_DEPTH.value].observations == [(7.0, {})]
    assert meter.instruments[Metric.APPROVAL_LATENCY.value].unit == "s"
    assert meter.instruments[Metric.DLQ_ENTRIES.value].description == (
        spec_for(Metric.DLQ_ENTRIES).description
    )


def test_the_package_never_imports_the_sdk_by_statement() -> None:
    """The SDK is not a declared dependency, so a literal import would be a static failure.

    Probed through ``importlib`` instead, which is what makes the absence a runtime degradation to a
    no-op rather than an ``ImportError`` at startup.
    """
    for name, source in _sources().items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("opentelemetry"), name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("opentelemetry"), name


def test_configuring_telemetry_with_no_sdk_installed_yields_a_no_op() -> None:
    """The state of this repository today, asserted rather than assumed."""
    previous = active_sink()
    try:
        sink = configure_telemetry(service_name="lecp")
        if opentelemetry_api_is_installed():  # pragma: no cover - the API is not a dependency
            assert isinstance(sink, OpenTelemetrySink)
        else:
            assert isinstance(sink, NullSink)
    finally:
        from ledger_exception_control_plane.observability import install_sink

        install_sink(previous)


# ======================================================================================
# 8. The Langfuse configuration path
# ======================================================================================


def test_the_otlp_endpoint_is_derived_from_the_host() -> None:
    assert langfuse_otlp_endpoint("http://langfuse:3000") == "http://langfuse:3000/api/public/otel"
    assert langfuse_otlp_endpoint("https://lf.example/ ") == "https://lf.example/api/public/otel"


@pytest.mark.parametrize("host", ["langfuse:3000", "ftp://langfuse", ""])
def test_a_host_that_is_not_an_http_url_is_refused(host: str) -> None:
    with pytest.raises(LangfuseConfigurationError):
        langfuse_otlp_endpoint(host)


def test_a_host_with_embedded_credentials_is_refused_at_configuration_time() -> None:
    """An exporter logs its endpoint on startup, so a credential in the URL is a credential leaked.

    Refusing it here is better than redacting it afterwards, which is what
    :func:`redact_text` would then be doing.
    """
    with pytest.raises(LangfuseConfigurationError, match="must not embed credentials"):
        langfuse_otlp_endpoint(f"https://public:{LEAK_PASSWORD}@langfuse.example")


def test_an_unconfigured_environment_produces_no_export_target() -> None:
    """Not exporting is the normal case — the test suite, a developer's machine, CI."""
    assert langfuse_target_from_environment({}) is None
    assert langfuse_target_from_environment({"LANGFUSE_HOST": "http://langfuse:3000"}) is None


def test_the_authorization_header_is_wrapped_and_never_rendered() -> None:
    """base64 is an encoding, not encryption, so the header is as sensitive as the key inside it."""
    header = langfuse_authorization_header("pk-lf-public", SecretStr(LEAK_PASSWORD))
    assert LEAK_PASSWORD not in repr(header)
    assert LEAK_PASSWORD not in str(header)
    assert header.get_secret_value().startswith("Basic ")

    target = langfuse_target_from_environment(
        {
            "LANGFUSE_HOST": "http://langfuse:3000",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-public",
            "LANGFUSE_SECRET_KEY": LEAK_PASSWORD,
        }
    )
    assert target is not None
    assert target.endpoint == "http://langfuse:3000/api/public/otel"
    assert LEAK_PASSWORD not in repr(target), "the target renders its own credential"


def test_only_variable_names_are_committed_never_values() -> None:
    """§17. The documentation and the code carry names; a deployment supplies the values."""
    assert all(name.isupper() and " " not in name for name in ENVIRONMENT_VARIABLES)
    langfuse_source = (OBSERVABILITY_ROOT / "langfuse.py").read_text(encoding="utf-8")
    for shape in ("pk-lf-", "sk-lf-", "sk-ant-", "eyJ"):
        assert shape not in langfuse_source, f"{shape!r} appears in the module"


# ======================================================================================
# 9. Firewalls this package must respect
# ======================================================================================


def test_the_package_imports_no_orm_no_engine_no_provider_and_no_corpus() -> None:
    """Instrumentation must not drag the world in behind it.

    Two reasons, both concrete. An import cycle: every module this package imports is a module that
    might want to import *it*, and the financial core is exactly that set. And the fixture-truth
    firewall — nothing in the decision path may import ``fixtures``, and telemetry sits alongside
    the decision path in every module it will eventually be called from.
    """
    forbidden = {
        "sqlalchemy",
        "asyncpg",
        "redis",
        "alembic",
        "fastapi",
        "starlette",
        "openai",
        "anthropic",
        "litellm",
        "instructor",
        "langchain",
        "naive",
    }
    for name, source in _sources().items():
        for node in ast.walk(ast.parse(source)):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert module.split(".")[0] not in forbidden, f"{name} imports {module}"
                assert "fixtures" not in module.split("."), f"{name} imports the corpus: {module}"


def test_importing_the_package_alone_loads_no_orm() -> None:
    """The import-graph claim, checked in a fresh interpreter rather than inferred from statements.

    A transitive import would satisfy the syntax scan above and still pull SQLAlchemy into every
    process that instruments anything — so the claim is tested the only way it can be proven.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ledger_exception_control_plane.observability as o; "
            "print(','.join(sorted(m for m in ('sqlalchemy', 'fastapi', 'asyncpg', 'redis') "
            "if m in sys.modules)) or 'clean'); assert o.Span.POST",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert result.stdout.strip() == "clean", result.stdout


def test_the_redaction_gate_has_no_bypass() -> None:
    """One caller of ``start_span``, and two of ``record``. A gate with a bypass is a comment.

    Walked as syntax over the package: ``instrument`` is the only thing that opens a span, and the
    only routes to a measurement are ``metrics._emit`` and ``instrument``'s duration histogram —
    both of which redact first.
    """
    openers = {
        name: source.count("start_span(")
        for name, source in _sources().items()
        if ".start_span(" in source
    }
    assert openers == {"observability/instrumentation.py": 1}, openers

    recorders = sorted(
        name
        for name, source in _sources().items()
        if "target.record(" in source or "sink.record(" in source
    )
    assert recorders == ["observability/instrumentation.py", "observability/metrics.py"], recorders

    for name in recorders:
        assert "redact_attributes" in _sources()[name], f"{name} records without redacting"


def test_no_module_in_the_package_is_named_for_a_forbidden_concept() -> None:
    """``outbox`` and ``workers`` stay forbidden at any depth, including here."""
    stems = {path.stem for path in OBSERVABILITY_ROOT.rglob("*.py")}
    assert not stems & {"outbox", "workers"}
    assert "logging" not in stems, "a module shadowing a stdlib name is a debugging trap"
