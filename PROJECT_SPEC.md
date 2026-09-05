# PROJECT_SPEC — ledger-exception-control-plane

Authoritative implementation specification. Derived from the approved entry in
`portfolio-control/PORTFOLIO_BLUEPRINT.md` (Project 1, flagship) and bound by
`portfolio-control/PORTFOLIO_PROGRESS.md`.

**Status: specification. Nothing here is implemented.**

---

## 1. Problem statement

A payments-heavy marketplace receives daily settlement files from its payment service provider (PSP)
and must reconcile them against its general ledger. Deterministic matching with tolerance bands
clears the great majority of lines. A residual does not match: partial captures, fee splits,
chargeback reversals, FX rounding, refunds crossing a period boundary.

That residual is resolved manually at month-end. Two failure modes are expensive and silent:

1. **Double-posting.** A retry fires after a ledger call that actually succeeded, and the adjustment
   is posted twice. Discovered months later, after it has flowed into reported revenue.
2. **Wrong-account posting.** An adjustment is booked against the wrong account because the decision
   rested on evidence nobody recorded.

The system exists to ensure it never *initiates* a duplicate financial write under injected failure;
that where the adapter contract in §13.4 is met, no duplicate is *applied*; that where the adapter does
not meet it, the system degrades safely and visibly rather than guessing; and that the second failure
mode is auditable. The distinction matters: not-initiating is this system's property, not-applied is
partly the counterparty's.

## 2. Scope

In scope:

- Ingestion of PSP settlement files and ledger snapshots through adapters over simulated sources.
- Deterministic normalisation, matching and tolerance handling.
- Residual exception creation, classification and evidence assembly.
- A model-proposed **treatment code** with rationale and evidence references.
- A mandatory human approval gate on every ledger-affecting write.
- Deterministic amount computation.
- Effectively-once financial side effects **conditional on the declared ledger adapter contract**
  (§13), built on a retry-independent operation identifier, a unique constraint and a transactional
  outbox — with explicit `UNKNOWN` handling and manual recovery where the contract is insufficient.
- Bounded retry with exponential backoff and jitter, dead-letter queue, and a replay CLI.
- Append-only audit trail with end-to-end correlation ids.
- An operations console for triage, approval, provenance and DLQ.
- A committed chaos suite run against both a naive RED baseline and the real implementation.
- A committed evaluation harness gating CI on recorded cassettes.
- Cost and latency measurement produced by committed scripts.

## 3. Non-goals

Stated explicitly so scope creep is visible:

- **Not** a general ledger. The ledger is an external system behind an adapter.
- **Not** a bank or PSP connector suite. One simulated PSP feed, one simulated ledger API.
- **Not** an agent. There is no tool-calling loop, no planner, no autonomous action.
- **Not** a matching-algorithm research project. Matching is competent and conventional.
- **Not** multi-tenant. One organisation, one ledger.
- **Not** a close-management or financial-reporting product.
- **Not** an ERP integration. No SAP, NetSuite or Oracle connector.
- **No** fine-tuning, training or model hosting.
- **No** auto-posting without human approval, at any confidence level, ever.
- **No** currency conversion policy engine. FX rates arrive as recorded inputs.

## 4. Functional requirements

**FR-1 Ingestion.** Accept a settlement batch through a signed webhook and a file drop. Persist the
raw payload immutably with a content hash before parsing. Re-delivery of an identical batch must not
create duplicate work.

**FR-2 Normalisation.** Parse settlement lines into a typed internal representation with explicit
currency on every monetary value. Reject structurally invalid batches into quarantine with a reason.

**FR-3 Matching.** Match settlement lines to ledger entries deterministically, with configurable
tolerance bands. Record for every line which rule matched it, or that none did.

**FR-4 Exception creation.** Create an exception for each residual line, classified into a closed
taxonomy (partial capture, fee split, chargeback reversal, FX rounding, cross-period refund,
unclassified).

**FR-5 Evidence assembly.** Assemble the evidence available for an exception — dispute reason text,
merchant memo, support-ticket notes, remittance references, candidate ledger entries — as addressable
records with stable ids.

**FR-6 Treatment proposal.** The model proposes exactly one treatment code from a closed enumeration,
with a free-text rationale and a list of evidence references, or abstains.

**FR-7 Human approval.** Every exception requires an explicit human decision — approve, edit, reject —
before any ledger write. Approval is attributable to a named principal and is recorded.

**FR-8 Amount computation.** On approval, the adjustment amount is computed deterministically from
settlement and ledger data, given the approved treatment code.

**FR-9 Posting.** The adjustment is claimed under a retry-independent `operation_id` (§12.1) and
dispatched to the ledger adapter through a transactional outbox. The adapter returns a three-valued
outcome (`CONFIRMED` / `REJECTED` / `UNKNOWN`); the outcome and any posting reference are recorded.

**FR-10 Retry and DLQ.** Dispatches failing with an **allowlisted transport error before any byte was
written** (§15) retry with bounded exponential backoff and jitter, then move to a dead-letter queue
with the full envelope, reason and attempt count. Every other failure defaults to `UNKNOWN`, which is
**not** an ordinary retry case and follows §13.5 instead. The word "provably" is deliberately avoided:
nothing at the client proves non-arrival, so the classifier is enumerated rather than asserted.

**FR-11 Replay and recovery.** A CLI replays DLQ entries. Replay must not create a second ledger effect
for an adjustment already `CONFIRMED`. Where an outcome is `UNKNOWN` and adapter capability does not
permit safe re-send or reconciliation, the operation is routed to a **manual-recovery queue** and is
never automatically replayed.

**FR-12 Audit.** Every state transition emits an append-only audit event under the contract in §11.

