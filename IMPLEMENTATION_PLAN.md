# IMPLEMENTATION_PLAN — ledger-exception-control-plane

Ordered, small, independently verifiable increments. Each maps to the portfolio milestone ladder in
`portfolio-control/PORTFOLIO_PROGRESS.md` (M0–M12) but is deliberately finer, so every step is a
reviewable commit rather than a large opaque one.

**Status: plan only. No increment has been started.**

Rules that apply to every increment:

- An increment is complete only when its tests have been **run** and pass, lint and type checks are
  clean, and the services still start.
- Each increment ends at one commit. Conventional prefixes: `feat:`, `fix:`, `test:`, `chore:`,
  `docs:`, `refactor:`.
- No increment is merged with a knowingly broken build.
- Never claim an increment works without running the verification named in its exit criteria.

**Two kill-test gates** sit at increments 3.1 and 4.5. Both are cheap to check and expensive to
discover late. Neither may be worked around.

---

## Phase M0 — Scaffold

### 0.1 Repository skeleton and tooling
- **Goal.** A repository that lints, type-checks and runs an empty test suite in CI.
- **Deliverables.** `pyproject.toml` (Python 3.12, pinned), `ruff`, `mypy` strict, `pytest`; `src/`
  package layout; `.env.example`; `LICENSE`; `PROJECT_STATUS.md`; `.github/workflows/ci.yml`.
- **Tests.** One trivial test proving the runner works.
- **Exit criteria.** CI green on a clean checkout. `mypy` and `ruff` clean.
- **Commit.** `chore: scaffold project tooling and CI`

### 0.2 Local stack and health endpoints
- **Goal.** The stack starts on a clean machine with no third-party account.
- **Deliverables.** `docker-compose.yml` (Postgres, Redis); FastAPI app with `/healthz`, `/readyz`;
  `Makefile` targets; structured JSON logging with a correlation-id middleware.
- **Tests.** Integration test hitting both endpoints against the composed stack.
- **Exit criteria.** `docker compose up` works from scratch; health checks pass in CI.
- **Commit.** `feat: add local stack, health endpoints and structured logging`

---

## Phase M1 — Data model

### 1.1 Core reconciliation schema
- **Goal.** Persist settlement and ledger data.
- **Deliverables.** Alembic setup; migrations for `settlement_batch` (unique `content_hash`),
  `settlement_line`, `ledger_entry`, `match_result`.
- **Tests.** Migration up and down; unique-constraint test on `content_hash`.
- **Exit criteria.** Migrations apply and roll back cleanly against a fresh database.
- **Commit.** `feat: add reconciliation core schema and migrations`

### 1.2 Exception, resolution and reliability schema
- **Goal.** Persist the decision path and the machinery that makes it safe.
- **Deliverables.** Migrations for `exception`, `evidence`, `treatment_proposal`, `approval`,
  `adjustment` (**unique `operation_id`**), `outbox`, `posting_attempt` (write-ahead record, state
  `IN_FLIGHT`/resolved), `dlq`, `recovery_queue` (with evidence-procedure and SLA fields),
  `audit_event` (insert-only grant).
- **Tests.** Migration up/down; unique-constraint test on `operation_id`; a test asserting the
  application role cannot `UPDATE` or `DELETE` `audit_event`.
- **Exit criteria.** All constraints enforced at the database level, proven by failing writes.
- **Commit.** `feat: add exception, outbox, DLQ and audit schema`

### 1.3 Seeded fixture generator
- **Goal.** Realistic, reproducible data — and the basis of the golden set.
- **Deliverables.** Seeded generator producing settlement batches with a controlled residual mix
  across the exception taxonomy; documented generation procedure; awkward-on-purpose cases
  (missing references, ambiguous memos, cross-period refunds).
- **Tests.** Same seed → identical corpus; residual mix matches the declared distribution.
- **Exit criteria.** Corpus regenerates byte-identically; committed sample loads.
- **Commit.** `feat: add seeded settlement fixture generator`

---

## Phase M2 — Deterministic core (no model anywhere)

