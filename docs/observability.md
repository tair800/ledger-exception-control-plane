# Observability

`PROJECT_SPEC.md` §18, increment M8.1. This document is the contract: the span, attribute and
metric vocabulary, what is measured and what deliberately is not, the redaction rule, the
self-hosted Langfuse configuration, and the exact one-line hooks the rest of the package adopts.

The conventions here are one of the five things `CLAUDE.md` records this repository as owing the
rest of the portfolio. Projects 4 and 6 **copy** them — they do not import them, because
repositories stay independent — so everything below is stated as a rule with its reason attached
rather than as a description of the code.

---

## 1. What the package is, and what it is not

`src/ledger_exception_control_plane/observability/` holds eight modules:

| Module | Responsibility |
|---|---|
| `conventions.py` | Span names, attribute keys, metric declarations, and the fields this system cannot know |
| `redaction.py` | The single gate every attribute and log field passes through (§16, §17) |
| `context.py` | The identity a span carries; there is no constructor without a correlation id |
| `runtime.py` | Where telemetry goes: a null sink (the default), a recording sink, an OpenTelemetry adapter |
| `instrumentation.py` | `instrument()`, `@instrumented`, and `SpanRecorder` — the only writer to an open span |
| `metrics.py` | One named function per §18 metric |
| `langfuse.py` | Environment-variable names → OTLP endpoint and authorisation header |
| `__init__.py` | The public surface |

It is **not** an observability platform. It configures no provider, starts no exporter thread,
opens no socket, adds no dependency, and holds no state beyond the installed sink. There is no
sampling policy, no dashboard code and no trace store, because §18 asks for none of those and every
one of them would be complexity justified by how advanced it looks rather than by the problem.

The package imports with **no ORM, no engine, no HTTP framework and no provider SDK** in its graph.
Two reasons, both concrete: every module this package imports is a module that may want to import
*it*, and the financial core is exactly that set — so a dependency there is an import cycle waiting
for the first hook. And the fixture-truth firewall keeps `fixtures` out of the decision path, which
telemetry sits alongside in every module it will be called from. A test asserts the syntax and a
second test asserts the *transitive* graph in a fresh interpreter, because a transitive import would
satisfy the first and still pull SQLAlchemy into every process that instruments anything.

---

## 2. Span names

Twelve, closed, and checked against the audit contract. Nine of §11's ten `tool` verbs name a stage
that also deserves a span and the tenth (`approve`) does too, so the span set is the audit verbs
prefixed `lecp.`, plus the two §18 flows that emit no audit event — ingestion and classification,
deliberately unaudited under ADR-058 because neither is ledger-affecting. A span is then the only
signal those two have beyond their own tables.

A test asserts the two sets agree in both directions, so a verb cannot arrive without a span and a
span cannot be invented for a stage that does not exist.

| Span | Stage | Audit verb |
|---|---|---|
| `lecp.ingest` | One settlement payload accepted, hashed, normalised | — (ADR-058) |
| `lecp.match` | One deterministic matching pass | `match` |
| `lecp.classify` | One classification pass over the residuals | — (ADR-058) |
| `lecp.propose_treatment` | One treatment proposal attributed to a model | `propose_treatment` |
| `lecp.approve` | One human approval decision | `approve` |
| `lecp.compute_amount` | The deterministic amount calculation | `compute_amount` |
| `lecp.post` | One posting attempt against a ledger adapter | `post` |
| `lecp.retry` | Scheduling a further attempt | `retry` |
| `lecp.dlq` | Writing a dead letter | `dlq` |
| `lecp.replay` | An operator replaying a dead letter | `replay` |
| `lecp.reconcile` | Querying a ledger about an undetermined operation | `reconcile` |
| `lecp.recover` | An operator judging an ambiguous posting | `recover` |

---

## 3. Attribute keys

Namespaced `lecp.*`, except published OpenTelemetry conventions and **one deliberate exception**:
`correlation_id` is unprefixed, spelled exactly as `log.py` spells the log field and as
`audit_event` spells the column. It is the join key across the three signals, and three spellings of
a join key is what makes cross-signal correlation quietly stop working.

### Identity — on every span