**FR-13 Operations console.** Exception queue with filters, exception detail with full provenance,
approval flow, DLQ view with replay, and a demonstrable fault-injection control in demo mode.

**FR-15 Adapter capability declaration.** Every ledger adapter declares `idempotency`,
`posting_identity_query` and `reversal` capabilities (§13.4). The dispatcher branches on the declared
capability, never on inferred behaviour, and an adapter that cannot express an `UNKNOWN` outcome is
rejected by a contract test.

**FR-14 Three-arm comparison.** A committed harness runs the same labelled exception set through the
deterministic matcher, an LLM-as-matcher baseline and the shipped hybrid, reporting accuracy, USD per
1,000 lines and p95.

## 5. Non-functional requirements

- **NFR-1** Typed Python throughout; `mypy` clean in CI.
- **NFR-2** All boundary schemas are Pydantic v2 models with `extra="forbid"`.
- **NFR-3** Money is `Decimal` with explicit quantisation and rounding mode. `float` is banned for
  monetary values and this is enforced by test.
- **NFR-4** Migrations apply and roll back cleanly.
- **NFR-5** Health and readiness endpoints.
- **NFR-6** Structured JSON logging with correlation id on every record.
- **NFR-7** The full stack starts from `docker compose up` with no third-party account.
- **NFR-8** The evaluation gate runs in CI with no live API key.
- **NFR-9** Deterministic seeding — the same seed produces the same fixture corpus.
- **NFR-10** No secret ever appears in logs, traces or the repository.
- **NFR-11** Graceful degradation: if the model provider is unavailable, exceptions still queue for
  human treatment. Provider unavailability must never block or corrupt the deterministic path.

## 6. AI responsibility boundary

The model is responsible for exactly one thing: **reading assembled evidence for one exception and
proposing one treatment code, with a rationale and evidence references, or abstaining.**

The model is explicitly **not** responsible for, and structurally cannot perform:

- computing, proposing, inferring, modifying or emitting any monetary amount;
- deciding whether to post;
- selecting a ledger account;
- matching lines;
- determining tolerance;
- any write of any kind.

### 6.1 Structural containment — the schema

The response model must satisfy all of the following, enforced by a CI guard test:

- **No numeric type anywhere in the schema tree.** No `int`, `float`, `Decimal`; no JSON Schema
  property of type `number` or `integer`; no numeric enum values. Confidence is expressed as a closed
  band (`LOW` / `MEDIUM` / `HIGH`), not a number.
- **`extra="forbid"`** on every model in the tree, so the model cannot introduce a field.
- **Evidence references are opaque string identifiers** of records the system already holds. They
  carry no values.
- **Field names are checked against an amount-like pattern list** (`amount`, `value`, `total`, `sum`,
  `qty`, `quantity`, `rate`, `pct`, `percent`, `balance`, `delta`, `fee`, `price`, `cost`) and the
  test fails if any appears.

Intended shape, to be implemented and then locked by the guard test:

```
TreatmentProposal
  treatment       : TreatmentCode        # closed enum: REBOOK | ACCRUE | WRITE_OFF | ESCALATE
  confidence      : ConfidenceBand       # closed enum: LOW | MEDIUM | HIGH
  rationale       : str                  # human-readable provenance only
  evidence_refs   : list[EvidenceRef]    # EvidenceRef = { evidence_id: str }
  abstained       : bool
```

### 6.2 Structural containment — the function boundary

The schema alone is insufficient, because `rationale` is free text and free text can contain digits.
Containment therefore also holds at the call boundary:

```
compute_adjustment(
    exception   : Exception,        # system-owned, deterministic
    treatment   : TreatmentCode,    # the ONLY value derived from model output
    ledger_ctx  : LedgerContext,    # system-owned, deterministic
) -> Money
```

`compute_adjustment` has **no parameter through which model free text can flow**. `rationale` is
persisted and displayed, never parsed, never tokenised for numbers, never used in a branch. This is
asserted by test: the calculator module must not import the proposal model, and a static check
enforces that.

**Consequence to state plainly in the README:** a hallucinated amount is not "unlikely" or "caught in
review" — there is no representation for it and no path for it.

## 7. Deterministic financial responsibility boundary

Everything touching money is deterministic, pure where possible, and unit-tested:

- normalisation and currency handling;
- matching and tolerance bands;
- **amount computation**, given the approved treatment code;
- rounding, quantisation and sign;
- account selection, from a configured mapping;
- period assignment;
- idempotency key derivation;
- posting, retry, DLQ and replay.

Rules:

- `Decimal` only, with explicit quantisation and rounding mode recorded alongside the result.
- Currency explicit on every value; no cross-currency arithmetic without an explicitly recorded rate.
- If a treatment cannot be priced deterministically for a given exception, the outcome is **escalate**.
  Guessing is a defect, not a fallback.
- The calculator is pure: same inputs, same output, no I/O, no clock, no randomness.

## 8. Expected workflow

1. Settlement batch arrives (signed webhook or file drop); raw payload persisted with a content hash.
2. Batch parsed and normalised; invalid batches quarantined with a reason.
3. Deterministic matching runs; matched lines recorded with the rule that matched them.
4. Residual lines become exceptions, classified into the closed taxonomy.
5. Evidence assembled and addressed.
6. Model proposes a treatment code with rationale and evidence references, or abstains.
7. Exception surfaces in the analyst queue with full provenance.
8. Analyst approves, edits or rejects. Nothing proceeds without a decision.
9. On approval, the amount is computed deterministically.
10. A posting is claimed under an idempotency key; state change and outbox row are written in one
    transaction.