### 2.1 Normalisation and quarantine
- **Goal.** Typed internal representation with explicit currency.
- **Deliverables.** Parser, normaliser, quarantine path with reasons.
- **Tests.** Unit tests over malformed and awkward inputs; quarantine reasons asserted.
- **Exit criteria.** No `float` anywhere in monetary handling, enforced by test.
- **Commit.** `feat: add settlement normalisation and quarantine`

### 2.2 Matching engine with tolerance bands
- **Goal.** Clear the bulk deterministically.
- **Deliverables.** Rule-based matcher, configurable tolerance bands, recorded matching rule per line.
- **Tests.** Unit tests per rule; boundary tests at tolerance edges; a test recording the share of the
  fixture corpus cleared without any model call.
- **Exit criteria.** Matcher clears the bulk; every matched line records which rule matched it.
- **Commit.** `feat: add deterministic matching with tolerance bands`

### 2.3 Exception creation and classification
- **Goal.** Turn residual into typed, classified work.
- **Deliverables.** Residual detection; closed classification taxonomy; exception records with
  correlation ids.
- **Tests.** Classification unit tests across the taxonomy; unclassified path exercised.
- **Exit criteria.** Every residual line becomes exactly one exception.
- **Commit.** `feat: create and classify residual exceptions`

### 2.4 Deterministic amount calculator
- **Goal.** The money path, isolated and provable, **before** any model exists.
- **Deliverables.** Pure `compute_adjustment(exception, treatment, ledger_ctx) -> Money`; explicit
  quantisation and rounding mode; account and period selection from configuration.
- **Tests.** Unit tests per treatment; **property tests** for determinism, sign, currency and
  quantisation invariants; a test asserting the module performs no I/O.
- **Exit criteria.** Calculator is pure and total; escalates rather than guessing when a treatment
  cannot be priced.
- **Commit.** `feat: add deterministic adjustment calculator`

> Building the money path before the model path is deliberate: it makes the containment argument a
> structural fact about the codebase rather than a claim added afterwards.

---

## Phase M3 — Model layer behind a port

### 3.1 KILL-TEST GATE — treatment enum closure
- **Goal.** Prove the treatment set genuinely closes into an enum before building anything on it.
- **Deliverables.** A written analysis over the fixture corpus and the exception taxonomy showing every
  case resolves to `REBOOK | ACCRUE | WRITE_OFF | ESCALATE`; recorded in `DECISIONS.md`.
- **Tests.** A test asserting every fixture exception is resolvable to a treatment code without any
  amount being proposed.
- **Exit criteria.** The enum closes. **If real cases require the model to propose an amount, the
  containment claim is false and must be dropped, not softened** — the project changes shape here.
- **Commit.** `docs: record treatment enum closure analysis`

### 3.2 Provider port and closed response schema
- **Goal.** Structured output that structurally cannot carry money.
- **Deliverables.** Provider port with at least two adapters; `TreatmentProposal` Pydantic v2 model
  per `PROJECT_SPEC.md` §6.1, `extra="forbid"` throughout.
- **Tests.** **Schema guard** walking the JSON Schema — fails on any numeric type, amount-like field
  name, or additional property. **Boundary guard** asserting the calculator module does not import the
  proposal model. Provider swap proven by test.
- **Exit criteria.** Both guards pass, and both are proven to **fail** when deliberately violated.
- **Commit.** `feat: add provider port and closed treatment proposal schema`

### 3.3 Evidence assembly and proposal flow
- **Goal.** Give the model addressable evidence and accept an abstention.
- **Deliverables.** Evidence assembler producing stable ids; prompt construction; abstention handling;
  persistence of model id, version, prompt hash.
- **Tests.** Evidence ids stable across runs; abstention path exercised; malformed model output
  rejected without falling back to a guess.
- **Exit criteria.** Provider unavailability queues the exception for human treatment and never blocks
  the deterministic path.
- **Commit.** `feat: assemble evidence and record treatment proposals`

### 3.4 Cassette recording harness
- **Goal.** CI evaluation with no live API key — a pattern reused portfolio-wide.
- **Deliverables.** Record/replay harness; scrubbing of authorisation headers and provider identifiers.
- **Tests.** Replay determinism; a test asserting no cassette contains a secret-shaped string.
- **Exit criteria.** The suite runs offline from cassettes.
- **Commit.** `test: add cassette record and replay harness`