| Key | Meaning |
|---|---|
| `correlation_id` | Required. Present on every span, and on every log line emitted inside one |
| `lecp.correlation_source` | `artefact` or `ambient` — see §4 |
| `lecp.exception_id` | Where the stage knows one |
| `lecp.operation_id` | §12's stable identifier: a SHA-256 digest, so it discloses nothing |
| `lecp.adjustment_id` | Where the stage knows one |
| `lecp.batch_id` | Where the stage is batch-scoped |
| `lecp.stage` | The span name, repeated so a metric or a flattened log line can carry it |

An identifier the stage does not know is **omitted**, never sent as null: an attribute set to null
is not an absent attribute, it is an assertion that the value is null, and a dashboard cannot tell
that apart from a stage that failed to populate it.

### Outcome

| Key | Values |
|---|---|
| `lecp.outcome` | `success` / `failure` / `abstained` / `quarantined` — §11's own vocabulary |
| `lecp.error_type` | The exception **class name**. Never the message; see §6 |

### Domain

| Key | Values |
|---|---|
| `lecp.treatment_code` | `rebook` / `accrue` / `write_off` / `escalate` |
| `lecp.confidence_band` | `low` / `medium` / `high` — a band, because the response contract carries no numeric type |
| `lecp.abstained` | boolean |
| `lecp.classification` | The classification the deterministic engine assigned |
| `lecp.adapter.name` | The adapter an attempt went to |
| `lecp.adapter.idempotency_mode` | `none` / `accepts_key` / `enforces_key` |
| `lecp.adapter.query_mode` | `none` / `by_operation_id` |
| `lecp.attempt_no` | Which send this was, the first included |
| `lecp.posting_outcome` | `confirmed` / `rejected` / `throttled` / `unknown` / `partially_applied` / `not_sent` |
| `lecp.query_answer` | `found` / `not_found` / `indeterminate` |
| `lecp.approval.decision` | `approved` / `rejected` / `edited` |
| `lecp.approval.role` | The role the approval token resolved to (§16 role separation) |
| `lecp.recovery.reason` | Why an ambiguous posting reached manual recovery |
| `lecp.recovery.resolution` | How an operator resolved it |

`lecp.posting_outcome` is kept distinct from `lecp.outcome` on purpose: the audit reading is lossy by
construction — both `unknown` and `partially_applied` read as `quarantined` — and the difference
between them is the whole subject of §13.5.

### Model attribution — `lecp.propose_treatment` only

| Key | Value | Truthful because |
|---|---|---|
| `gen_ai.provider.name` | `anthropic` / `openai` | It is the adapter family in use |
| `gen_ai.request.model` | The configured model id | It is the model the deployment configured, the contract the answer was validated against, and the id persisted on `treatment_proposal` |
| `gen_ai.operation.name` | `chat` | It names the **request shape** the adapter builds, not evidence that a request was sent |
| `lecp.model.version` | The model version | §11 requires both halves; no published convention names a caller-declared version distinct from a response model |
| `lecp.model_call.origin` | `replayed` / `live` | Always `replayed` here. This is what stops a GenAI span implying a network call that never happened |

`gen_ai.system` is the superseded spelling of `gen_ai.provider.name` and is deliberately **not** also
emitted: two keys for one fact is how a dashboard ends up double-counting.

### Bookkeeping

| Key | Meaning |
|---|---|
| `lecp.not_recorded` | Field names this system cannot know, with reasons in `UNRECORDED_REASONS` |
| `lecp.redacted` | Attribute keys the redaction gate removed. Accumulated across the whole span |

---

## 4. Two kinds of correlation id, and why the span says which

This system already has two, and conflating them would be a lie.

* **Artefact-derived** — `audit.correlation_id_for(content_hash, line_number)` produces
  `lecp:<hash>:<line>`. It is a property of the data, so any stage can compute it, it is stable
  across a crash, and it is the id that **joins to `audit_event`**.
* **Ambient** — `api.py`'s middleware binds a request-scoped id from a header or generates one. A
  stage driven from a CLI has neither until something binds one. It joins log lines to spans within
  one process and to nothing outside it.

Both belong on a span, so both are admitted and the span records which under
`lecp.correlation_source`. That makes §18's *"generated at ingestion, propagated through every
layer"* a claim a query can check rather than one a docstring asserts.