11. The dispatcher sends to the ledger adapter with the idempotency key; provider response recorded.
12. On failure: bounded retry with backoff and jitter; then DLQ.
13. Audit events are emitted throughout; correlation id spans the whole path.

## 9. Data model direction

Indicative, not final; settled at M1.

| Entity | Purpose |
|---|---|
| `settlement_batch` | Raw payload, content hash, source, received-at, status |
| `settlement_line` | Normalised line, currency, references, match state |
| `ledger_entry` | Snapshot rows available for matching |
| `match_result` | Line ↔ entry, matching rule id, tolerance applied |
| `exception` | Residual line, classification, status, correlation id |
| `evidence` | Addressable evidence record attached to an exception |
| `treatment_proposal` | Model output, model id and version, prompt hash, cassette id |
| `approval` | Decision, principal, timestamp, edited treatment if any |
| `adjustment` | Computed amount, currency, account, period, `operation_id`, posting reference |
| `outbox` | Pending dispatch intent, attempt count, next-attempt-at, last outcome (`CONFIRMED`/`REJECTED`/`UNKNOWN`) |
| `posting_attempt` | Write-ahead record of one dispatch attempt: `operation_id`, attempt number, sent-at, `IN_FLIGHT`/resolved, outcome (§12.1.1) |
| `dlq` | Failed envelope, reason, attempts, replay state |
| `recovery_queue` | `UNKNOWN` outcomes awaiting reconciliation or an operator decision (§13.5) |
| `audit_event` | Append-only, contract v1 (§11) |

Constraints that carry the guarantee:

- `adjustment.operation_id` — **unique** (§12.1, retry-independent).
- `exception` claim via `SELECT … FOR UPDATE SKIP LOCKED`.
- `settlement_batch.content_hash` — unique, so re-delivery is a no-op.
- `posting_attempt` — unique `(adjustment_id, attempt_no)`, committed **before** the socket write (§12.1.1).
- `audit_event` — append-only. Enforced by a trigger that refuses `UPDATE`, `DELETE` and `TRUNCATE`
  from **any** role including the table owner; the insert-only application grant is defence in depth
  rather than the primary control, because a grant does not constrain the owner (ADR-026).

## 10. API direction

FastAPI, versioned under `/api/v1`. Indicative surface:

- `POST /webhooks/settlement` — signed, verified, idempotent on content hash.
- `GET /exceptions` — filter by status, classification, batch.
- `GET /exceptions/{id}` — full provenance: evidence, proposal, approval, adjustment, audit trail.
- `POST /exceptions/{id}/approve` · `/reject` — requires an authenticated principal; returns the
  claimed idempotency key.
- `GET /dlq` · `POST /dlq/{id}/replay`.
- `GET /recovery` · `POST /recovery/{id}/resolve` — the manual-recovery queue for `UNKNOWN` outcomes
  (§13.5). Resolution requires an authenticated principal and is recorded as an approval-class audit
  event.
- `GET /healthz` · `GET /readyz`.

### 10.1 Ledger adapter interface direction

Shape only — **not implemented at this stage**. It exists here so the contract in §13.4 is
representable before any code is written, and so no adapter can be added that hides an `UNKNOWN`.

```
LedgerAdapterCapabilities
    idempotency            : IdempotencyMode      # NONE | ACCEPTS_KEY | ENFORCES_KEY
    idempotency_window     : Duration | UNBOUNDED # how long the provider retains the key
    idempotency_scope      : GLOBAL | PER_ENDPOINT | PER_ACCOUNT
    posting_identity_query : PostingQueryMode     # NONE | BY_OPERATION_ID
    query_consistency      : LINEARIZABLE | EVENTUAL(visibility_bound: Duration)
    max_inflight_window    : Duration             # how long a sent request may still commit
    atomicity              : ATOMIC | NON_ATOMIC  # are multi-leg postings all-or-nothing?
    reversal               : ReversalMode         # NONE | VOID | COMPENSATING   (RESERVED)

PostingOutcome  (closed — no boolean success anywhere)
    Confirmed(posting_ref: str)
    Rejected(reason: str)                         # the ledger declined it; nothing applied
    Throttled(retry_after: Duration)              # NOT a declination; split out so Rejected has one meaning
    Unknown(detail: str)                          # sent, outcome undetermined
    PartiallyApplied(applied_legs, posting_refs)  # NON_ATOMIC adapters only; always manual recovery

QueryOutcome  (closed, three-valued — symmetric with PostingOutcome by design)
    Found(posting_ref: str)
    NotFound
    Indeterminate(detail: str)                    # query failed, or the answer is not yet trustworthy

LedgerAdapter (port)
    capabilities()  -> LedgerAdapterCapabilities
    post(operation_id: str, instruction: PostingInstruction) -> PostingOutcome
    get_by_operation_id(operation_id: str) -> QueryOutcome
        # present only when posting_identity_query == BY_OPERATION_ID;
        # absent capability is a typed absence, not a method that raises at runtime
```

Constraints on any future implementation:

- `post` **must** be able to return `Unknown`. An adapter whose return type cannot express it is
  inadmissible, and a contract test rejects it.
- **The query is three-valued for the same reason the post outcome is.** A two-valued
  `Confirmed | None` would conflate *never applied*, *applied but not yet visible* (read-after-write
  lag, replica lag, an async posting pipeline) and *still in flight*. Treating that `None` as "not
  applied" and re-sending is a textbook double-post, and it would reintroduce one layer down exactly
  the defect this section bans at the post boundary.
- `operation_id` is supplied by the caller, never generated inside the adapter, so it cannot acquire a
  retry-dependent component.
- Capability is **declared data**, not inferred from behaviour. The dispatcher branches on
  `capabilities()`, so the recovery path is chosen by contract rather than by exception type.