---

## Phase M4 — Reliability (the flagship claim)

### 4.1 Claim locking and retry-independent operation identifier
- **Goal.** One residual, one worker, one operation identifier.
- **Deliverables.** `SELECT … FOR UPDATE SKIP LOCKED` claim; deterministic `operation_id` derivation
  over `(exception_id, resolution_version, treatment_code, approver_id)` per `PROJECT_SPEC.md` §12.1;
  identifier persisted before dispatch.
- **Tests.** Concurrency test — two workers, one residual. Determinism and stability of `operation_id`;
  an explicit assertion that it contains **no** attempt counter, timestamp, clock reading, random value,
  hostname or process id; a collision test over differing input tuples; an assertion that mutating any
  component of the posting instruction changes the identifier; and an assertion that changing only
  `approver_id` does **not**.
- **Exit criteria.** Two workers provably cannot claim one residual; the identifier is provably
  retry-independent and provably instruction-bound.
- **Commit.** `feat: add claim locking and retry-independent operation identifier`

### 4.2 Transactional outbox and capability-declaring ledger adapter
- **Goal.** Decision and intent cannot diverge, and the adapter's guarantees are declared rather than
  assumed.
- **Deliverables.** Outbox write in the same transaction as the state change; dispatcher; ledger
  adapter port per `PROJECT_SPEC.md` §10.1 returning the three-valued `PostingOutcome`
  (`Confirmed` / `Rejected` / `Unknown`); `capabilities()` declaration; retry-independent
  `operation_id` supplied by the caller; posting reference recorded on `Confirmed`.
- **Also delivers.** The **write-ahead attempt record** committed in its own transaction before every
  socket write; the three-valued `QueryOutcome` (`Found` / `NotFound` / `Indeterminate`); and the
  **capability conformance suite** that must pass before `ENFORCES_KEY` or `BY_OPERATION_ID` may be
  declared for any adapter.
- **Tests.** Contract test rejecting any adapter whose posting type cannot express `Unknown` or whose
  query type cannot express `Indeterminate`; conformance suite proving a declared `ENFORCES_KEY`
  adapter actually suppresses a repeated `operation_id` (applied-count 1) and a declared
  `BY_OPERATION_ID` adapter actually returns a known posting; an unverified capability is treated as
  `NONE`; `operation_id` identical across attempts one and five; no outbox row exists without its state
  change and none is lost; capability is read, not inferred.
- **Exit criteria.** Dispatch works end to end; the dispatcher branches on declared capability; a crash
  between socket write and response write leaves a recoverable `IN_FLIGHT` record.
- **Commit.** `feat: add transactional outbox and capability-declaring ledger adapter`

### 4.3 Bounded retry, DLQ and replay CLI
- **Goal.** Failure has a floor and a way back.
- **Deliverables.** Exponential backoff with jitter, bounded attempts and a time budget; an
  **enumerated transport classifier** — retryable only for DNS failure, TCP connect failure/refusal,
  TLS handshake failure and connect-timeout before first byte, with **`UNKNOWN` as the default for
  everything else**; `Throttled` handled on its own path, distinct from `Rejected`; DLQ with full
  envelope; `replay` CLI.
- **Tests.** Backoff bounds; terminal 4xx goes straight to DLQ; **replay produces an applied-count of
  exactly 1** for the operation (verified against the simulated ledger, not inferred
  from our own records); replay of an already-`CONFIRMED` adjustment applies nothing further; a test
  asserting an `UNKNOWN` outcome never enters the ordinary retry path; a test asserting a transport
  error absent from the allowlist defaults to `UNKNOWN` rather than to retry.
- **Exit criteria.** DLQ replay demonstrated end to end.
- **Commit.** `feat: add bounded retry, dead-letter queue and replay CLI`

### 4.4 Ambiguous outcome (`UNKNOWN`) semantics and manual recovery
- **Goal.** Make the conditional nature of the side-effect guarantee real in code, not just in prose.
  This is the increment added by the side-effect-semantics correction.