`config.is_valid_correlation_id` is deliberately **not** reused to validate these. That policy
governs an untrusted inbound HTTP header and admits only `[A-Za-z0-9_-]` — an alphabet the canonical
artefact-derived id fails, because it contains colons. Reusing it would have replaced the one
correlation id that joins to the audit trail with a marker. The check applied instead is narrower in
purpose and wider in alphabet: printable, no whitespace, no control characters, at most 128
characters — the log-injection characters and nothing else. A value that fails becomes
`lecp-correlation-invalid`, a fixed greppable marker, rather than raising: telemetry that can fail a
financial operation is worse than telemetry that loses an id, which is the same choice `api.py`
makes for a malformed header and `audit.py` makes for an unwalkable chain.

**A correlation id on every log line, including outside an HTTP request.** `log.py` renders
`correlation_id` from a context variable that only the HTTP middleware binds, so a stage driven from
a CLI — a matching pass, a retry pass, the replay command — logs `correlation_id: null` today.
`instrument()` binds the scope for the duration of the span, which fixes the log line at the same
time. A test asserts both states, because a correction whose "before" nobody can see is a correction
nobody can review.

---

## 5. Metrics

§18 names five counted things and three distributions, and that is the whole set. Nothing is added:
an invented metric is an invented claim.

| Metric | Instrument | Unit | Recorded by |
|---|---|---|---|
| `lecp.retries` | counter | `{retry}` | `record_retry(idempotency_mode=…, query_mode=…, posting_outcome=…)` |
| `lecp.dlq.entries` | counter | `{entry}` | `record_dead_letter()` |
| `lecp.dlq.depth` | **gauge** | `{entry}` | `record_dlq_depth(depth)` |
| `lecp.approvals` | counter | `{approval}` | `record_approval(decision=…, role=…)` |
| `lecp.abstentions` | counter | `{abstention}` | `record_abstention()` |
| `lecp.quarantines` | counter | `{quarantine}` | `record_quarantine(posting_outcome=…)` |
| `lecp.approval.latency` | histogram | `s` | `record_approval_latency(seconds)` |
| `lecp.dispatch.latency` | histogram | `s` | `instrument(Span.POST, …, duration_histogram=…)` |
| `lecp.exception.latency` | histogram | `s` | `record_exception_latency(seconds)` |

### One correction to §18, stated rather than hidden

§18 lists *DLQ depth* under "Counters". It is a **level**: a counter named "depth" can only ever
rise, which is precisely wrong for a queue that drains. It is therefore a gauge, and the monotonic
half of the same question — how many dead letters have ever been written — is a separate counter
named for what it measures. `record_dlq_depth` has no default and refuses a negative: a zero from a
pass that never queried is indistinguishable from an empty queue.

### No metric carries a high-cardinality label

Not `correlation_id`, not `lecp.exception_id`, not `lecp.adjustment_id`, not `lecp.operation_id`.
Those belong on spans, where one value per trace is the point. On a metric they create one time
series per settlement line, which costs money at the backend, makes every query slow, and is the
single most common reason a first observability increment has to be withdrawn. A test asserts no
measurement carries one.

### No function in `metrics.py` reads a clock

Approval latency and end-to-end exception latency cross process boundaries and can span hours, so
they can only come from persisted timestamps the caller already holds — the same discipline every
business path here follows, where `received_at`, `matched_at` and `sent_at` are parameters because
on a replay the correct value is not now. `seconds_between(start, end)` computes the interval;
the recorders **drop** a negative or non-finite one rather than clamping it, because two persisted
timestamps can be ordered backwards by clock skew between two processes and a zero is a measurement
claiming the stage was instantaneous.

Dispatch latency is the exception: §18 defines it as send to outcome recorded, which is exactly what
the `lecp.post` span brackets, so it is the span's own measured wall time.

---

## 6. The redaction rule

§16: *"No secrets in logs or traces; merchant identifiers redacted in telemetry."*

Every attribute passes `redact_attributes` before it reaches a sink, and there is no other route: a
test asserts that `instrument()` is the only caller of `sink.start_span` in the package and that the
only two callers of `sink.record` both redact first. A gate with a bypass is a comment.

**Three mechanisms, because one is not enough.**