- **Declaration is not evidence.** Before `ENFORCES_KEY` or `BY_OPERATION_ID` may be declared for any
  adapter, a **capability conformance suite** must pass against it: post the same `operation_id` twice
  and assert the applied-count is 1; query a known posting and assert it is returned. The conformance
  run and its date are recorded in the repository. An undeclared or unverified capability is treated
  as `NONE`.
- `reversal` is **RESERVED and consumed by nothing today.** No dispatcher branch, no §14 row, no §19
  scenario and no acceptance criterion reads it. It remains a placeholder for the operator-initiated
  unwind path in OPEN-12 and must not be cited as a capability the system acts on.
- Demo-mode only: `POST /demo/inject-fault` — enabled by configuration, disabled by default.

All request and response bodies are Pydantic v2 models with `extra="forbid"`.

## 11. Audit-event contract v1

Portfolio-wide contract, implemented independently here and copied — not imported — by later
repositories.

| Field | Meaning in this project |
|---|---|
| `principal` | Authenticated human, or `system` |
| `agent_identity` | Model identifier and version, or `null` for deterministic steps |
| `tool` | Operation performed (`match`, `propose_treatment`, `approve`, `compute_amount`, `post`, `retry`, `dlq`, `replay`) |
| `scope_granted` | Authorisation under which the action ran |
| `approval_decision` | `approved` / `rejected` / `edited` / `n_a` |
| `approver` | Principal who decided, where applicable |
| `model` | Model id and version, where a model was involved |
| `region_jurisdiction` | Processing region of the model call |
| `outcome` | `success` / `failure` / `abstained` / `quarantined` |
| `correlation_id` | Spans ingestion → posting |

Append-only. Every ledger-affecting action has at least one event. Thesis stated in the README:
*agent actions in regulated environments must be attributable, scoped, approvable and
jurisdiction-provable.*

## 12. Operation identity and the idempotency contract

### 12.1 Stable operation identifier

Every ledger-affecting operation carries an **operation identifier** that is deterministic, stable and
derived **independently of retries**:

```
operation_id = SHA256( DOMAIN_TAG
                     || len_prefixed(exception_id)
                     || len_prefixed(resolution_version)
                     || len_prefixed(instruction_payload_hash) )
```

Full digest, a named algorithm, a domain-separation tag, and length-prefixed canonical encoding of
every component — unprefixed concatenation is a live collision source and is banned.

`instruction_payload_hash` covers **everything that determines the financial effect**: treatment code,
computed amount, currency, account, period, and the ledger-context version used to derive them. This
matters. If account mapping or period configuration changes between the first attempt and a later
re-send, the instruction is genuinely different and **must** produce a different identifier. Binding
only the treatment code would let one identifier denote two different postings — and under
`ENFORCES_KEY` the provider would suppress the second while this system recorded `CONFIRMED` for a
posting that was never applied.

`approver_id` is deliberately **excluded**. §16 permits the approver to differ for the same economic
event (a re-approval, or an edit requiring a different principal), and an identifier that varies with a
non-financial input is the mirror image of the retry-dependence this rule exists to prevent — and it
fails just as silently. The approver is recorded in `approval` and in the audit trail, never in the key.

`resolution_version` increments when an approved resolution is superseded, so a corrected resolution is
a *different* operation rather than a silent overwrite. Supersession is **interlocked** — see §13.1.

The derivation **must not** include an attempt counter, a timestamp, a clock reading, a random value,
a hostname or a process id. Attempt one and attempt five of the same approved resolution produce the
identical `operation_id`. This is asserted by test, because every other guarantee below rests on it.

### 12.1.1 Write-ahead attempt record

The identifier alone is insufficient for crash recovery. Before **every** socket write, an attempt
record `(operation_id, attempt_no, sent_at, state=IN_FLIGHT)` is committed **in its own transaction**.

On recovery, any `IN_FLIGHT` attempt with no recorded response is `UNKNOWN` **by definition**, and is
never retryable. Without this record, a crash between the socket write and the response write is
indistinguishable from a crash before the write — the system would hold no evidence a send occurred,
and the §14 recovery rows would not be implementable.

The identifier is **persisted before** the external call, never derived at call time.

### 12.2 Internal enforcement

A **unique constraint** on `adjustment.operation_id` is the internal enforcement point. Application
logic is a convenience; the database is the guarantee.

### 12.3 What the identifier does *not* do by itself

Sending an operation identifier to an external system does not make anything idempotent. It is a
*request* for idempotent treatment, honoured only if the downstream ledger implements one. §13
separates which guarantees are ours and which are conditional on the adapter.

## 13. External ledger side-effect semantics

This section exists because "exactly-once" is not achievable across a process boundary, and
"effectively-once" is a **conditional** property that must not be claimed unconditionally. Five
distinct guarantees are separated below. Only the fifth concerns the external financial side effect,
and only it is conditional.

The phrase **"exactly-once" is banned** from this repository (ADR-005).

### 13.1 Guarantee 1 — internal processing (unconditional, ours)

- Work is claimed with `SELECT … FOR UPDATE SKIP LOCKED`; two workers cannot claim one residual.
- At most one approved resolution exists per `(exception_id, resolution_version)`.
- At most one `adjustment` row exists per `operation_id`, enforced by a unique constraint.
- **Supersession interlock.** A new `resolution_version` may **not** be approved for an exception while
  any prior operation on that exception is in a non-terminal state (`IN_FLIGHT`, `UNKNOWN`, or open in
  the recovery queue). Supersession from a non-terminal state must go through recovery, where the prior
  operation is resolved first.