- **Deliverables.** `UNKNOWN` as a first-class persisted state with monotonic transitions (never
  overwritten in place); the capability branch of `PROJECT_SPEC.md` §13.5 — re-send under
  `ENFORCES_KEY` **bounded by `idempotency_window` and `idempotency_scope`**, bounded scheduled
  reconciliation under `BY_OPERATION_ID` where `NotFound` resolves to `REJECTED` **only** after the
  declared visibility and in-flight windows have elapsed across N consecutive queries, and **manual
  recovery with no automatic re-send** otherwise; the **supersession interlock**; the manual-recovery
  queue with its evidence procedure, SLA, staleness alert, segregation-of-duties rule and the
  `RESOLVED_UNVERIFIED` outcome; `/recovery` endpoints; audit events for every attempt, `UNKNOWN`,
  query result and operator decision; a second simulated adapter configured `idempotency=NONE,
  posting_identity_query=NONE`.
- **Tests.** Each of the three capability configurations drives its own branch; **no automatic re-send
  occurs under `NONE`/`NONE`**; `NotFound` alone never resolves to `REJECTED` before the windows
  elapse, and a posting appearing after an initial `NotFound` still resolves correctly;
  `Indeterminate` never counts toward the consecutive-`NotFound` requirement; a re-send outside the
  idempotency window or scope is refused; supersession during `UNKNOWN` is blocked and audited; the
  resolving principal may not be the approving principal; `UNKNOWN` is never silently coerced; the
  audit trail is complete in all three branches.
- **Exit criteria.** An irreversible financial write is never blindly repeated; behaviour is determined
  by declared capability rather than by exception type; and reconciliation that exhausts its bounds
  lands in recovery rather than falling off the end of the design.
- **Commit.** `feat: add UNKNOWN outcome handling, reconciliation and manual recovery`

### 4.5 KILL-TEST GATE — naive RED baseline and chaos suite
- **Goal.** Make the green result mean something.
- **Deliverables.** `naive/` baseline with no operation identifier, no outbox, no claim locking; chaos
  suite covering every scenario in `PROJECT_SPEC.md` §19 via an explicit fault-injection port,
  **including the lost-response scenario §19.1** (dispatcher sends → ledger commits → response lost →
  dispatcher observes an ambiguous result → recovery follows adapter capability); each scenario run
  against all **three** adapter configurations; results table for both branches.
- **Tests.** The full suite, run against `naive/` **and** `main`. The §19.1 assertion inspects the
  simulated ledger's applied-count for the operation directly — never inferred from our own records,
  since inferring from our own records is exactly what fails in this scenario — and requires it to be
  **1 in every branch**.
- **Exit criteria.** **`naive/` double-posts. `main` does not.** If the naive branch cannot be made to
  fail, the suite is theatre and the flagship claim collapses — stop and reconsider rather than
  proceeding.
- **Commit.** `test: add chaos suite and naive RED baseline`

---

## Phase M5 — Human gate and audit

### 5.1 Approval gate with role separation
- **Goal.** No ledger write without a recorded human decision.
- **Deliverables.** Approve / edit / reject endpoints; authenticated principals; analyst, controller
  and operator roles; approval-token single use.
- **Tests.** No path posts without approval; role restrictions enforced; consumed approval token
  rejected on replay.
- **Exit criteria.** The gate blocks the write, proven by test.
- **Commit.** `feat: add human approval gate with role separation`

### 5.2 Audit-event contract v1
- **Goal.** The portfolio's canonical audit shape, established here.
- **Deliverables.** Contract v1 per `PROJECT_SPEC.md` §11; emission at every state transition;
  append-only enforcement.
- **Tests.** Every ledger-affecting action emits at least one event; append-only enforced by database
  grant; correlation id spans ingestion to posting.
- **Exit criteria.** A posted adjustment answers: what evidence, which model, who approved, what was
  computed, by which path.
- **Commit.** `feat: implement audit-event contract v1`

---

## Phase M6 — Evaluation

### 6.1 Golden set and scorer
- **Goal.** Measurable proposal quality.
- **Deliverables.** Committed golden JSONL with a documented generation procedure and a human-labelled
  hold-out slice; scorer reporting accuracy, abstention rate and confusion across treatment codes.