1. **Denied keys are dropped.** An amount, a merchant reference and an approval token look like
   ordinary values; the key is the only thing that identifies them. Matched whole-segment *and* by
   substring, after `lecp.` and `gen_ai.` are stripped — whole-segment matching alone admits
   `merchant_ref`, `approval.token_sha` and `provider_api_key`, which are exactly what the fence
   exists to stop. Denied: `amount`, `currency`, `rationale`, `evidence`, `merchant_reference`,
   `psp_reference`, `posting_ref`, `token`, `dsn`, `password`, `secret`, `authorization`, `api_key`,
   `cookie`, and anything containing `merchant`, `bearer`, `credential`, `apikey`.
2. **String values are scrubbed by pattern.** A DSN password
   (`scheme://user:password@host` → `scheme://user:[redacted]@host`, keeping the scheme, user and
   host, which are useful and are not credentials), bearer tokens, and the credential shapes of six
   providers. Values are also capped at 256 characters: a span attribute is a label, never a
   payload, and free text is how a secret reaches an aggregator even after pattern redaction.
3. **Types that cannot appear are refused.** A `Decimal` is dropped whatever key it arrives under,
   because the only `Decimal` in this system is a ledger amount and the key is the part a call site
   chooses (`lecp.value`, `lecp.delta`, `lecp.total` are all plausible and none is in the deny
   list). `bytes` is dropped as a payload. Anything exposing `get_secret_value` is dropped
   **without being read** — relying on another library's `__str__` for a security property is
   relying on a decision somebody else may revisit. Nested mappings are dropped: flatten at the
   call site or leave it out.

**Every removal is recorded.** The dropped keys go on the span under `lecp.redacted`, accumulated
across the whole span. Key names are not sensitive, and without them a removal is
indistinguishable from a value nobody set — which would make the gate unfalsifiable from outside.

**A merchant reference is dropped, not pseudonymised.** A stable pseudonym for a merchant reference
is still a merchant identifier, and one low enough in entropy to invert from a dictionary. §16 says
redacted, and the only redaction that is not also a re-identification risk is removal. Grouping
telemetry by merchant would need a deliberate, separately-decided pseudonymisation, and nothing has
decided one — **open question**, recorded here rather than resolved silently.

**The honest limit of the guarantee.** Value-level merchant detection is not attempted: a merchant
reference is indistinguishable from any other short opaque string, and a heuristic that tried would
either miss or redact the adapter names and operation identifiers a trace is followed by. The
guarantee is therefore structural — **no attribute key in the vocabulary is one that could carry an
amount, a merchant reference or a credential**, and a test runs every `Attr` member through the
fence to prove it. A caller that put a merchant reference into `lecp.adapter.name` would defeat
this, and no reviewable design prevents that; what is prevented is a *designated* field for one.

**An error records its type and never its message.** A client library's exception text routinely
embeds the request URL, and an authorisation header or a key in a query string travels with it.
`log.py` omits tracebacks for the same reason, and the OpenTelemetry adapter deliberately never
calls `record_exception`, which would write the formatted traceback into a span event.

**`redact_text` is duplicated rather than imported from `llm.cassette`.** That function is correct
for what it guards and wrong here in both directions: it does not touch a DSN password or a merchant
reference — a test in the cassette suite asserts merchant text *survives*, because an evidence pack
is meant to carry it — and it lives in a package whose import graph pulls in the ORM. Portfolio
conventions are copied, not imported.

---

## 7. What is measured, and what is not

### Not measured, with the reason on record

§18 asks for *"token usage, estimated cost and processing region as span attributes"* on every model
call. **No model call is made anywhere in this repository**: `llm/port.py` ships no transport, no
provider SDK is a dependency, and every committed cassette is marked synthesised — a state a test
enforces. So those three, plus §11's agent identity, are declared absent **by name with a reason**,
which is the same discipline `provenance._gaps` applies to §11's two null fields.