**Holds unconditionally.** It depends on nothing outside this system.

> **Why the interlock is load-bearing.** Without it there is a double-post that survives all five
> guarantees. Exception E: v1 approved, dispatched, response lost, state `UNKNOWN`; the analyst edits
> the treatment; v2 approved with a *different* `operation_id`, dispatched, `CONFIRMED` — while the
> ledger had in fact committed v1. Two adjustments, one residual. Guarantee 1 holds (one row per
> `operation_id`), guarantee 3 is silent (v1 never reached a terminal state), and `ENFORCES_KEY` cannot
> help because the two keys differ by construction. The unit of the guarantee is the **approved
> resolution**, not the exception, and the interlock is what stops one exception producing two live
> resolutions.

### 13.2 Guarantee 2 — transactional outbox (unconditional, ours)

The state change and the dispatch intent are written in a **single database transaction**. There is no
committed approval without an outbox row, and no outbox row without a committed approval.

**Holds unconditionally — and is deliberately at-least-once.** The outbox guarantees the intent is not
*lost*. It does not, and cannot, guarantee the intent is delivered only once. Conflating those two is
the most common error in this pattern.

### 13.3 Guarantee 3 — duplicate dispatch prevention (ours, bounded by knowledge)

The dispatcher will not *initiate* a second send for an `operation_id` already in a **known terminal
state** (`CONFIRMED` or `REJECTED`).

**Bounded by what we know.** When the outcome is `UNKNOWN` (§13.5) this guarantee is silent by
construction: a system cannot suppress a duplicate of an operation whose first outcome it does not
know. Anything stronger would be a claim about the network, not about this code.

### 13.4 Guarantee 4 — the ledger adapter contract (declared, not assumed)

Every ledger adapter **declares its capabilities**. Capability is data the system reads and branches
on, never an assumption baked into the dispatcher.

| Capability | Values | Meaning |
|---|---|---|
| `idempotency` | `NONE` · `ACCEPTS_KEY` · `ENFORCES_KEY` | `ACCEPTS_KEY` means the key is transmitted and echoed; only `ENFORCES_KEY` means the provider contractually suppresses a duplicate for the same key |
| `posting_identity_query` | `NONE` · `BY_OPERATION_ID` | Whether a posting can be looked up by our operation identifier — the queryable posting identity that makes reconciliation possible |
| `reversal` | `NONE` · `VOID` · `COMPENSATING` | Whether an applied posting can be undone, and how |

`ACCEPTS_KEY` is explicitly **not** sufficient on its own. A provider that accepts a header and ignores
it is, from our side, indistinguishable from one that has none — unless we can query.

The adapter's posting operation returns a **three-valued outcome**, never a boolean:

- `CONFIRMED(posting_ref)` — the ledger applied it and told us so.
- `REJECTED(reason)` — the ledger declined it; nothing was applied.
- `UNKNOWN` — we do not know. Sent but no response, timeout, connection reset, ambiguous 5xx.

An adapter that can only return success or failure is **not admissible**, because it forces the caller
to guess which of `CONFIRMED` and `UNKNOWN` occurred.

### 13.5 Guarantee 5 — effectively-once financial side effect (CONDITIONAL)

**The rule.** We may claim an effectively-once ledger side effect **only** when the downstream ledger
adapter provides a verifiable idempotency mechanism — a stable external idempotency / operation key
that the provider *enforces*, or an equivalent queryable posting identity — **and** the operation
identifier is stable and derived independently of retries (§12.1).

The claim is permitted only when:

```
idempotency == ENFORCES_KEY   OR   posting_identity_query == BY_OPERATION_ID
```

**Where the adapter does not meet that bar**, all of the following apply and are enforced in code:

1. **Do not claim exactly-once or effectively-once execution** — not in the README, not in a CV bullet,
   not in a docstring. Adapter capability determines what may be said, and the README renders the
   capability table rather than a slogan.
2. **Classify an ambiguous timeout or posting result as `UNKNOWN`** — never as success, never as
   failure. `UNKNOWN` is a first-class state with its own storage, not an error code.
3. **Do not blindly retry an irreversible financial write.** Automatic retry from `UNKNOWN` is
   permitted *only* where capability allows the duplicate to be suppressed or detected — and even then
   it is **bounded by the declared window and scope**. A re-send under `ENFORCES_KEY` is permitted only
   while `now - first_send < idempotency_window` **and** the target endpoint matches the original
   `idempotency_scope`. Real providers retain keys for a limited period (commonly hours to days) and
   often scope them per-endpoint or per-account; a re-send outside either bound is an ordinary
   duplicate write wearing an idempotency header. Outside the bounds, the operation routes to manual
   recovery. Otherwise the automatic path stops.
4. **Reconcile against the downstream system where possible.** If `posting_identity_query` is
   `BY_OPERATION_ID`, query by operation identifier. Reconciliation is bounded and scheduled, never an
   unbounded retry loop, and its transitions are **monotonic**: `UNKNOWN → CONFIRMED` and
   `UNKNOWN → REJECTED` are permitted; `CONFIRMED → anything` is not.

   | Query result | Resolution |
   |---|---|
   | `Found(ref)` | Resolve to `CONFIRMED` immediately. A positive hit is trustworthy. |
   | `NotFound` | **Not** sufficient on its own. Resolve to `REJECTED` only after **both** the declared `visibility_bound` (or immediately, if `query_consistency == LINEARIZABLE`) **and** `max_inflight_window` have elapsed since the last send, observed on N consecutive queries. Until then the outcome stays `UNKNOWN`. |
   | `Indeterminate` | Stays `UNKNOWN`. Never counts toward the consecutive-`NotFound` requirement. |

   A `NotFound` means "not visible to this query yet", which is not the same as "will never be
   applied": an in-flight request the ledger has received but not yet committed, or a read that is not
   linearizable with respect to the posting write, both produce it. Acting on it is a double-post.

   **On bound exhaustion** — the windows elapsed and the answer is still not definite — the operation
   routes to **manual recovery**. Reconciliation is not total, and the design says so rather than
   leaving the exhausted case to fall off the end.

   The windows used, the query results observed and the resolution reached are all recorded as audit
   events, so an auditor can reconstruct why a resolution was considered safe.