- **Tests.** Scorer unit tests; golden set schema validation.
- **Exit criteria.** Scorer runs offline from cassettes.
- **Commit.** `test: add golden set and treatment proposal scorer`

### 6.2 CI evaluation gate
- **Goal.** A gate that actually gates.
- **Deliverables.** GitHub Actions job running the scorer against a committed threshold.
- **Tests.** **Inject a deliberate regression and confirm CI fails**; restore and confirm it passes.
- **Exit criteria.** The gate is proven to fail, not merely to exist.
- **Commit.** `ci: gate builds on treatment proposal evaluation`

### 6.3 Three-arm comparison harness
- **Goal.** The portfolio's "why we did NOT use an agent here", with numbers.
- **Deliverables.** Harness running the same labelled set through the deterministic matcher, an
  LLM-as-matcher baseline and the shipped hybrid; accuracy, USD per 1,000 lines and p95 per arm.
- **Tests.** Harness determinism on cassettes; cost computed from provider usage fields, not estimated.
- **Exit criteria.** All three arms measured on identical inputs. **The result is published as
  measured, including if the LLM matcher wins.**
- **Commit.** `test: add three-arm matcher comparison harness`

---

## Phase M7 — Operations console

### 7.1 Exception queue and detail
- **Goal.** A reviewer understands the system in under a minute.
- **Deliverables.** Next.js + TypeScript; queue with filters; detail view showing evidence, proposal,
  rationale marked as model-generated, computed amount and audit trail.
- **Tests.** Key flow test; loading, empty and error states asserted.
- **Exit criteria.** Provenance for any exception is reachable in two clicks.
- **Commit.** `feat: add operations console queue and exception detail`

### 7.2 Approval flow, DLQ view and fault-injection demo
- **Goal.** Make the guarantee visible, not just tested.
- **Deliverables.** Approval interaction; DLQ view with replay; demo-mode "inject a crash" control
  showing duplicate suppression live.
- **Tests.** Approval round-trip; DLQ replay from the UI; demo control disabled outside demo mode.
- **Exit criteria.** A visitor can trigger a crash and see that no second adjustment is posted.
- **Commit.** `feat: add approval flow, DLQ view and fault-injection demo control`

---

## Phase M8 — Observability

### 8.1 OpenTelemetry and Langfuse conventions
- **Goal.** Conventions later projects copy.
- **Deliverables.** OTel spans using GenAI semantic conventions on every model call; token usage,
  estimated cost and processing region as attributes; self-hosted Langfuse in Compose; counters and
  histograms per `PROJECT_SPEC.md` §18.
- **Tests.** Span attributes asserted; correlation id present on every log line and span; a test
  asserting no secret or unredacted merchant identifier appears in telemetry.
- **Exit criteria.** A single exception is traceable end to end in Langfuse.
- **Commit.** `feat: instrument with OpenTelemetry and self-hosted Langfuse`

---

## Phase M9 — Measurement

### 9.1 Measurement harness and `Measured` table
- **Goal.** Numbers that can be reconstructed on demand.
- **Deliverables.** `scripts/measure/` producing USD cost per exception, p50/p95 latency, throughput,
  with exact model, hardware and date; documented load profile.
- **Tests.** Harness reproducibility; cost derived from provider usage fields.
- **Exit criteria.** `Measured` table generated by script and pasted into the README **from that
  output only**. No hand-written numbers.
- **Commit.** `feat: add measurement harness and publish measured results`

---

## Phase M10 — Deployment

### 10.1 Fly.io + Neon with safe demo mode
- **Goal.** A live link that is safe to send.
- **Deliverables.** Fly deployment for app and worker; Neon Postgres; migrations on release; seeded
  demo data; request quotas; demo mode with no live provider; smoke tests.
- **Tests.** Post-deploy smoke tests against the deployed environment.
- **Exit criteria.** Live demo works; no unrestricted paid endpoint is publicly exposed.
- **Commit.** `chore: deploy to Fly.io with Neon and safe demo mode`

---

## Phase M11 — Documentation