| Field | Why it is not recorded |
|---|---|
| `gen_ai.usage.input_tokens` | No call, so no request to count tokens in. A synthesised cassette carries no usage field, and a test enforces that: a zero would read as *"this call was free"* rather than as an absence |
| `gen_ai.usage.output_tokens` | Same — there is no completion |
| `gen_ai.usage.cost` | Derived from usage and a provider price, and the usage half does not exist. An estimate from an assumed token count is an invented metric, which `CLAUDE.md` rule 10 forbids. Cost per 1,000 lines comes from the measurement harness, from provider usage fields, and only when there are provider usage fields |
| `gen_ai.processing.region` | §11 defines it as the processing region of the model call. No call is made. The region a deployment *declares* is a statement about where calls would be processed; stamping it on a span would describe a request that never happened (ADR-058) |
| `gen_ai.agent.id` | §2 states this system is not an agent and §11 admits a null `agent_identity` for deterministic steps. The model proposes a treatment code and takes no action. **Permanent**, not pending a transport |

The mechanism is not a comment. `SpanRecorder.mark_unrecorded(*fields)` puts the names on the span
and **raises** for a field with no reason on record — a call site allowed to invent an absence would
give the list the one property it exists to prevent. No attribute key in the vocabulary contains
`usage`, `token`, `cost`, `region` or `jurisdiction`, so no later call site can populate one without
adding a key here and justifying it.

When a live transport ships, each entry is deleted and the attribute takes its place.

### Measured, and honestly

Span durations are real `perf_counter` readings of the wrapped block. The three §18 histograms are
described in §5. Nothing else in this package produces a number.

---

## 8. Adoption — the exact hooks

Every hook below is a **single line**. `@instrumented(span)` uses the ambient correlation id and
lifts identifiers from the call by parameter name, restricted to a closed set —
`exception_id`, `operation_id`, `adjustment_id`, `batch_id`. `instrument()` is the context-manager
form for a stage that knows which settlement line it is about or wants to record an outcome from
inside the body.

Add `from ledger_exception_control_plane.observability import …` to each module that adopts one.

| File | Function | Line to add |
|---|---|---|
| `api.py` | `create_app` | `configure_telemetry(service_name=resolved.service_name)`, immediately after the `configure_logging(...)` call |
| `ingest/service.py` | `ingest` | `@instrumented(Span.INGEST)` |
| `matching/service.py` | `run_matching` | `@instrumented(Span.MATCH)` — lifts `batch_id` |
| `classification/service.py` | `run_classification` | `@instrumented(Span.CLASSIFY)` — lifts `batch_id` |
| `llm/service.py` | `propose_for_exception` | `@instrumented(Span.PROPOSE_TREATMENT)` — lifts `exception_id` |
| `money/calculator.py` | `compute_adjustment` | `@instrumented(Span.COMPUTE_AMOUNT)` |
| `operations/approval.py` | `record_decision` | `@instrumented(Span.APPROVE)` — lifts `exception_id` |
| `operations/dispatcher.py` | `dispatch_once` | `@instrumented(Span.POST, duration_histogram=Metric.DISPATCH_LATENCY)` — lifts `adjustment_id` |
| `operations/retry.py` | `attempt_one` | `@instrumented(Span.RETRY)` — lifts `adjustment_id` |
| `operations/retry.py` | `dead_letter` | `@instrumented(Span.DLQ)` — lifts `adjustment_id` |
| `operations/retry.py` | `replay_dead_letter` | `@instrumented(Span.REPLAY)` |
| `operations/reconcile.py` | `reconcile_once` | `@instrumented(Span.RECONCILE)` — lifts `adjustment_id` |
| `operations/recovery.py` | `open_item` | `@instrumented(Span.RECOVER)` — lifts `adjustment_id` |
| `operations/recovery.py` | `resolve_item` | `@instrumented(Span.RECOVER)` |

The metric calls are one line each at the site that already knows the value:

| File | Function | Line to add |
|---|---|---|
| `operations/retry.py` | `attempt_one`, on the bounded-retry path | `record_retry(idempotency_mode=…, query_mode=…, posting_outcome=…)` |
| `operations/retry.py` | `dead_letter` | `record_dead_letter()` |
| `operations/retry.py` | `run_due_once`, at the end of the pass | `record_dlq_depth(len(await pending_dead_letters(session)))` |
| `operations/approval.py` | `record_decision`, after the decision is recorded | `record_approval(decision=decision.value, role=principal.role.value)` |
| `llm/service.py` | `propose_for_exception`, on the abstention path | `record_abstention()` |
| `operations/dispatcher.py` | `dispatch_once`, where the outcome is `UNKNOWN` or `PARTIALLY_APPLIED` | `record_quarantine(posting_outcome=code.value)` |
| `operations/approval.py` | `record_decision` | `record_approval_latency(seconds_between(exception.created_at, now))` |
| `operations/reconcile.py` / `operations/recovery.py` | wherever a posting reaches a terminal state | `record_exception_latency(seconds_between(received_at, resolved_at))` |

