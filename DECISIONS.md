# DECISIONS — ledger-exception-control-plane

ADR-style decision log. Records only decisions already justified by the approved portfolio
architecture. Anything not yet settled is marked **OPEN** with what must be decided — no decision is
invented to make this document look finished.

**Status: planning.** No implementation decisions have been tested against running code yet.

> **Reserved.** A flagship `DECISIONS.md` must eventually record a **real, unplanned failure** from the
> build — something that broke, was diagnosed from a trace, and is written up with the first wrong
> hypothesis left in. That section cannot be written before the build produces one, and inventing it
> would be fabrication. It is reserved as ADR-014 and stays empty until then.

---

## ADR-001 — Confine the model to proposing a treatment code

**Status:** Accepted (portfolio blueprint, Project 1)

**Context.** The residual carries unstructured evidence — PSP dispute text, merchant memos, ticket
notes, remittance references — that no normalisation rule captures. But it also carries money, and a
model that emits money is a liability that review does not fix.

**Decision.** The model reads assembled evidence and proposes exactly one treatment code from a closed
enumeration, with a rationale and evidence references, or abstains. It performs no other function.

**Consequences.** The model's value is confined to interpretation. Every consequential decision stays
deterministic or human. The rationale is provenance for humans and is never parsed.

**Rejected alternative.** *LLM as matcher* — letting the model do the reconciliation itself. Rejected
on the expectation that it loses on accuracy, cost and latency; increment 6.3 measures this rather
than assuming it, and publishes the result either way.

---

## ADR-002 — Structural containment, not procedural containment

**Status:** Accepted

**Context.** "The human reviews it" is the standard mitigation for model error in financial workflows.
It is procedural: it depends on attention, and it degrades under volume.

**Decision.** Containment is enforced by types, in two places:
1. The response schema contains **no numeric type anywhere in its tree**, `extra="forbid"` throughout,
   and evidence references are opaque string ids.
2. `compute_adjustment(exception, treatment, ledger_ctx)` has **no parameter through which model free
   text can flow**. The calculator module does not import the proposal model.

Both are enforced by CI guard tests that are themselves verified by deliberate violation.

**Consequences.** A hallucinated amount is unrepresentable rather than unlikely. A reviewer can verify
the claim by reading one Pydantic model and one function signature.

**Rejected alternative.** *Numeric output with validation bounds.* Rejected — it makes correctness a
matter of range-checking rather than representability, and any bound is arguable.

---

## ADR-003 — Build the money path before the model path

**Status:** Accepted

**Context.** Containment claimed after the fact is weaker than containment that was never possible to
violate.

**Decision.** Increment 2.4 (deterministic calculator) precedes increment 3.2 (model schema).

**Consequences.** The calculator is written with no model in the codebase, so it cannot accidentally
depend on one. The boundary guard test then locks that in.

---

## ADR-004 — Five separated guarantees, only the last one conditional

**Status:** Accepted · **Supersedes** the original single-guarantee framing of this ADR

**Context.** The first version of this decision bundled internal duplicate prevention and external
financial side-effect prevention into one claim, with the external dependency reduced to a caveat
sentence. That framing is wrong: it invites an unconditional reading of a conditional property, and a
reviewer who probes it finds the caveat doing load-bearing work it was not written to carry.

**Decision.** Separate five guarantees and state the conditionality of the fifth in the claim itself
(`PROJECT_SPEC.md` §13):

1. **Internal processing** — one claim per residual, one approved resolution per
   `(exception_id, resolution_version)`, one `adjustment` per `operation_id`. **Unconditional.**
2. **Transactional outbox** — state change and dispatch intent in one transaction. **Unconditional,
   and deliberately at-least-once.** It prevents a *lost* intent, not a *repeated* delivery.
3. **Duplicate dispatch prevention** — no second send is initiated for an `operation_id` in a known
   terminal state. **Ours, but bounded by knowledge**: silent by construction when the outcome is
   `UNKNOWN`.
4. **Ledger adapter contract** — capabilities are **declared data**
   (`idempotency`, `posting_identity_query`, `reversal`), and the posting operation returns a
   three-valued outcome. An adapter that cannot express `UNKNOWN` is inadmissible.
5. **Effectively-once financial side effect** — **conditional**, permitted only when
   `idempotency == ENFORCES_KEY` **or** `posting_identity_query == BY_OPERATION_ID`, and only with a
   retry-independent operation identifier.