### 11.1 README, architecture and decision record
- **Goal.** The artifact a suspicious reviewer actually reads.
- **Deliverables.** README with the measured table above the fold, the chaos results table for both
  branches, the three-arm comparison, the Ledge.co concession stated plainly, and the honest statement
  of what is *not* guaranteed; `docs/architecture.md` with Mermaid diagrams; `DECISIONS.md` completed
  including **a real, unplanned failure from the build with the first wrong hypothesis left in**;
  operator runbook.
- **Tests.** Link check; a test asserting the phrase "exactly-once" appears nowhere in `src/`, `web/`,
  `docs/` or `README.md`. The rule-defining documents — `CLAUDE.md`, `PROJECT_SPEC.md`,
  `DECISIONS.md` and this plan — are excluded by an explicit allowlist, since they must quote the
  banned phrase in order to ban it.
- **Exit criteria.** Every number traces to a committed script. No claimed functionality is absent.
- **Commit.** `docs: complete README, architecture and decision record`

### 11.2 Demo recording
- **Goal.** 90 seconds that carry the argument.
- **Deliverables.** Screen recording: problem → exception → proposal → approval → posting → injected
  crash → no double-post.
- **Exit criteria.** Under 90 seconds, no narration of implementation detail before business value.
- **Commit.** `docs: add demo recording and screenshots`

---

## Phase M12 — Career assets

### 12.1 Career assets
- **Goal.** Reusable positioning material, written only once the project genuinely works.
- **Deliverables.** `docs/career-assets.md` — CV bullets, portfolio description, 30-second and
  2-minute explanations, hardest challenge, trade-offs, likely interview questions.
- **Exit criteria.** Every claim traces to something that exists. No invented metrics.
- **Commit.** `docs: add career assets`

---

## Reusable foundations established here

Seven later repositories depend on patterns first built in this project. Repositories stay
independent — these are **copied, not imported**. No shared library, no submodules.

| Foundation | Established in | Consumed by |
|---|---|---|
| **Audit-event contract v1** — principal, agent identity, tool, scope, approval decision and approver, model, region/jurisdiction, outcome, correlation id | 5.2 | Projects 2, 4, 5, 6, 8, 9 |
| **Reliability patterns** — retry-independent operation identifier, transactional outbox, `FOR UPDATE SKIP LOCKED` claim, bounded backoff with jitter, DLQ envelope, replay CLI ergonomics | 4.1–4.3 | Projects 2, 6, 9 (load-bearing); 5, 8 (secondary) |
| **Ledger adapter capability contract** — declared capabilities, three-valued `PostingOutcome`, `UNKNOWN` as a first-class state, reconciliation and manual recovery | 4.2, 4.4 | Projects 2, 6, 9 — every repo with an irreversible external side effect |
| **RED-baseline discipline** — a deliberately naive branch that must fail the same suite | 4.5 | Project 2 (n8n arm as the RED baseline) |
| **Cassette / offline CI evaluation pattern** — record, scrub, replay; CI with no live API key | 3.4, 6.2 | Projects 3, 6, 7, 8, 9 |
| **Evaluation harness conventions** — committed golden JSONL, scorer module, CI gate proven to fail on an injected regression | 6.1–6.2 | All nine remaining projects |
| **Observability conventions** — OTel GenAI semantic conventions, token/cost/region span attributes, self-hosted Langfuse, correlation-id propagation | 8.1 | Projects 4 and 6 (load-bearing); others secondary |
| **Measurement harness and load profile** — how every `Measured` table in the portfolio is produced | 9.1 | All ten projects |
| **Seeded fixture-generation discipline** — documented procedure, reproducible corpus, human-labelled hold-out | 1.3 | Projects 2, 6, 8, 9 |

## Sequencing notes

- **The money path is built before the model path** (2.4 before 3.2). Containment is then a structural
  property of the codebase rather than a claim retrofitted onto it.
- **Both kill-test gates come early** (3.1 and 4.5) — before the console, evaluation, observability and
  deployment work that would be wasted if either failed.
- **The evaluation gate is verified by breaking it** (6.2), because a gate that has never failed is
  not known to be a gate.
- Total: **31 increments across 13 phases**, against a blueprint estimate of ~20 sessions. Increments
  are deliberately sized at one focused session or less, so several will share a session. If the count
  and the session estimate diverge during the build, the estimate is what gets revised — the
  increments are not merged to make the arithmetic tidy.