Richer attribution — the treatment code on a proposal span, the posting outcome on a dispatch span,
the GenAI attribute set — needs the context-manager form, because the values are only known inside
the body:

```python
with instrument(
    Span.PROPOSE_TREATMENT,
    TelemetryContext.artefact(correlation_id, exception_id=exception_id),
    attributes=proposal_attributes(
        provider=proposer.provider.value,
        model_id=proposer.model_id,
        model_version=proposer.model_version,
    ),
) as span:
    span.mark_unrecorded(*UNRECORDED_REASONS)
    ...
    span.set(Attr.TREATMENT_CODE, proposal.treatment.value)
    span.outcome(OUTCOME_ABSTAINED if proposal.abstained else OUTCOME_SUCCESS)
```

---

## 9. Dependencies

**None are added by this increment.** The package imports and its whole test suite passes with no
OpenTelemetry SDK installed, because the sink is an injected handle behind a narrow protocol with a
no-op default — the same seam `llm/port.py` uses for a provider and `ledger/port.py` for a ledger.

To export for real, add three packages:

```bash
uv add opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
```

* `opentelemetry-api` — `get_tracer`, `get_meter`, `StatusCode`. What `runtime.py` probes for.
* `opentelemetry-sdk` — the `TracerProvider` and `MeterProvider` the application installs at
  startup. A library must not install a global provider; two of them is a silent outage.
* `opentelemetry-exporter-otlp-proto-http` — OTLP over HTTP, which is what Langfuse ingests.

Pin the exact versions `uv` resolves, in the existing style: every dependency in `pyproject.toml` is
exact-pinned so the lockfile is the single source of truth and an upgrade is a reviewable change.
**No version is asserted here**, because no version can be verified from this branch — with one
constraint to check against whatever `uv` resolves: the adapter calls `Meter.create_gauge`, the
synchronous gauge instrument, which older API releases do not have. If the resolved API predates it,
`lecp.dlq.depth` is the only metric affected and `runtime.py` is the only file that changes.

### What is proven and what is not

`OpenTelemetrySink` is tested against a hand-written double of the API surface it calls. That proves
the adapter's logic — instrument caching, unit and description propagation, attribute forwarding,
the counter/gauge/histogram split, the injected error status. It does **not** prove
interoperability with the real SDK, and nothing in this repository can until the dependency is
declared and a run reaches a collector. §18's exit criterion — *a single exception traceable end to
end in Langfuse* — is therefore **not discharged**; what remains is the dependency, the provider
bootstrap in `api.py`, and one observed run.

---

## 10. Self-hosted Langfuse

Langfuse ingests OpenTelemetry over OTLP/HTTP, so there is no Langfuse client here: `langfuse.py`
turns three environment variables into an endpoint and an authorisation header, and the OTLP
exporter does the rest. Keeping the integration that small is the point — a vendor SDK in the
instrumentation layer is a vendor SDK every later project inherits.

### Environment variables — names only

§17: only names are committed, in code and in documentation. A deployment supplies the values.

| Variable | Meaning |
|---|---|
| `LANGFUSE_HOST` | Where the instance is reachable, e.g. `http://langfuse:3000` |
| `LANGFUSE_PUBLIC_KEY` | The project's public key |
| `LANGFUSE_SECRET_KEY` | The project's secret key. A credential |
| `OTEL_SERVICE_NAME` | The service name spans are attributed to |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Set by the bootstrap from `LANGFUSE_HOST`, or directly for another backend |
| `OTEL_EXPORTER_OTLP_HEADERS` | The authorisation header, if the exporter is configured by environment rather than in code |
| `OTEL_SDK_DISABLED` | The SDK's own off switch |