5. **Otherwise route to manual recovery.** The operation enters an operator queue carrying the full
   envelope, last known state, operation identifier, every attempt and every query result, and the
   reason it could not be resolved.

   A queue is not a control on its own, so the design specifies what the operator actually does. The
   recovery item names the **evidence procedure**: which downstream artefact must be inspected (the
   next ledger snapshot, a statement export, or vendor support confirmation), and what constitutes
   sufficient evidence for each permitted resolution. Permitted resolutions are `CONFIRMED_BY_EVIDENCE`,
   `REJECTED_BY_EVIDENCE` and **`RESOLVED_UNVERIFIED`** — the last recording explicitly that no
   evidence was obtainable and a judgement was made anyway. `RESOLVED_UNVERIFIED` is reportable, so an
   unverifiable resolution is visible to an auditor rather than indistinguishable from a verified one.

   Segregation of duties applies: the principal resolving an `UNKNOWN` may not be the principal who
   approved the original adjustment. Recovery items carry an SLA and age; a stale `UNKNOWN` is an
   alertable condition, and an `UNKNOWN` unresolved at period close is escalated rather than carried
   silently across the boundary.
6. **Preserve the complete audit trail** in every branch. Every attempt, every `UNKNOWN`, every
   reconciliation query and result, and every manual decision emits an audit event. `UNKNOWN` is never
   overwritten in place; resolution is an appended transition.

### 13.6 What this repository actually claims

The reference adapter shipped here is a simulated ledger declaring `ENFORCES_KEY` **and**
`BY_OPERATION_ID`. The declared contract is stated in the README beside the claim.

The claim is therefore, precisely: **under the declared adapter contract, and with a retry-independent
operation identifier, this system does not produce a duplicate financial side effect — and the chaos
suite demonstrates that a deliberately naive implementation does.**

The same suite also runs a second adapter configured `idempotency=NONE, posting_identity_query=NONE`
and asserts the system **degrades correctly**: `UNKNOWN` is recorded, no automatic retry of the
irreversible write occurs, and the operation routes to manual recovery. Correct degradation under a
weak contract is part of the demonstration, not an exception to it.

## 14. Failure and recovery expectations

Every one of these is a named chaos scenario in §19:

Behaviour in the last three rows is **capability-dependent** (§13.4), and the table says so rather
than promising an outcome the adapter may not support.

| Failure | Expected behaviour |
|---|---|
| Crash before commit | No outbox row, no effect; work re-claimable |
| Duplicate webhook delivery | Content-hash unique constraint makes it a no-op |
| Worker killed mid-batch | Unclaimed work re-claimable; claimed work times out and returns |
| Two workers claim one residual | `SKIP LOCKED` prevents it; asserted under concurrency |
| Replay of a consumed approval token | Rejected; audit event recorded |
| Model provider unavailable | Exceptions queue for human treatment; no blocking, no corruption |
| Ledger unreachable, allowlisted transport error before first byte | Classified `NOT_SENT`; nothing applied; bounded retry, then DLQ. Distinct from `Rejected`, which means the ledger declined |
| **Supersession attempted while a prior operation is `UNKNOWN`** | Blocked by the §13.1 interlock; the analyst is routed to recovery to resolve the prior operation first |
| **Reconciliation returns `NotFound`, posting later appears** | `NotFound` alone never resolves to `REJECTED`; resolution waits for the visibility and in-flight windows, so the later appearance is observed |
| **Provider idempotency window expired before re-send** | Re-send is refused; operation routes to manual recovery |
| **Multi-leg posting partially applied** (`NON_ATOMIC` adapters) | `PartiallyApplied` outcome; never auto-retried; straight to manual recovery |
| **Crash after send, before response recorded** | Outcome is `UNKNOWN`. `ENFORCES_KEY` → safe re-send of the same `operation_id`. `BY_OPERATION_ID` → reconcile by query. Neither → **manual recovery; no automatic re-send** |
| **Timeout after the ledger committed** (response lost) | Outcome is `UNKNOWN` — indistinguishable from "never applied" at the transport layer. Resolution follows the same capability branch above. **Never assumed to be a failure, never assumed to be a success** |
| **Ledger returns an ambiguous 5xx** | Outcome is `UNKNOWN`; same capability branch. A 5xx is never classified as "not applied" |

## 15. Retry behaviour

- Exponential backoff with **jitter**; base delay, multiplier and cap are configuration.
- Bounded maximum attempts; on exhaustion the envelope moves to the DLQ with reason and attempt count.
- Retries apply only to transport failures on an **enumerated, testable allowlist** where no byte of
  the request was written:
  **DNS resolution failure · TCP connect failure or refusal · TLS handshake failure · connect-timeout
  before first byte written.**
  Everything at or after first-byte-written — read timeout, connection reset, ambiguous 5xx, a 429
  arriving after send — is `UNKNOWN`.
  **The default is `UNKNOWN`, not retryable:** any transport error not on the allowlist is `UNKNOWN`.
  The word "provably" is deliberately avoided, because nothing at the client proves non-arrival — a
  gateway can accept, forward, and then fail the client leg. The classifier *is* the guarantee, so it
  is enumerated rather than described.