**Consequences.** The dispatcher branches on declared capability rather than on exception type. The
README renders the capability table beside the claim instead of asserting the claim alone. Guarantee 2
is explicitly labelled at-least-once, because conflating "not lost" with "delivered once" is the most
common error in this pattern.

**Rejected alternative.** *Keep one combined claim with a caveat.* Rejected — a caveat is not a
mechanism, and the distinction between at-least-once delivery and once-only effect is exactly what a
technical reviewer probes first.

---

## ADR-004a — `UNKNOWN` is a first-class outcome, not an error

**Status:** Accepted

**Context.** A timeout after the request was sent is indistinguishable, at the transport layer, from a
ledger that committed and lost the response. Binary success/failure forces a guess, and either guess is
wrong some of the time — unrecoverably and silently. One direction double-posts; the other drops a real
posting. Neither error announces itself. (No rate is stated here: the split depends on a specific
ledger's latency profile and nothing in this repository measures it.)

**Decision.** The adapter returns `CONFIRMED` / `REJECTED` / `UNKNOWN`. `UNKNOWN` is persisted with its
own transitions and is never overwritten in place; resolution is an appended transition. Behaviour from
`UNKNOWN` depends strictly on declared capability:

| Capability | Behaviour from `UNKNOWN` |
|---|---|
| `ENFORCES_KEY` | Safe re-send of the same `operation_id` |
| `BY_OPERATION_ID` | Bounded, scheduled reconciliation by query — no re-send |
| Neither | **No automatic re-send.** Route to manual recovery |

**Consequences.** An `UNKNOWN` financial write never enters the ordinary retry path — a deliberate
carve-out from ADR's retry policy, since retrying an irreversible write on the assumption it failed is
the failure mode this project exists to prevent. Throughput suffers under a weak adapter. That is the
correct trade.

**Rejected alternative.** *Treat timeout as transient and retry.* Rejected — it is the textbook cause
of duplicate financial postings and would invalidate the project's headline claim.

---

## ADR-004b — Retry-independent operation identifier

**Status:** Accepted

**Decision.** `operation_id = SHA256(DOMAIN_TAG || len_prefixed(exception_id) ||
len_prefixed(resolution_version) || len_prefixed(instruction_payload_hash))` — a named algorithm, a
domain-separation tag, and canonical length-prefixed encoding, because unprefixed concatenation is a
collision source.

Two compositional rules, both learned from an adversarial review of the first draft of this ADR:

- **`instruction_payload_hash` binds everything that determines the financial effect** — treatment,
  amount, currency, account, period, ledger-context version. If configuration changes between attempts,
  the instruction differs and the identifier *must* differ; otherwise `ENFORCES_KEY` would suppress a
  genuinely different posting and the system would record `CONFIRMED` for something never applied.
- **`approver_id` is excluded.** §16 permits the approver to vary for the same economic event, so
  including it would make the identifier vary with a non-financial input — the mirror image of the
  retry-dependence this ADR exists to prevent, failing just as silently.

No attempt counter, timestamp, clock reading, random value, hostname or process id. Asserted by test,
alongside a collision test over differing input tuples.

**Rationale.** Every guarantee in ADR-004 rests on this. An identifier that varies with the attempt
makes provider-side suppression and reconciliation-by-query both impossible, and it fails silently —
the system looks correct until the first retry.

**Consequences.** A corrected resolution must increment `resolution_version`, making it a genuinely
different operation rather than a silent overwrite of the previous one.

---

## ADR-005 — Never claim "exactly-once"

**Status:** Accepted (portfolio rule 5)

**Decision.** The phrase is banned repository-wide. Write *effectively-once effect* and name the
mechanism. A test asserts the phrase appears nowhere in the repository.

**Rationale.** The stronger phrase is false in a distributed system with an external provider, and a
2026 reviewer will end the review on it. Precision here is a credibility signal, not pedantry.

**Extended by ADR-004.** "Effectively-once" is itself **conditional** and may only be claimed where the
adapter meets the §13.5 bar. Banning the stronger phrase while asserting the weaker one unconditionally
would reproduce the same error one notch down.

---

## ADR-006 — A deliberately naive RED baseline

**Status:** Accepted

**Context.** A passing chaos suite proves nothing if it would also pass against a broken system.

**Decision.** Ship `naive/` — no idempotency key, no outbox, no claim locking — and run the identical
suite against both branches. Publish both columns.

**Consequences.** `naive/` **must** double-post. If it cannot be made to fail, the suite is theatre and
the flagship claim collapses. This is a kill-test gate at increment 4.4, not a nice-to-have.

---

## ADR-007 — Human approval on every ledger-affecting write

**Status:** Accepted

**Decision.** No adjustment posts without an explicit, attributable human decision. There is no
confidence threshold above which the system posts automatically.

**Consequences.** Throughput is bounded by human review. That is accepted: the failure this system
exists to prevent is expensive and silent, and auto-posting reintroduces it.

**Rejected alternative.** *Auto-post above a confidence band.* Rejected — it would make the model's
output consequential, contradicting ADR-001 and ADR-002.

---

## ADR-008 — Append-only audit under portfolio contract v1

**Status:** Accepted

**Decision.** Implement the portfolio audit-event contract v1 (principal, agent identity, tool, scope
granted, approval decision and approver, model, region/jurisdiction, outcome, correlation id).
Append-only, enforced by database grant rather than convention.

**Consequences.** This repository defines the canonical shape for six later projects. They
**re-implement** it independently — repositories stay independent, with no shared library and no
submodules.

---

## ADR-009 — Postgres and Redis, no message broker

**Status:** Accepted

**Decision.** PostgreSQL for state and the outbox; Redis with arq for worker queueing. No Kafka, no
RabbitMQ.

**Rationale.** The transactional outbox requires the state change and the dispatch intent to share a
transaction, which is exactly what a relational database gives and a broker does not. Market research
also found heavyweight event streaming over-weighted relative to demand for this profile. Adding a
broker would be complexity without a problem.

---

## ADR-010 — Provider port with at least two adapters

**Status:** Accepted (portfolio rule 7)

**Decision.** All model access goes through a port; at least two provider adapters exist and the swap
is proven by test.

**Consequences.** No project is welded to one vendor. Provider unavailability degrades to human
treatment rather than blocking the deterministic path.

---

## ADR-011 — Cassette-based evaluation in CI

**Status:** Accepted (portfolio rule 3)

**Decision.** The evaluation gate runs on recorded, scrubbed cassettes, so CI needs no live API key.
The gate is verified by injecting a deliberate regression and confirming the build fails.

**Consequences.** Establishes the offline-evaluation pattern that five later projects copy. Cassettes
are scrubbed of authorisation headers and provider identifiers, asserted by test.

---

## ADR-012 — OpenTelemetry into self-hosted Langfuse

**Status:** Accepted (blueprint, Project 1 observability)

**Decision.** OTel spans using GenAI semantic conventions on every model call, carrying token usage,
estimated cost and processing region; exported to self-hosted Langfuse.

**Rationale.** Vendor-neutral, and it spans two skill clusters the market research found are usually
held by different people. Conventions established here are reused by projects 4 and 6.

---

## ADR-013 — Fly.io + Neon, and a safe demo mode

**Status:** Accepted (blueprint deployment spread)

**Decision.** Deploy to Fly.io with Neon Postgres. Public demo runs seeded data with no live provider,
request quotas, and the fault-injection control enabled only in demo mode.

**Rationale.** The portfolio deliberately spreads deployment targets. No unrestricted paid endpoint is
ever exposed publicly.

---

## ADR-014 — Real failure encountered during the build

**Status:** RESERVED — cannot be written yet

Required for a flagship: a genuine, unplanned failure from this build, how it was detected, the first
wrong hypothesis, and the guard or test that now catches it. Planned comparisons are labelled
comparisons; only genuine surprises count. **Do not fabricate this entry.**

---

## ADR-015 — Liveness and readiness are separate endpoints with different dependency coupling

**Status:** Accepted (M0.2)

**Decision.** `GET /healthz` is liveness and probes nothing external. `GET /readyz` is readiness and
probes PostgreSQL and Redis concurrently, read-only, under a bounded per-dependency timeout, returning
`503` with per-dependency status when either is unavailable. The container healthcheck in Compose uses
**liveness only**.

**Rationale.** An orchestrator restarts a container that fails liveness. A liveness probe coupled to
the database therefore converts a brief database outage into a restart storm that removes the capacity
needed to recover. Readiness is the correct place to express "do not send me work right now".

**Consequences.** Neither endpoint returns a DSN, credential, host or stack trace — a readiness
endpoint is reachable by anyone who can reach the service.

---

## ADR-016 — Dependency probes run concurrently, not sequentially

**Status:** Accepted (M0.2) · **Found by real-stack measurement, not by design review**

**Context.** The first implementation awaited each probe in turn. Measured against the running stack
with PostgreSQL stopped, `/readyz` answered in **3.07 s** — the sum of the per-dependency timeouts,
not the maximum. Bounded, but the bound grows with every dependency added.

**Decision.** Probe concurrently with `asyncio.gather`, so readiness is bounded by the slowest single
probe and the worst case stays flat as the dependency list grows.

**Consequences.** Re-measured at roughly 2.4 s, though samples ranged 2.36–3.43 s on Docker Desktop
for Windows, so the design property is the claim here, not a precise speedup.

---

## ADR-017 — The application emits its own request log line

**Status:** Accepted (M0.2) · **Found by inspecting real container logs**

**Context.** Uvicorn's access log is written *after* the correlation middleware unbinds the context
variable, so every access line carried `correlation_id: null` — useless exactly where a correlation id
is most wanted.

**Decision.** Emit a structured `http request` line from inside the middleware, carrying method, path,
status and duration, while the id is still bound. Uvicorn's access logger is set to `WARNING` so
requests are not logged twice.

**Consequences.** Metadata only; request and response bodies are never logged.

---

## ADR-018 — `configure_logging` replaces only its own handler

**Status:** Accepted (M0.2) · **Found by a test that was passing vacuously**

**Context.** The first implementation cleared *all* root handlers. That removed pytest's `caplog`
handler, which made a secret-leak test assert over an empty record list — it passed without ever
inspecting real log output.

**Decision.** Tag the handler this function installs and remove only handlers carrying that tag.

**Consequences.** Repeated calls still do not stack duplicate output, but an embedding application's
logging — and the test harness's — survives.

---

## ADR-019 — Compose publishes non-default host ports

**Status:** Accepted (M0.2) · Narrow and reversible

**Context.** Port 5432 was already bound on the development machine by a locally installed PostgreSQL.

**Decision.** Publish `127.0.0.1:15432` for PostgreSQL and `127.0.0.1:16379` for Redis. The
application is unaffected: inside the Compose network it reaches dependencies by service name on their
standard ports. The host mapping exists only for attaching a local client.

**Consequences.** No collision with a locally installed PostgreSQL or Redis, which is a common setup.
Documented in the README so the ports are not surprising.

---

## ADR-020 — Money is unconstrained `NUMERIC` with a rejecting check, not `NUMERIC(20, 4)`

**Status:** Accepted (M1.1) · **Superseded the original fixed-typmod decision on 2026-08-29,
after empirical testing disproved its central assumption**

**The defect.** The first version of this ADR specified `NUMERIC(20, 4)` and asserted that it
satisfied the project's "no silent rounding at persistence boundaries" rule. **It does not.** A
fixed-scale typmod does not reject an over-precise value — it rounds it, before any check
constraint can observe the original. Measured on PostgreSQL 16 against the shipped schema:

```
submitted : Decimal('1.23456')      (5 fractional digits)
INSERT    : ACCEPTED — no error, no warning
read back : Decimal('1.2346')       silently rounded, difference -0.00004
```

The application had no way to know the value had changed. That is exactly the failure the rule
exists to prevent, and the original ADR asserted the opposite without testing it.

**Decision.** Monetary columns are **unconstrained `NUMERIC`** — no precision, no scale — so the
original decimal reaches the database unchanged, guarded by a check constraint per column:

```sql
CHECK (trunc(amount, 4) = amount AND abs(amount) < 10000000000000000)
```

**Why `trunc`, not `scale`.** `scale()` is representation-based: `scale(1.230000)` is 6, so a
scale-based rule rejects a value numerically identical to `1.2300` that loses nothing when
stored. `trunc(v, 4) = v` is value-based and rejects exactly the values that would lose
information. Verified: `1.230000` passes under `trunc`, fails under `scale`.

**Magnitude.** `abs(v) < 10^16` preserves the integer range `NUMERIC(20, 4)` allowed — 20 total
digits with scale 4 gives 16 integer digits, maximum `9999999999999999.9999`. The literal parses
as `bigint`→`numeric`, never as a float.

**NULL.** A bare `CHECK` evaluates to `NULL`, not `FALSE`, so the nullable `tolerance_applied`
needs no guard clause. Verified.

**Verified end to end through asyncpg**, not merely in SQL — the driver does not round
client-side, so the constraint genuinely fires:

| Value | Result |
|---|---|
| `1.2345`, `-1.2345`, `100`, `0`, `1.2300`, `1.230000` | accepted, stored exactly |
| `9999999999999999.9999`, `-9999999999999999.9999` | accepted, stored exactly |
| `1.23456`, `-1.23456`, `0.00005` | **rejected** by `ck_*_precision` |
| `10000000000000000`, `-10000000000000000` | **rejected** by `ck_*_precision` |

**Consequences.** Over-precise input now fails loudly at the write instead of corrupting the
value. Callers must quantize *explicitly* before persisting — an audited, deliberate rounding
earlier in the pipeline is permitted; a silent one at the boundary is not. Scale 4 remains the
limit, for the reasons in the original ADR (JPY 0, most currencies 2, BHD/KWD/TND 3, plus a
fourth for intermediate fee-split and FX values). Amounts stay signed.

**A note on process.** This defect shipped in commit `70a8888` because the original ADR reasoned
about `NUMERIC(20,4)` rather than testing it. The correction was found only when the behaviour was
actually exercised. Two regression guards now exist — one on the model metadata, one querying
`information_schema` — because a future migration could reintroduce a typmod without touching the
models.

---

## ADR-021 — Alembic is the schema-management mechanism; `create_all()` is not

**Status:** Accepted (M1.1)

**Decision.** Schema changes go through reviewed Alembic migrations. `create_all()` is not used to
manage the schema in any environment, including tests — the integration tests migrate a real database
from zero to head, so migration correctness itself is what gets exercised.

**Alembic reads its URL from `Settings`, not `alembic.ini`.** No connection string is committed, none
is printed, and migrations cannot run against an environment the application is not configured for.
`compare_type` and `compare_server_default` are both enabled, without which autogenerate silently
misses a changed column type or default while every migration still appears to succeed.

**Consequences.** A model change without a migration is caught by `alembic check` in CI, not
discovered in production.

---

## ADR-022 — UUID primary keys, generated by the application

**Status:** Accepted (M1.1) · Narrow and reversible

**Context.** The specification does not state an identifier strategy, so this was decided here.

**Decision.** UUIDv4 primary keys generated in Python, stored as PostgreSQL `uuid`.

**Rationale.** An identifier exists before the row is flushed, which later increments need when
correlating identifiers across logs, evidence references and audit events; a sequence would force a
round trip. UUIDs also avoid exposing row counts through enumerable ids.

**Trade-off, stated honestly.** Random UUIDs have worse index locality than a sequence. At this
project's scale that is not a real cost, and it can be revisited with UUIDv7 if it ever becomes one.

---

## ADR-023 — Timestamp ownership is explicit per column

**Status:** Accepted (M1.1)

**Decision.** All timestamps are `TIMESTAMP WITH TIME ZONE`; naive datetimes are impossible.
Ownership is split deliberately:

- **`created_at` is generated by the database** (`server_default=now()`), so a row cannot exist
  without one even when inserted by a migration, a bulk load or psql.
- **Business timestamps** — `received_at`, `booked_at`, `matched_at` — are supplied by the
  **application**, because only it knows the real-world event time, which on replay differs from
  row-insert time.

`settlement_line.value_date` is a `DATE`, not a timestamp: settlement files state a value date, and
widening it would invent precision the source does not have.

---

## ADR-024 — Uniqueness that prevents double-counting

**Status:** Accepted (M1.1)

**Decision.** Beyond the specified `settlement_batch.content_hash`, three further unique constraints:

| Constraint | Invariant protected |
|---|---|
| `settlement_line (settlement_batch_id, line_number)` | A batch cannot contain two line 7s — guards a partial re-parse |
| `ledger_entry (external_ref)` | Re-importing a snapshot must not create two matchable copies of one entry |
| `match_result (settlement_line_id)` | A line matches at most one entry; a line needing several is a *split*, which is residual by definition and becomes an exception |
| `match_result (ledger_entry_id)` | **A ledger entry is consumed at most once.** Without this, two settlement lines could both claim one entry and the ledger would appear to reconcile twice — a silent double-count |

All are in the database rather than the application because concurrent writers can both pass an
application-level check.

**Two indexes only**, both for M2's actual read paths — unmatched lines of a batch, and ledger
candidates by account and booking time. A test asserts no other index exists, so speculative indexes
cannot accumulate.

---

# Open decisions

Not yet decided. Each names what must be settled and by when.

## OPEN-1 — Settlement file format and the shape of the simulated PSP feed

**Must decide:** the concrete schema of the simulated settlement file, whether it mirrors a specific
public PSP report layout or is a synthesised composite, and how FX rates arrive.
**Constraint:** must be awkward on purpose — inconsistent references, missing fields, memo text of
varying quality — or the matcher is validated against tidy input and proves nothing.
**Needed before:** increment 1.3.

## OPEN-2 — Tolerance band configuration and defaults

**Must decide:** which dimensions carry tolerance (absolute, relative, date window), their defaults,
and whether they are per-currency.
**Impact:** directly determines the size of the residual, and therefore how much work the model sees.
**Needed before:** increment 2.2.

## OPEN-3 — Exception classification taxonomy, final form

**Must decide:** whether the six proposed classes (partial capture, fee split, chargeback reversal, FX
rounding, cross-period refund, unclassified) survive contact with the fixture corpus.
**Needed before:** increment 2.3. Related to the ADR-001 enum-closure gate at 3.1.

## OPEN-4 — Account mapping and period-assignment rules

**Must decide:** how a treatment code maps to ledger accounts, and how period is assigned for
cross-period cases.
**Constraint:** must be configuration, not code, and must be deterministic.
**Needed before:** increment 2.4.

## OPEN-5 — Which two model providers

**Must decide:** the two provider adapters implemented behind the port, and the specific model
identifiers pinned for measurement.
**Constraint:** must be pinned and date-stamped, since the `Measured` table is meaningless otherwise.
**Needed before:** increment 3.2.

## OPEN-6 — Evaluation threshold for the CI gate

**Must decide:** the accuracy and abstention thresholds that fail the build.
**Constraint:** cannot be chosen before a baseline exists, or the threshold is arbitrary. Set it after
the first scorer run, and record the reasoning.
**Needed before:** increment 6.2.

## OPEN-7 — Measurement load profile

**Must decide:** the exact load profile for the `Measured` table — batch size, concurrency, corpus mix.
**Constraint:** must be documented and reproducible; every later project's table depends on this shape.
**Needed before:** increment 9.1.

## OPEN-8 — Authentication mechanism for the console

**Must decide:** how principals authenticate for approval, and how roles are assigned.
**Constraint:** must support role separation and attributable approval. Deliberately *not* OAuth —
project 4 owns that territory, and duplicating it here would be redundant.
**Needed before:** increment 5.1.

## OPEN-9 — Demo-mode data volume and quota policy

**Must decide:** how much seeded data the public demo carries and what request quotas apply.
**Constraint:** no unrestricted paid endpoint exposed publicly.
**Needed before:** increment 10.1.

## OPEN-12 — The reversal / unwind path

**Must decide:** whether to build an operator-initiated unwind from the recovery queue, and if so what
`VOID` versus `COMPENSATING` mean concretely, what audit events they emit, and what §19 scenario
exercises them.
**Context:** the `reversal` capability is currently **declared and consumed by nothing** — no dispatcher
branch, no failure row, no test, no acceptance criterion reads it. It is retained as a reserved
placeholder rather than removed, because a recovery queue with no unwind path is a plausible gap an
auditor would raise. Until this is decided, `reversal` must not be cited as a capability the system
acts on.
**Needed before:** any claim that the system can correct a wrongly applied posting.

## OPEN-11 — Capability profile of any real ledger the project is later pointed at

**Must decide:** if the simulated adapter is ever replaced by a real ledger (or a second reference
adapter is added), its actual `idempotency`, `posting_identity_query` and `reversal` capabilities must
be established from that vendor's documentation — **not assumed** — and the README claim adjusted to
match whatever the capability table then says.
**Constraint:** the claim follows the capability, never the reverse. If a real ledger turns out to be
`ACCEPTS_KEY` without enforcement and without query, the effectively-once claim must be withdrawn for
that adapter rather than reworded.
**Needed before:** any adapter other than the two simulated ones ships.

## OPEN-10 — Hosting cost ceiling and shutdown policy

**Must decide:** the monthly ceiling for Fly.io and Neon, and whether the demo sleeps when idle.
**Context:** portfolio decision D4 is still open and applies across all ten projects. A dead demo link
is worse than no demo link.
**Needed before:** increment 10.1.