**None of these is `LECP_`-prefixed, deliberately.** `Settings` declares `env_prefix="LECP_"` with
`extra="forbid"`, so any `LECP_`-named variable that is not a declared field makes startup fail —
documenting one in `.env.example` would break the app for anybody who copied it into `.env`. The
cassette harness hit exactly this and named its switch `CASSETTE_CAPTURE` for the same reason. Using
the vendor's and OpenTelemetry's own names also makes them recognisable to an operator who has
configured either before.

`langfuse.py` refuses a host with credentials embedded in it (`https://user:key@host`). An exporter
logs its endpoint on startup and a container dumps its environment, so a credential in the URL is a
credential leaked; refusing it at configuration time is better than redacting it afterwards. The
Basic header is returned as a `SecretStr`, because base64 is an encoding rather than encryption and
the header is exactly as sensitive as the key inside it.

### The Compose service block

**Proposed, not yet exercised.** No `docker` command was run on this branch, so the block below has
not been started and the image tag has not been pulled. It is written for review, not presented as
verified. Add to `docker-compose.yml` under `services:`:

```yaml
  # --- self-hosted Langfuse (M8.1, §18) -----------------------------------------
  # Its own database rather than a second schema in the app's: Langfuse runs its own
  # migrations at startup, and pointing them at the database that holds `audit_event`
  # would put a third party's DDL next to an append-only financial table.
  langfuse-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse_local_dev # dev-only placeholder; see the note at the top
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse -d langfuse"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 10s

  langfuse:
    image: langfuse/langfuse:2
    depends_on:
      langfuse-db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://langfuse:langfuse_local_dev@langfuse-db:5432/langfuse
      NEXTAUTH_URL: http://localhost:13000
      # No default and no committed value. `:?` makes Compose fail loudly with this
      # message rather than starting with a predictable secret, which is the failure
      # mode a default would produce.
      NEXTAUTH_SECRET: ${LANGFUSE_NEXTAUTH_SECRET:?generate one locally; never commit a value}
      SALT: ${LANGFUSE_SALT:?generate one locally; never commit a value}
      TELEMETRY_ENABLED: "false" # no usage reporting out of a developer's machine
    ports:
      # Off the default port, for the same reason PostgreSQL and Redis are, and bound
      # to localhost only.
      - "127.0.0.1:13000:3000"
```

Add one line to the existing `volumes:` block at the end of the file:

```yaml
  langfuse_data:
```

And three lines to the `app` service's `environment:` block. The keys are passed rather than a
pre-built header, so the credential is assembled in the process and never appears in a Compose file
or a shell history:

```yaml
      LANGFUSE_HOST: http://langfuse:3000
      LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:-}
      LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY:-}
```

**Why the `2` tag.** Langfuse v3 splits into a web tier and a worker and adds ClickHouse, Redis and
MinIO — six containers to see one trace, on a stack whose whole selling point is that it comes up
with `docker compose up` and needs no third-party account (NFR-7). v2 is one container plus a
database. If v3 is wanted later, that is a deliberate decision about the local stack's weight, not a
detail of this increment.

### Bringing it up

1. `docker compose up -d langfuse-db langfuse`
2. Open `http://localhost:13000`, create the local account and a project.
3. Copy the project's public and secret keys into `.env` under `LANGFUSE_PUBLIC_KEY` and
   `LANGFUSE_SECRET_KEY`. `.env` is git-ignored; `.env.example` carries the names with empty
   placeholders and nothing else.
4. Restart the app. With the SDK installed and a provider configured, `configure_telemetry` returns
   an `OpenTelemetrySink`; without it, a `NullSink`, and the application behaves identically.

---

## 11. Verification

```bash
make observability-verify   # the conventions, the redaction gate and the correlation contract
```

The suite needs no database, no network, no provider and no OpenTelemetry SDK, so it runs in the
default `uv run pytest`. Four assertions carry the increment, and each can fail:

* the span set is checked against §11's audit verbs, in both directions;
* the leak test asserts a DSN password, a bearer token and a merchant reference are gone **and**
  that the non-sensitive attributes survive — with a kill test beside it that hands the same sink
  the same secret with the gate bypassed and asserts the secret appears, because otherwise a passing
  leak test is equally consistent with a sink that emits nothing;
* a log line emitted inside a span carries the span's correlation id, and one emitted outside it
  carries `null` — both asserted, so the correction is visible;
* `mark_unrecorded` refuses a field with no reason on record.