- A 4xx other than 429 is `Rejected` and goes straight to DLQ — retrying a validation error is a defect.
  A 429 is `Throttled`, which is a scheduling signal, not a declination, and is retried on its own path.
- **A timeout or connection reset after the request was sent is NOT a transient retry case.** It is an
  `UNKNOWN` outcome (§13.5) and enters the capability branch: safe re-send only under `ENFORCES_KEY`,
  reconciliation under `BY_OPERATION_ID`, manual recovery otherwise. Treating an ambiguous financial
  write as an ordinary transient retry is precisely the defect this design exists to prevent.
- Every attempt is an audit event.
- Total attempt budget is bounded in time as well as count, so an entry cannot retry indefinitely.

## 16. Security requirements

- Webhook signature verification on the settlement feed; unsigned or invalid payloads rejected and
  recorded.
- Authenticated principals for every approval; **role separation** between analyst, controller and
  operator.
- The approver cannot be the same principal as the requester where an edit changed the treatment.
- No secrets in logs or traces; merchant identifiers redacted in telemetry.
- Rate limiting on the webhook endpoint.
- The demo fault-injection endpoint is disabled unless demo mode is explicitly configured.
- Least privilege on the database role: no `UPDATE`/`DELETE` on `audit_event`.

## 17. Secrets handling

- `.env.example` with placeholders; `.env` git-ignored.
- Secrets from environment or the platform secret store; never in code, fixtures or cassettes.
- Cassettes are **scrubbed** of authorisation headers and any provider identifiers before commit, and
  a test asserts no cassette contains a secret-shaped string.
- Staged diff scanned for secrets before every commit.

## 18. Observability

- Correlation id generated at ingestion, propagated through every layer, present on every log line,
  span and audit event.
- Structured JSON logs.
- Counters: retries, DLQ depth, approvals, abstentions, quarantines.
- Histograms: approval latency, dispatch latency, end-to-end exception latency.
- **OpenTelemetry** using GenAI semantic conventions on every model call, with token usage, estimated
  cost and processing region as span attributes — exported to **self-hosted Langfuse**. Both are
  approved for this project by the blueprint, and the conventions established here are reused by
  projects 4 and 6.

## 19. Failure-injection strategy

A committed chaos suite, run against **both** branches:

- `naive/` — a deliberately unsafe baseline: no idempotency key, no outbox, no claim locking. **It
  must double-post.**
- `main` — the real implementation. **It must not.**

Scenarios: crash before commit · duplicate webhook · worker killed mid-batch · two workers claiming
one residual · replay of a consumed approval token · **lost response after a committed ledger write**
(§19.1) · ambiguous 5xx.

Each scenario runs against **three adapter configurations** — `ENFORCES_KEY`, `BY_OPERATION_ID` only,
and `NONE`/`NONE` — because the correct behaviour differs by capability and a suite that tests only
the strong adapter proves only the easy case.

Output is a README table: scenario × branch × adjustments posted × expected × observed. The naive
column must show failures. **A suite that passes on both branches proves nothing and is a defect.**

Injection is via explicit, testable seams (a fault-injection port), not by patching internals.

### 19.1 Required scenario — lost response after a committed ledger write

The scenario this design exists for, specified so it cannot be quietly omitted:

1. The dispatcher sends a posting for `operation_id` X.
2. The downstream ledger **commits it**.
3. The response is lost — connection failure before the dispatcher records the outcome.
4. The dispatcher observes an ambiguous result and records `UNKNOWN` for X. It must **not** record
   failure, and must **not** record success.
5. Recovery behaviour is asserted per adapter capability:
   - `ENFORCES_KEY` → re-send X is safe; the ledger suppresses the duplicate; final state `CONFIRMED`;
     **ledger applied-count for X == 1**.
   - `BY_OPERATION_ID` → no re-send; reconcile by querying X; resolve to `CONFIRMED`;
     **applied-count for X == 1**.
   - `NONE`/`NONE` → **no automatic re-send occurs at all**; X routes to manual recovery; applied-count
     stays 1; an operator decision is required to proceed.
6. The assertion that carries the claim: **an irreversible financial write is never blindly repeated.**
   The test inspects the simulated ledger's applied-count for X directly — it does not infer the
   outcome from application state, since inferring from our own records is exactly what fails here.

The audit trail is asserted complete in all three branches: attempt, `UNKNOWN`, reconciliation query
and result where applicable, and the final resolution.

## 20. Evaluation strategy

- A committed **golden set** of labelled exceptions with expected treatment codes, generated by a
  seeded, committed generator, with the generation procedure documented and a human-labelled hold-out
  slice.
- Scorer reports treatment-proposal accuracy, abstention rate, and confusion across treatment codes.
- **CI gate** fails the build on regression against a committed threshold.
- Runs on **recorded cassettes**, so CI needs no live API key. This pattern is reused portfolio-wide.
- The regression gate is itself verified by injecting a deliberate regression and confirming CI fails.
- **Three-arm comparison** — deterministic matcher, LLM-as-matcher, shipped hybrid — on the same set,
  reporting accuracy, USD per 1,000 lines and p95. The expected result is that the LLM matcher loses
  on all three; if it does not, the result is published unchanged.

## 21. Testing strategy

| Layer | Coverage |
|---|---|
| Unit | Matching rules, tolerance bands, amount computation, key derivation, rounding |
| Property | Amount computation invariants — sign, currency, quantisation, determinism |
| Schema guard | **No numeric type, no amount-like field name, no extra fields** in the model response schema |
| Boundary guard | Calculator module must not import the proposal model |
| Integration | API, worker, migrations up and down |
| Concurrency | Two workers, one residual |
| Chaos | §19, both branches |
| Evaluation | §20, cassette-based, CI-gated |
| Frontend | Key flow, loading / empty / error states |
| Smoke | Post-deploy against the deployed environment |

