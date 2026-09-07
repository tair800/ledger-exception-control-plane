"""Span names, attribute keys and metric names as committed data, not string literals.

`PROJECT_SPEC.md` §18 asks for OpenTelemetry spans, five counters and three histograms.
`CLAUDE.md` says this repository owes the portfolio *"OpenTelemetry → self-hosted Langfuse
conventions — span naming, token and cost attributes"*, and projects 4 and 6 copy them. A
convention that lives as a quoted string at each call site is not a convention: it is nine
spellings waiting to happen, and the copy that lands in project 4 will be whichever one the author
last read.

So every name is a member of a closed enumeration here, every metric carries its kind, unit and
description in one place, and the fields this system **cannot** know are enumerated with their
reasons rather than filled in with plausible zeroes.

**Span names mirror the audit verbs.** §11 fixes ten ``tool`` values; nine of them name a stage that
also deserves a span, and the two §18 flows that emit no audit event — ingestion and classification,
deliberately unaudited under ADR-058 — get one each. A test asserts every
:class:`~ledger_exception_control_plane.db.control.AuditTool` has a span, so a new verb cannot
arrive without a span name and the two signals cannot drift apart. The enumerations are *not*
imported from ``db.control``: this package must import with no ORM, no engine and no provider SDK
in its graph, and a test asserts the vocabularies match instead.

**One attribute is deliberately not namespaced.** ``correlation_id`` is spelled exactly as
``log.py`` spells the log field and as ``audit_event`` spells the column, because it is the join key
across the three signals and three spellings of a join key is the defect that makes cross-signal
correlation quietly not work. Everything else is namespaced ``lecp.*`` or is a published
OpenTelemetry semantic convention.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Final

__all__ = [
    "ATTRIBUTE_DENY_LIST",
    "GEN_AI_ATTRIBUTES",
    "METRIC_SPECS",
    "SPAN_ATTRIBUTE_MAX_LENGTH",
    "UNRECORDED_REASONS",
    "Attr",
    "AttributeValue",
    "CorrelationSource",
    "Metric",
    "MetricKind",
    "MetricSpec",
    "ModelCallOrigin",
    "Span",
    "spec_for",
]

#: What a span attribute or a metric label may hold.
#:
#: Narrower than OpenTelemetry's own type union on purpose. ``Decimal`` is absent and is refused at
#: the boundary rather than coerced, because the only ``Decimal`` in this system is a ledger amount
#: and a monetary amount has exactly one owner (§7, and the audit contract's own field fence). A
#: coercion here would put a money value into telemetry under whichever key happened to hold it.
type AttributeValue = str | bool | int | float | tuple[str, ...]

#: Longest string a single attribute may carry before it is truncated.
#:
#: A span attribute is a label, never a payload. Free text — a provider message, an evidence pack, a
#: traceback — is how a secret reaches an aggregator even after pattern redaction, so length is
#: capped independently of content.
SPAN_ATTRIBUTE_MAX_LENGTH: Final = 256


class Span(enum.StrEnum):
    """Every span this system emits. Closed, and a test proves the set against the audit verbs.

    Named for the stage rather than the module, so a refactor that moves ``run_matching`` does not
    rename a span that dashboards and Langfuse views are keyed on.
    """

    #: One settlement payload accepted, hashed and normalised (FR-1). Emits no audit event —
    #: ingestion is not ledger-affecting, ADR-058 — so this span is the only signal for the stage
    #: beyond its own tables.
    INGEST = "lecp.ingest"

    #: One deterministic matching pass over the eligible lines (§18, audit verb ``match``).
    MATCH = "lecp.match"

    #: One classification pass over the residuals. Unaudited by ADR-058, for the same reason as
    #: ingestion.
    CLASSIFY = "lecp.classify"

    #: One treatment proposal attributed to a model. The only span carrying GenAI attributes, and
    #: the only span with a ``not_recorded`` list (audit verb ``propose_treatment``).
    PROPOSE_TREATMENT = "lecp.propose_treatment"

    #: One human approval decision (audit verb ``approve``).
    APPROVE = "lecp.approve"

    #: The deterministic amount calculation (audit verb ``compute_amount``).
    COMPUTE_AMOUNT = "lecp.compute_amount"

    #: One posting attempt against a ledger adapter (audit verb ``post``).
    POST = "lecp.post"

    #: Scheduling a further attempt after an allowlisted transport failure (audit verb ``retry``).
    RETRY = "lecp.retry"

    #: Writing a dead letter after a bounded retry ran out (audit verb ``dlq``).
    DLQ = "lecp.dlq"

    #: An operator replaying a dead letter (audit verb ``replay``).
    REPLAY = "lecp.replay"

    #: Asking a ledger what happened to an operation whose outcome is undetermined (audit verb
    #: ``reconcile``).
    RECONCILE = "lecp.reconcile"

    #: An operator judging what happened to an ambiguous posting (audit verb ``recover``).
    RECOVER = "lecp.recover"


class CorrelationSource(enum.StrEnum):
    """Where a span's ``correlation_id`` came from, recorded because the two are not equivalent.

    ``audit.py`` derives the canonical id from the ingested artefact — content hash plus line
    number — so it is stable across a crash and identical in every stage that touches that line.
    The HTTP middleware in ``api.py`` binds a *request-scoped* id instead, and a batch stage driven
    from a CLI has neither until something binds one.

    Both are real correlation ids and both belong on a span. What would be dishonest is letting a
    reader assume the second kind joins to an ``audit_event`` row, so the span says which it is. It
    also makes §18's *"generated at ingestion, propagated through every layer"* a claim a query can
    check rather than one a docstring asserts.
    """

    #: Derived from the ingested artefact by ``audit.correlation_id_for``. Joins to ``audit_event``.
    ARTEFACT = "artefact"

    #: The request- or run-scoped id bound by the HTTP middleware or by an instrumented entry point.
    #: Joins log lines to spans within one process, and to nothing outside it.
    AMBIENT = "ambient"


class ModelCallOrigin(enum.StrEnum):
    """Whether the proposal on this span came from a replayed recording or a live call.

    Mirrors ``llm.cassette.Origin``, and exists for the same reason it does: a span carrying GenAI
    attributes reads as evidence that a provider was called, and in this repository no provider is
    called — no transport ships, no provider SDK is a dependency, and every committed cassette is
    marked synthesised. Recording the origin is what stops the span implying a network request that
    never happened.
    """

    #: Served from a committed cassette. No bytes crossed a network.
    REPLAYED = "replayed"

    #: Obtained from a provider over the wire. No code path in this repository produces this value
    #: today; it exists so the attribute does not have to change shape when a transport ships.
    LIVE = "live"


class Attr(enum.StrEnum):
    """Every attribute key. Namespaced ``lecp.*`` except the join key and published conventions."""

    # -- identity ---------------------------------------------------------------------
    #: Deliberately unprefixed, matching the ``log.py`` field and the ``audit_event`` column.
    #: Required on every span; see the module docstring.
    CORRELATION_ID = "correlation_id"

    #: Which kind of correlation id the span carries. See :class:`CorrelationSource`.
    CORRELATION_SOURCE = "lecp.correlation_source"

    #: The exception under treatment, where the stage knows one.
    EXCEPTION_ID = "lecp.exception_id"

    #: §12's stable operation identifier — a SHA-256 hex digest, so it discloses nothing.
    OPERATION_ID = "lecp.operation_id"

    #: The adjustment being posted, where the stage knows one.
    ADJUSTMENT_ID = "lecp.adjustment_id"

    #: The ingestion batch, where the stage is batch-scoped.
    BATCH_ID = "lecp.batch_id"

    #: The :class:`Span` member, repeated as an attribute so a flattened log line or a metric can
    #: carry the same stage label the span is named for.
    STAGE = "lecp.stage"

    # -- outcome ----------------------------------------------------------------------
    #: ``success`` / ``failure`` / ``abstained`` / ``quarantined`` — the audit outcome vocabulary,
    #: reused rather than reinvented so a span and its audit row read the same.
    OUTCOME = "lecp.outcome"

    #: The exception *type* of a failure. Never the message: a client library's exception text
    #: routinely embeds a request URL, and ``log.py`` omits tracebacks for exactly that reason.
    ERROR_TYPE = "lecp.error_type"

    # -- classification and proposal --------------------------------------------------
    #: One of the closed treatment codes. Not an amount, and there is no field here that could hold
    #: one.
    TREATMENT_CODE = "lecp.treatment_code"

    #: ``low`` / ``medium`` / ``high``. A band, not a number — the response contract carries no
    #: numeric type anywhere in its tree.
    CONFIDENCE_BAND = "lecp.confidence_band"

    #: Whether the model declined to propose.
    ABSTAINED = "lecp.abstained"

    #: The exception classification the deterministic engine assigned.
    CLASSIFICATION = "lecp.classification"

    # -- model attribution ------------------------------------------------------------
    #: The model version, paired with ``gen_ai.request.model``. No published convention names a
    #: caller-declared version distinct from the model a response echoes back, and §11 requires both
    #: halves — a bare model id cannot distinguish two versions that behave differently.
    MODEL_VERSION = "lecp.model.version"

    #: See :class:`ModelCallOrigin`.
    MODEL_CALL_ORIGIN = "lecp.model_call.origin"

    #: The §18 and §11 fields this span could not fill truthfully, by name. See
    #: :data:`UNRECORDED_REASONS`.
    NOT_RECORDED = "lecp.not_recorded"

    #: Attribute keys the redaction layer removed, by name. The names are not sensitive; recording
    #: them is what makes a removal visible instead of indistinguishable from "never set".
    REDACTED = "lecp.redacted"

    # -- ledger adapter and dispatch --------------------------------------------------
    #: The adapter the attempt went to, by name.
    ADAPTER_NAME = "lecp.adapter.name"

    #: ``none`` / ``accepts_key`` / ``enforces_key``. §19 requires every scenario to be read per
    #: capability, so a dispatch metric that cannot be split by capability answers the wrong
    #: question.
    ADAPTER_IDEMPOTENCY_MODE = "lecp.adapter.idempotency_mode"

    #: ``none`` / ``by_operation_id``.
    ADAPTER_QUERY_MODE = "lecp.adapter.query_mode"

    #: Which send this was, the first included.
    ATTEMPT_NO = "lecp.attempt_no"

    #: ``confirmed`` / ``rejected`` / ``throttled`` / ``unknown`` / ``partially_applied`` /
    #: ``not_sent``. Kept distinct from :attr:`OUTCOME` because the audit reading is lossy by
    #: construction: both ``unknown`` and ``partially_applied`` read as ``quarantined``, and the
    #: difference is the whole subject of §13.5.
    POSTING_OUTCOME = "lecp.posting_outcome"

    #: The answer a reconciliation query returned: ``found`` / ``not_found`` / ``indeterminate``.
    QUERY_ANSWER = "lecp.query_answer"

    # -- human decisions --------------------------------------------------------------
    #: ``approved`` / ``rejected`` / ``edited``.
    APPROVAL_DECISION = "lecp.approval.decision"

    #: The role the approval token resolved to. §16's control is role separation, so a trail that
    #: cannot say which kind of principal decided cannot show that an operator never approved.
    APPROVAL_ROLE = "lecp.approval.role"

    #: Why an ambiguous posting reached manual recovery.
    RECOVERY_REASON = "lecp.recovery.reason"

    #: How an operator resolved a recovery item.
    RECOVERY_RESOLUTION = "lecp.recovery.resolution"

    # -- OpenTelemetry GenAI semantic conventions -------------------------------------
    #: The provider family the adapter speaks to. Current semantic-convention key; the earlier
    #: ``gen_ai.system`` is its superseded spelling and is deliberately not emitted as well, because
    #: two keys for one fact is how a dashboard ends up double-counting.
    GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"

    #: The model asked for. Truthful here: it is the model the deployment configured, the contract
    #: the answer was validated against, and the id persisted on ``treatment_proposal``.
    GEN_AI_REQUEST_MODEL = "gen_ai.request.model"

    #: The operation kind the adapter builds a request for. ``chat`` — a structured-output chat
    #: completion. It names the *request shape*, not evidence that a request was sent; that fact is
    #: :attr:`MODEL_CALL_ORIGIN`.
    GEN_AI_OPERATION_NAME = "gen_ai.operation.name"


#: The GenAI attributes the proposal span carries. Every one has a truthful value here.
#:
#: The published convention names more — ``gen_ai.usage.input_tokens``,
#: ``gen_ai.usage.output_tokens``, ``gen_ai.response.model``, and the cost and region §18 asks for.
#: None of those is knowable in this repository, so none of them is emitted; they are listed in
#: :data:`UNRECORDED_REASONS` instead. Absence is the only encoding that cannot be mistaken for a
#: measurement, which is the same rule the cassette harness applies to usage fields.
GEN_AI_ATTRIBUTES: Final[tuple[Attr, ...]] = (
    Attr.GEN_AI_PROVIDER_NAME,
    Attr.GEN_AI_REQUEST_MODEL,
    Attr.GEN_AI_OPERATION_NAME,
)


#: What this system cannot know, and why. Recorded rather than filled in.
#:
#: §18 asks for *"token usage, estimated cost and processing region as span attributes"*. All three
#: describe a model call, and **no model call is made anywhere in this repository**: ``llm/port.py``
#: ships no transport, no provider SDK is a dependency, and every committed cassette is marked
#: synthesised — a state a test enforces. A zero for token usage does not read as "nobody measured
#: this", it reads as "this call was free", and that zero would flow into a published figure.
#:
#: This is the same discipline ``provenance._gaps`` applies to §11's two null fields, and it is
#: applied here for the same reason: a field a contract requires and a deployment cannot supply must
#: be named, not blanked, or its absence reads as "not applicable".
#:
#: Keyed by the attribute name the field *would* have, so the day a live transport ships the entry
#: is deleted and the attribute takes its place.
UNRECORDED_REASONS: Final[dict[str, str]] = {
    "gen_ai.usage.input_tokens": (
        "no model call is made in this repository — no transport ships, no provider SDK is a "
        "dependency, and every committed cassette is marked synthesised. A synthesised recording "
        "carries no usage field, and a test enforces that, because a zero would read as a "
        "measurement rather than as an absence."
    ),
    "gen_ai.usage.output_tokens": (
        "same reason as gen_ai.usage.input_tokens: there is no completion to count tokens in."
    ),
    "gen_ai.usage.cost": (
        "cost is derived from token usage and a provider price, and the usage half does not exist. "
        "An estimate computed from an assumed token count would be an invented metric, which "
        "CLAUDE.md rule 10 forbids outright. Cost per 1,000 lines is produced by the measurement "
        "harness from provider usage fields, and only when there are provider usage fields."
    ),
    "gen_ai.processing.region": (
        "§11 defines region_jurisdiction as the processing region of the model call. No call is "
        "made, so there is no region. The region a deployment declares is a statement about where "
        "calls would be processed, and stamping it on a span would describe a request that never "
        "happened (ADR-058, and audit.py leaves the same field null for the same reason)."
    ),
    "gen_ai.agent.id": (
        "§2 states this system is not an agent and §11 admits a null agent_identity for "
        "deterministic steps. The model proposes a treatment code and takes no action, so there is "
        "no agent to identify. This entry is permanent, not pending a transport."
    ),
}


class MetricKind(enum.StrEnum):
    """The instrument a metric is recorded through."""

    #: Monotonic. Only ever added to.
    COUNTER = "counter"

    #: A level, set to whatever it currently is. Not monotonic and not summable across time.
    GAUGE = "gauge"

    #: A distribution of observations.
    HISTOGRAM = "histogram"


class Metric(enum.StrEnum):
    """§18's five counters and three histograms, and nothing else.

    §18 lists *"Counters: retries, DLQ depth, approvals, abstentions, quarantines"* and
    *"Histograms: approval latency, dispatch latency, end-to-end exception latency"*. That is the
    whole set. Nothing is added here — an invented metric is an invented claim, and this file is
    copied by two later projects.

    **One correction to §18, stated rather than hidden.** *DLQ depth* is a level, not a monotonic
    count, so it is a gauge; a counter named "depth" can only ever go up, which is precisely wrong
    for a queue that drains. The monotonic half of the same question — how many dead letters have
    ever been written — is genuinely a counter, so both exist and are named for what they measure.
    """

    #: Retries scheduled (§18 "retries").
    RETRIES = "lecp.retries"

    #: Dead letters written. Monotonic.
    DLQ_ENTRIES = "lecp.dlq.entries"

    #: Dead letters currently awaiting replay (§18 "DLQ depth"). A level.
    DLQ_DEPTH = "lecp.dlq.depth"

    #: Human approval decisions recorded (§18 "approvals"), split by decision and role.
    APPROVALS = "lecp.approvals"

    #: Proposals where the model declined (§18 "abstentions").
    ABSTENTIONS = "lecp.abstentions"

    #: Postings held aside for a decision rather than decided (§18 "quarantines").
    QUARANTINES = "lecp.quarantines"

    #: Exception raised → approval recorded (§18 "approval latency").
    APPROVAL_LATENCY = "lecp.approval.latency"

    #: Send → outcome recorded (§18 "dispatch latency").
    DISPATCH_LATENCY = "lecp.dispatch.latency"

    #: Payload received → posting resolved (§18 "end-to-end exception latency").
    EXCEPTION_LATENCY = "lecp.exception.latency"


@dataclasses.dataclass(frozen=True, slots=True)
class MetricSpec:
    """Everything an exporter needs to declare one instrument."""

    metric: Metric
    kind: MetricKind
    unit: str
    description: str


#: Seconds, not milliseconds, for every duration.
#:
#: OpenTelemetry's convention for a duration is seconds, and a histogram exported under a unit its
#: backend does not expect is a chart with the wrong axis. ``log.py`` renders ``duration_ms`` on its
#: request line and that stays as it is: a human reads a log line, a backend reads a metric, and the
#: unit each wants is different.
_SECONDS: Final = "s"


METRIC_SPECS: Final[dict[Metric, MetricSpec]] = {
    Metric.RETRIES: MetricSpec(
        metric=Metric.RETRIES,
        kind=MetricKind.COUNTER,
        unit="{retry}",
        description=(
            "Further attempts scheduled after an allowlisted transport failure. Split by adapter "
            "capability, because §19 requires every dispatch question to be readable per "
            "capability."
        ),
    ),
    Metric.DLQ_ENTRIES: MetricSpec(
        metric=Metric.DLQ_ENTRIES,
        kind=MetricKind.COUNTER,
        unit="{entry}",
        description="Dead letters written after a bounded retry ran out. Monotonic.",
    ),
    Metric.DLQ_DEPTH: MetricSpec(
        metric=Metric.DLQ_DEPTH,
        kind=MetricKind.GAUGE,
        unit="{entry}",
        description=(
            "Dead letters awaiting replay. A level: recorded from a count the caller already "
            "holds, never inferred, and never emitted as zero by a caller that did not look."
        ),
    ),
    Metric.APPROVALS: MetricSpec(
        metric=Metric.APPROVALS,
        kind=MetricKind.COUNTER,
        unit="{approval}",
        description=(
            "Human approval decisions recorded, split by decision and by the role the token "
            "resolved to."
        ),
    ),
    Metric.ABSTENTIONS: MetricSpec(
        metric=Metric.ABSTENTIONS,
        kind=MetricKind.COUNTER,
        unit="{abstention}",
        description=(
            "Proposals where the model declined to propose a treatment. An abstention is a "
            "first-class answer, not a failure, and is counted separately for that reason."
        ),
    ),
    Metric.QUARANTINES: MetricSpec(
        metric=Metric.QUARANTINES,
        kind=MetricKind.COUNTER,
        unit="{quarantine}",
        description=(
            "Postings held aside for a decision rather than decided — an undetermined or partially "
            "applied outcome. Split by posting outcome, because folding the two together loses the "
            "distinction §13.5 exists to preserve."
        ),
    ),
    Metric.APPROVAL_LATENCY: MetricSpec(
        metric=Metric.APPROVAL_LATENCY,
        kind=MetricKind.HISTOGRAM,
        unit=_SECONDS,
        description=(
            "Exception raised to approval recorded. Computed by the caller from the two timestamps "
            "it already holds; this layer reads no clock for it."
        ),
    ),
    Metric.DISPATCH_LATENCY: MetricSpec(
        metric=Metric.DISPATCH_LATENCY,
        kind=MetricKind.HISTOGRAM,
        unit=_SECONDS,
        description=(
            "Send to outcome recorded, for one attempt. Measured as the duration of the "
            "lecp.post span, which is exactly the interval in question."
        ),
    ),
    Metric.EXCEPTION_LATENCY: MetricSpec(
        metric=Metric.EXCEPTION_LATENCY,
        kind=MetricKind.HISTOGRAM,
        unit=_SECONDS,
        description=(
            "Payload received to posting resolved. Spans process boundaries and can span hours, so "
            "it can only come from persisted timestamps the caller supplies — never from a span "
            "duration."
        ),
    ),
}


def spec_for(metric: Metric) -> MetricSpec:
    """The declaration for one metric. Total over :class:`Metric`, and a test proves it.

    Total rather than defaulted, for the reason every mapping in this project is: a new metric must
    be given a kind and a unit deliberately, and a ``dict.get`` default would export the next one as
    whatever the fallback happened to be.
    """
    try:
        return METRIC_SPECS[metric]
    except KeyError:  # pragma: no cover - the test above makes this unreachable
        raise ValueError(
            f"{metric!r} has no declaration; give it a kind, a unit and a description rather than "
            "letting an exporter guess"
        ) from None


#: Attribute keys that may never carry a value into telemetry, matched whole and case-insensitively
#: after the ``lecp.`` and ``gen_ai.`` namespaces are stripped.
#:
#: The first six are the audit contract's own fence, applied to the second signal: §11 carries no
#: amount and no evidence because the financial facts live in ``adjustment`` and the reasoning in
#: ``treatment_proposal``, and a span duplicating either would be a second copy of a value with
#: exactly one owner. The rest are §16 and §17 — *"No secrets in logs or traces; merchant
#: identifiers redacted in telemetry"*.
#:
#: **A denied key is dropped, not scrubbed.** A merchant reference replaced by a stable pseudonym
#: would still be a merchant identifier, and one low enough in entropy to invert from a dictionary;
#: §16 says redacted, and the only redaction that is not also a re-identification risk is removal.
#: Grouping telemetry by merchant would need a deliberate, separately-decided pseudonymisation, and
#: nothing has decided one.
ATTRIBUTE_DENY_LIST: Final[frozenset[str]] = frozenset(
    {
        "amount",
        "currency",
        "rationale",
        "evidence",
        "merchant_reference",
        "psp_reference",
        "posting_ref",
        "token",
        "dsn",
        "password",
        "secret",
        "authorization",
        "api_key",
        "cookie",
    }
)