No milestone is complete until its tests have been run and pass.

## 22. Deployment direction

Fly.io for application and worker, Neon for PostgreSQL, per the blueprint's deployment spread. Docker
Compose for local development with Postgres, Redis and self-hosted Langfuse. GitHub Actions for lint,
type check, tests, chaos suite and the evaluation gate.

Safe demo mode: seeded data, no live provider, request quotas, and the fault-injection control enabled
only in that mode. No unrestricted paid API endpoint is ever exposed publicly.

## 23. Acceptance criteria

The project is complete only when every one of these is true **and has been verified by running it**:

1. `docker compose up` brings the full stack up on a clean machine with no third-party account.
2. Migrations apply and roll back cleanly.
3. The deterministic path — ingest, match, create exceptions — works end to end with **no model call**.
4. The schema guard test passes and **fails** when a numeric field is deliberately added.
5. The boundary guard test passes and **fails** when the calculator is made to import the proposal model.
6. `naive/` **double-posts** under the chaos suite; `main` does not. Both results are tabulated.
7. Every chaos scenario in §19 has a committed test and a recorded outcome.
8. The DLQ replay CLI produces **exactly one applied posting** for the operation — verified by the
   simulated ledger's applied-count, not by our own records — and replay of an already-`CONFIRMED`
   operation applies nothing further. ("Byte-identical ledger state" was the earlier wording and is
   withdrawn: a real ledger assigns its own posting reference and commit timestamp, so byte-identity is
   not a well-formed property and would only ever have been checkable against the test double.)
8a. The lost-response scenario (§19.1) passes under all three adapter configurations, and the
    simulated ledger's applied-count for the operation is 1 in every branch.
8b. Under `idempotency=NONE, posting_identity_query=NONE`, no automatic re-send of an `UNKNOWN`
    financial write occurs, and the operation reaches the manual-recovery queue.
8c. `operation_id` is proven retry-independent: attempt one and attempt five of the same approved
    resolution produce an identical identifier.
8d. An adapter whose posting return type cannot express `Unknown`, or whose query return type cannot
    express `Indeterminate`, is rejected by the contract test.
8e. The **capability conformance suite** passes before `ENFORCES_KEY` or `BY_OPERATION_ID` is declared
    for any adapter, and an unverified capability is treated as `NONE`.
8f. The **supersession interlock** holds: approving a new `resolution_version` while a prior operation
    is `IN_FLIGHT`, `UNKNOWN` or open in recovery is refused, and the refusal is audited.
8g. A **write-ahead attempt record** exists for every send; on simulated crash between socket write and
    response write, recovery classifies the attempt `UNKNOWN` and never retries it.
8h. Reconciliation is **monotonic** and `NotFound` alone never resolves to `REJECTED` before the
    declared visibility and in-flight windows have elapsed; a posting that appears after an initial
    `NotFound` is still resolved correctly.
8i. A re-send under `ENFORCES_KEY` outside the declared `idempotency_window` or `idempotency_scope` is
    refused and routed to manual recovery.
8j. `operation_id` is collision-resistant over canonical length-prefixed encoding: two different input
    tuples never collide, and any mutation of the posting instruction changes the identifier.
9. Two workers cannot claim one residual, asserted under real concurrency.
10. No ledger write occurs without a recorded human approval.
11. Every ledger-affecting action has at least one audit event under contract v1.
12. The evaluation gate runs in CI on cassettes with no live key, and fails on an injected regression.
13. The three-arm comparison is published with accuracy, USD per 1,000 lines and p95 for each arm.
14. The `Measured` table is produced by a committed script and is reproducible on demand.
15. The operations console demonstrates the key flow with loading, empty and error states.
16. Traces carry token usage, estimated cost and region; correlation ids span ingestion to posting.
17. `.env.example` is complete and no secret exists anywhere in the repository or its history.
18. `DECISIONS.md` records a **real, unplanned** failure from the build, with the first wrong
    hypothesis left in.
19. The README states the Ledge.co concession plainly, never uses the phrase "exactly-once", and
    renders the adapter capability table beside the duplicate-suppression claim rather than stating the
    claim unconditionally.
20. Deployed with a working safe demo mode and passing smoke tests.

### Kill tests

Both come from `portfolio-control/PORTFOLIO_BLUEPRINT.md`, which states them as build preconditions
("build only if—") and names no phase for either. `portfolio-control/PORTFOLIO_PROGRESS.md`, which
this document is bound by, states both as "Gate before M*n*" and discharges each at a numbered
increment **inside** that phase — "Increment 3.1 confirms—" — because a gate is *decided against
working code rather than against an intention*. `IMPLEMENTATION_PLAN.md` assigns those increments.
See ADR-053.

Both are checked early; failing either changes the project rather than being worked around.

- **Before M4 — discharged at increment 4.5:** if `naive/` cannot be made to double-post, the chaos
  suite is theatre and the flagship claim collapses. The comparison needs a working `main` —
  dispatcher and adapter (4.2), bounded retry and DLQ (4.3), `UNKNOWN` handling and recovery (4.4) —
  and the §19 suite to be decided against, so 4.5 is its earliest honest decision point. "Early" still
  binds, and means before the console, evaluation, observability and deployment work a failure would
  waste.
- **Before M3 — discharged at increment 3.1:** if the treatment set does not genuinely close into an
  enum — if real cases require the model to propose an amount — the containment claim is false and
  must be **dropped, not softened**.
