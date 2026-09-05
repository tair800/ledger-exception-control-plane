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

## ADR-025 — `evidence_refs` is a relation, not a `UUID[]` column

**Status:** Accepted (M1.2)

`PROJECT_SPEC.md` §6.1 requires a treatment proposal to record the evidence it cited. Two ways to
store that: a `UUID[]` column on `treatment_proposal`, or an association table.

**Decision.** An association table, `treatment_proposal_evidence`.

The array keeps the table count at exactly the ten the implementation plan names, and that was the
argument for it. It was rejected anyway: PostgreSQL cannot enforce a foreign key from an array
element, so a proposal could cite an evidence id that does not exist, or that was later removed. A
provenance record pointing at nothing is worse than no provenance record, because it reads as
evidence during an audit. This project's standing rule is to prefer a database-enforced invariant
wherever the database can express one cleanly, and here it can.

**This is a realisation of a specified field, not a new business entity.** It has no columns of its
own beyond the two foreign keys, its primary key is the pair, and a metadata test pins that shape so
it cannot quietly grow into something else. The eleventh table is bookkeeping, not scope.

---

## ADR-026 — `audit_event` immutability is a trigger; the grant is defence in depth

**Status:** Accepted (M1.2)

`IMPLEMENTATION_PLAN.md` §1.2 specifies an "insert-only grant" and a test that the application role
cannot `UPDATE` or `DELETE` `audit_event`. Both are delivered — and a second, stronger control was
added, because the grant alone does not hold.

**Decision.** Two independent controls.

1. **A trigger** (`audit_event_append_only_row`, `audit_event_append_only_truncate`) raising on
   `UPDATE`, `DELETE` and `TRUNCATE`. This is the primary control.
2. **An insert-only grant** to a least-privilege `lecp_app` role. Defence in depth.

The trigger is primary because a grant protects only the roles someone remembered to restrict, and it
does not constrain the table owner at all — and the owner is precisely the identity a migration, a
maintenance script or a `psql` session runs as. A grant-based control is therefore strongest against
the application, which is the actor least likely to be the problem, and absent against the operator,
who is most likely to be.

`TRUNCATE` needs its own statement-level trigger: it bypasses row-level triggers entirely, so an
append-only table that permits it is not append-only.

**Stated limit, not glossed over.** The table owner can drop the trigger. This stops accidental and
application-level mutation; it does not stop a determined privileged operator, and nothing inside one
database can. Detecting that would need audit logging outside this database, which is not in scope
here.

**The role is provisioned by a script, not a migration.** `scripts/sql/provision_app_role.sql`. A role
is a cluster-level object while a migration operates on one database, so role DDL in a migration is
wrong in both directions: it leaks outside the database being migrated, and it fails on a managed
platform where the migrating identity cannot create roles. Release order is migrate, then provision —
and that order matters, because `GRANT ... ON ALL TABLES` applies to the tables existing when it runs.
The schema suite runs the real script and then uses `SET LOCAL ROLE`, so the grant is tested without a
credential for the role existing anywhere.

---

## ADR-027 — Ambiguity is representable but not settleable

**Status:** Accepted (M1.2)

**Decision.** `outbox` and `posting_attempt` can both hold an ambiguous result, and a check constraint
forbids an ambiguous outbox row from being marked `settled`:

```sql
state <> 'settled' OR last_outcome IN ('confirmed', 'rejected')
```

Two failure modes, opposite in direction, and the schema has to block both. If `UNKNOWN` were not
storable, the code would be forced to write something false the moment a timeout occurred. If it were
storable *and* settleable, `UNKNOWN` would quietly become "done" the first time someone wrote a state
machine that treated a resolved attempt as a finished one. `throttled` and `partially_applied` are
excluded from the settled set for the same reason.

`posting_attempt` carries the matching rule in the other direction:
`(state = 'resolved') = (outcome IS NOT NULL AND resolved_at IS NOT NULL)` — an attempt is
`in_flight` with nothing known, or resolved with both recorded, and never half of either. That is what
makes the write-ahead record §12.1.1 requires actually usable as recovery evidence: a row that says a
send happened and nothing came back is exactly the `UNKNOWN` case, and it is distinguishable from a
crash before the send only because `sent_at` is `NOT NULL`.

---

## ADR-028 — Segregation of duties is enforced by a verified principal, not a copied one

**Status:** Accepted (M1.2). **Superseded in part by its own correction — see below.**

§13.5 requires that the principal resolving an `UNKNOWN` is not the principal who approved it. A check
constraint cannot reference another table, so enforcing this in the database needs the approver's
identity on the `recovery_queue` row itself.

**First decision, and it was half right.** Carry `approving_principal` on the recovery item and
constrain `resolved_by <> approving_principal`. The reasoning recorded at the time was that the
alternative is an application-level check, and *"a segregation-of-duties control that depends on
application discipline is not a control — it is a convention that holds until someone writes a second
code path."*

**An adversarial review then pointed that sentence back at the decision itself, correctly.** If the
application supplies `approving_principal`, the discipline has been *relocated* from the comparison to
the copy, not removed. A second code path writing `approving_principal = 'system'` leaves the check
comparing the resolver against a value nobody approved: `'alice' <> 'system'` is true, every constraint
passes, and Alice resolves her own `UNKNOWN` while the audit record looks clean. The original ADR
presented a false dichotomy — denormalised copy versus application check — and never considered the
third option.

**Corrected decision.** The value is carried *and verified*, by a chain of composite foreign keys:

| Key | Effect |
|---|---|
| `approval UNIQUE (id, approved_treatment, principal)` | Gives the chain something to reference |
| `adjustment (approval_id, approved_treatment, approving_principal) → approval` | The approver is copied from the approval and checked against it |
| `adjustment UNIQUE (id, approving_principal)` | Gives the next hop something to reference |
| `recovery_queue (adjustment_id, approving_principal) → adjustment` | The recovery item cannot name anyone else |

`approving_principal` is still denormalised — that cost is real and still accepted, and freezing it is
still correct, because the question is who approved *this* operation at the time rather than who holds
the role today. What changed is that it is no longer *trusted*. The check constraint is unchanged; it
now compares against a value the database has verified rather than one the writer asserted.

**The lesson worth keeping.** The original reasoning was sound and the conclusion did not follow from
it. That is not a careless error — it is the ordinary failure mode of checking your own work by reading
it, and it is the reason the increment was reviewed adversarially before commit rather than after.

---

## ADR-030 — An adjustment is bound to what its approval actually authorised

**Status:** Accepted (M1.2)

FR-7 and `CLAUDE.md` rule 3 require a recorded human decision before any ledger write. The schema
originally expressed that as a plain foreign key, `adjustment.approval_id → approval.id`.

**That proves an approval row exists. It does not prove the approval said yes.** The database accepted
a fully formed, dispatchable adjustment — with an `operation_id`, eligible for an outbox row — whose
sole authority was a *rejection*. Two independent review lenses found it. The audit trail would have
answered "who approved this?" by pointing at someone who declined.

**Decision.** Reference the authorisation itself, not just the approval's identity:

```
adjustment (approval_id, approved_treatment, approving_principal)
    → approval (id, approved_treatment, principal)
```

`approval.approved_treatment` is NULL precisely when the decision was a rejection — that is already
enforced by `ck_approval_approved_treatment_iff_authorising`. The referencing columns are NOT NULL, so
a rejection has no value that could ever match. The bad row is not discouraged; it is unreachable.

Both referencing columns must be NOT NULL for this to work at all. PostgreSQL's default `MATCH SIMPLE`
skips the entire check if *any* referencing column is NULL, so a nullable `approved_treatment` would
have produced a foreign key that silently enforced nothing.

**Two further constraints from the same idiom:**

- `CHECK (approved_treatment <> 'escalate')` — §6.2 escalates precisely when a case *cannot* be priced
  deterministically, so an adjustment for an escalated treatment is a computed amount for the case
  that was referred because no amount could be computed.
- `posting_attempt (adjustment_id, operation_id) → adjustment (id, operation_id)` — the attempt record
  duplicates `operation_id` deliberately, so that it is self-contained evidence after a crash. Verified
  rather than copied, because recovery reads that row to decide whether an irreversible financial write
  may be repeated. An attempt naming a different operation than its adjustment would be well-formed and
  wrong, which is the worst thing evidence can be.

The redundant-looking unique constraints (`approval (id, …)`, `adjustment (id, …)`) exist only because
a foreign key must reference a uniquely-constrained column list. They add index maintenance cost on
tables that are written once per decision, which is the cheapest possible place to pay it.

---

## ADR-029 — Default DSNs point at the stack's published ports, not the service defaults

**Status:** Accepted (M1.2)

**Decision.** `Settings.postgres_dsn` defaults to port `15432` and `redis_dsn` to `16379` — the ports
Compose publishes (ADR-019) — rather than `5432` and `6379`.

Found while running migrations during M1.2. The default pointed at `localhost:5432`, so an
unconfigured `alembic upgrade head` targeted whatever PostgreSQL happened to be on the default port —
on this machine, a developer's own unrelated instance. It failed on authentication, which is luck, not
design: had that instance held a matching role and password, the migration would have created fifteen
tables in someone else's database and reported success.

An unconfigured default must be **wrong-but-harmless** — connection refused — never
right-for-the-wrong-database. Nothing else changes: the container and CI both set `LECP_POSTGRES_DSN`
explicitly, so this affects only an unconfigured local run, which is exactly the case that was unsafe.

---

## ADR-031 — Settlement file format and the simulated PSP feed (resolves OPEN-1)

**Status:** Accepted (M1.3). **Resolves OPEN-1**, which the plan required to be settled before this
increment.

**Decision.** A synthesised composite CSV, not a copy of any vendor's report layout.

```
psp_reference,merchant_reference,transaction_type,amount,currency,value_date,
presentment_amount,presentment_currency,fx_rate,memo
```

**One row is one movement, carrying one signed amount.** Fees are their own rows rather than gross/fee/net
columns on a capture row. That is realistic — PSP settlement files do report fees separately — and it
avoids a trap: a file with `gross`, `fee` and `net` columns forces the *generator* to decide which becomes
`settlement_line.amount`, and that decision belongs to the normaliser in M2.1. With one amount per row
there is nothing for this increment to decide on M2.1's behalf.

**Not a specific PSP's layout.** Mirroring a named provider's proprietary report would imply a
compatibility claim the project cannot support and has not tested, in a public repository. The composite
carries the properties that matter — a provider reference, a pass-through merchant reference that is
sometimes absent, signed amounts, a value date, presentment/settlement currency pairs and free-text memos
— without pretending to be anyone's format.

**FX rates arrive as recorded inputs**, per §3's non-goal, and are stored **as a string** in the file.
A rate is not money: `money_column` carries a four-decimal ceiling and assumes a paired currency, and a
rate has neither property. Nothing in this system computes a rate, and nothing at M1.3 persists one — the
rate lives in the raw payload for M2.1 to interpret.

**Awkward on purpose, and enumerated rather than merely asserted.** Missing merchant references, empty
and ambiguous memos, three-row fee splits, opposing signed chargeback rows, a repeated `psp_reference`
within one file, cross-period dates, and a foreign presentment currency. Each is recorded as a declared
`Awkwardness` value on its scenario, so "the corpus is awkward" is a checkable property rather than a
claim in a README. A matcher validated against tidy input proves nothing.

**Deliberately invalid artifacts sit in `invalid/`, labelled and unloadable** — an over-precise amount, a
missing column, bad currency codes, an unparseable amount. They exist for M2.1's quarantine path, which
cannot be tested against well-formed data. Their labels state a *fact about the bytes*, never the
quarantine reason M2.1 will emit; inventing that vocabulary here would pre-empt an increment this one does
not own.

---

## ADR-032 — Fixture determinism: hashed draws, UUIDv5, an anchored epoch

**Status:** Accepted (M1.3)

NFR-9 requires the same seed to produce the same corpus; the plan is stricter and requires the corpus to
regenerate **byte-identically**. Three sources of variation had to be closed, and each was closed by
replacing it rather than by seeding it.

**Draws are hashed, not streamed.** `random.Random(seed)` is reproducible for a fixed interpreter, but it
is a stream: a value depends on how many draws preceded it. Adding one scenario to the catalogue would
shift every scenario after it, turning a one-line change into a whole-corpus diff — and CPython's
`shuffle` and `sample` have changed implementation before, so the guarantee was never quite what it
looked like. Every value is instead `SHA-256(domain ‖ seed ‖ label)` over length-prefixed components, so
it depends on nothing but its own label. A test proves a label's value is unchanged by intervening draws.

*Stated honestly:* reducing a digest modulo a range is very slightly biased toward the low end. For
synthetic fixtures that is immaterial, and rejection sampling would reintroduce order-dependence for no
benefit — but "uniform" is a claim, and this is not quite one.

**Identifiers are UUIDv5, which is load-bearing twice.** They are deterministic, and they are *visibly not
version 4*, which ADR-022 reserves for rows the application generates. A fixture row can therefore never
be mistaken for a real one, and the distinction is carried by the UUID version field rather than by a
naming convention someone could forget. A test asserts every identifier in the corpus has version 5.

**Time is anchored, never read.** All dates and timestamps derive from a fixed epoch. No `now()`,
`today()` or `uuid4()` appears anywhere in the package, and a test walks the AST to enforce that rather
than grepping — verified by injecting a `datetime.now()` call and confirming the test fails.

**Byte stability is explicit**, not inherited: `\n` line endings written in binary, UTF-8 without a BOM,
sorted JSON keys, a total ordering on every collection, and amounts rendered from `Decimal` by `str` so a
value keeps the scale its currency actually uses. Amounts serialise as JSON *strings* — a JSON number
would come back as a float, and a monetary value that round-trips through binary floating point has
already lost.

---

## ADR-033 — Two profiles, one committed corpus, pinned by a drift test

**Status:** Accepted (M1.3)

**Decision.** A closed set of two profiles, not a configuration language.

| Profile | Committed | Purpose |
|---|---|---|
| `canonical` | Yes | Exactly one instance of every scenario. Small enough to read; addressable by scenario id |
| `bulk` | No | Generated on demand at volume, so the declared residual mix can be checked *as a distribution* |

`canonical` cannot demonstrate a distribution — one of each is a checklist, not a mix — and `bulk` cannot
be committed without putting a large generated dataset in Git for no reason. Each exists because the other
cannot do its job.

**What is committed:** the generator, the canonical corpus (11 files, ~20 KB), the scenario metadata and a
manifest. **What is not:** bulk output, and any database.

**The committed corpus is pinned by a drift test** that regenerates it and compares bytes, run both in the
unit suite and as its own CI step. Without it the committed artifacts are just files someone once
produced, and nothing would notice them drifting from the code that claims to generate them. Verified by
tampering with a committed file and confirming both the test and the CI command fail — the command exits
non-zero and names the offending paths.

**The declared mix is a design parameter, not a measurement.** Nobody here has measured a real settlement
feed. The weights encode the only property §1 actually asserts — deterministic matching clears the great
majority, a modest residual remains — and the README says so rather than implying an empirical basis.

**`--instances` is a count, not a suggestion.** Proportional allocation zeroes the rare scenarios first,
so a floor guarantees each appears at least once; the floor takes its unit from the largest bucket rather
than adding one, because adding would make the corpus larger than the size requested. Found by a test that
asserted the requested size and got a larger one.

---

## ADR-034 — Ground truth is construction intent, never a computed answer

**Status:** Accepted (M1.3) · Reused by increment 6.1

Every scenario records what it is: intended classification, intended match outcome, the awkwardness it
carries, why it exists and what distinguishes it from its neighbours.

**Decision.** That metadata is written by the *constructor* and never derived by running anything over the
data.

A fee-split scenario is a fee split because the builder wrote a capture row, two fee rows and a single
combined ledger entry. Nothing compares a settlement line to a ledger entry to find that out — and
nothing could, since the matcher is M2.2 and does not exist. This is the difference between a corpus that
can judge a future matcher and one that cannot: an oracle produced by the system under test measures only
its self-consistency.

The rule is enforced structurally, not by discipline. A test walks the package's AST and fails on an
import outside an allowlist, or on a function whose name begins with a verb M2 owns (`match_`, `classify`,
`normalise`, `parse_`, `reconcile`, `compute_`). Both were verified by injection. The allowlist is
deliberately an allowlist: reconciliation modules do not exist yet, so a denylist would have nothing to
name and would pass silently when one arrives.

**Where the honest answer is "it depends", the metadata says so.** A line differing from the ledger by one
to three minor units carries the intent `tolerance_policy_dependent`, because whether it clears depends on
bands OPEN-2 has not settled. Recording either outcome would be inventing a decision nobody has taken —
and it is exactly the case OPEN-2 needs in order to be decided responsibly.

---

## ADR-035 — The fixture loader refuses any database that is not obviously disposable

**Status:** Accepted (M1.3)

**Decision.** The target database name must match `^lecp_(test|demo|fixtures)$`. Anything else — including
the project's own `lecp` — is refused before a connection is opened.

A loader that can be pointed somewhere by accident is a data-loss tool with a friendly name. Checking the
*name* rather than the port is deliberate: a disposable database can live on any port, and ADR-029 already
records that the developer machine this was built on runs an unrelated PostgreSQL on the default one. A
flag would put the decision in whichever caller forgot to pass it.

**Reset is by identifier, never `TRUNCATE`.** Deterministic UUIDv5 identifiers mean the loader can delete
exactly the rows it owns; a test inserts an unrelated ledger entry, runs a reset, and asserts it survived.

**No constraint is ever disabled.** Rows go in through the ORM against the real schema with every check,
foreign key and unique constraint enforced — that is the entire content of the plan's "committed sample
loads" criterion. A load that needed integrity switched off would prove the corpus is *not* loadable. The
loader also recomputes each batch's `content_hash` from the bytes on disk before inserting, because FR-1's
re-delivery guard is built on that value.

---

## ADR-036 — The corpus writer is guarded like the loader, because it is the destructive one

**Status:** Accepted (M1.3)

ADR-035 guards the fixture *loader*: it refuses any database not obviously disposable, on the
principle that "a loader that can be pointed somewhere by accident is a data-loss tool with a friendly
name". An adversarial review pointed that sentence at the other half of the system.

`write_corpus` removes stale artifacts — every file under its target that is not one it is about to
write — so a renamed artifact cannot linger and break the manifest digest. That is correct behaviour
for a corpus directory. It is catastrophic for anything else, and `--out` is a free-form path:
`generate --out .` from the repository root would have unlinked the source tree, the tests, the
migrations and, because `rglob` matches dotted entries, `.git` along with them.

The asymmetry was the tell. The loader, whose writes are additive and reversible, had an allowlist;
the writer, which deletes, had nothing.

**Decision.** `write_corpus` refuses a target that exists, is non-empty, and contains no
`manifest.json`. Writing is permitted into a directory that does not exist, is empty, or is already a
corpus.

A marker file rather than a path allowlist: a corpus is a thing with a manifest, and that is checkable
wherever someone chooses to put one. A path allowlist would have to guess at directory names and would
be wrong for a temp directory, which is where most callers legitimately write one.

**Related, and the same mistake in a different place.** The integration suites run
`alembic downgrade base` — fifteen tables dropped — before anything checks the target is disposable,
because the check lived inside `load()`. Both suites now check first. A guard that fires after the
destructive step is decoration.

---

## ADR-037 — The declared mix is an apportionment rule, not an approximation

**Status:** Accepted (M1.3 phase close)

`IMPLEMENTATION_PLAN.md` §1.3 requires the residual mix to "match the declared distribution". A share
of a discrete corpus is rarely a whole number, so "match" has to mean something precise or it means
nothing. The earlier tests took the weak reading — they asserted the total, the coverage and the
ordering, and PROJECT_STATUS then described that as verifying the mix, which was an overstatement of
the project's own work.

**Decision.** The distribution is a stated apportionment rule, and the rule is what is tested.

**The declared distribution**, in parts per 200 — a design parameter of the synthetic corpus, not a
measurement of a real settlement feed:

| Class | Parts | Share |
|---|---|---|
| `matched` | 163 | 81.5% |
| `tolerance_policy_dependent` | 12 | 6.0% |
| `partial_capture` | 7 | 3.5% |
| `fee_split` · `chargeback_reversal` · `fx_rounding` · `unclassified` | 4 each | 2.0% each |
| `cross_period_refund` | 2 | 1.0% |

200 as the denominator is deliberate: every declared share is then an exact number of parts, and a
corpus whose size is a multiple of 200 reproduces the percentages exactly rather than nearly.

**The rule — Hare quota with largest remainder (Hamilton's method), then a coverage floor:**

1. `ideal_i = N * weight_i / TOTAL_WEIGHT`, as an exact rational.
2. Allocate `floor(ideal_i)`.
3. Give the `N - Σ floor` remaining units to the largest fractional remainders, ties broken by
   catalogue position.
4. Raise any scenario still at zero to one, taking the unit from the largest bucket.
5. `Σ = N` exactly, always.

**What is guaranteed, and where:**

| Condition | Guarantee |
|---|---|
| Any `N ≥ 12` | Total is exactly `N`; every scenario appears; counts weakly decreasing by weight |
| `N ≥ 200` | The floor cannot bind, so every count is `floor` or `ceil` of its ideal — deviation **strictly under one instance** |
| `N` a multiple of 200 | No remainder exists; the corpus **is** the declared percentages |
| `12 ≤ N < 200` | The rarest classes have an ideal below one. Step 4 gives each exactly one and takes the units from the dominant bucket, so the deviation is concentrated there by construction and every other class stays within one of its ideal |

Step 4 is a real cost and it is chosen deliberately: a corpus missing a declared condition entirely is
worse than one whose dominant class is under-represented, because the missing condition silently
removes coverage a later test believes it has. The cost is bounded and tested, not waved at.

**Why the units are taken rather than added.** Adding would make the corpus larger than the size
requested, turning `--instances N` into a suggestion. It is a count.

**How it is tested** — three independent things, because any one alone is weak:

- **Hand-computed literals** at four sizes, worked through the rule by hand. These are the only tests
  that would catch a change to the *rule itself*.
- **An independent reimplementation** using `fractions.Fraction`, compared against the implementation
  across eleven sizes. Exact rationals rather than floats: at large `N` a float remainder can compare
  equal when the true values differ, which would make the tie-break depend on binary rounding.
- **The bound**, asserted directly: `|count - ideal| < 1` wherever that is claimable.

The literal for `N = 100` was written as 74 for the dominant bucket and the reimplementation disagreed
— the hand computation had forgotten step 4, and 72 is correct. That is precisely why both exist; a
reimplementation compared only against itself proves consistency, not correctness.

**Ground truth stays out of it.** The expected mix is aggregated from `intended_classification` — a
field the builder wrote — and from the declared weights. Nothing compares a settlement line to a
ledger entry to decide what a scenario is, and no M2 logic was introduced to validate the mix.

---

## ADR-038 — The ingestion boundary validates explicitly, not through Pydantic

**Status:** Accepted (M2.1) · A narrow exception to NFR-2

NFR-2 says every boundary schema is a Pydantic v2 model with `extra="forbid"`, and the settlement
file is unquestionably a boundary. This increment does not use Pydantic for it, and the reason is the
quarantine reason.

**Decision.** Parse and validate with explicit code, and report failures as a closed
`QuarantineCode` enum plus a line and a column.

FR-2 requires an invalid batch to be quarantined **with a reason**, and that reason is stored in a
financial control record. Three properties follow, and Pydantic's `ValidationError` has none of them:

| Required | Pydantic's error strings |
|---|---|
| A **closed** vocabulary an operator can write a runbook against | Open, and they change between library versions |
| **Bounded** length, so the reason cannot grow with the input | Grow with the number and size of the failing fields |
| **Free of the offending input** | Interpolate the input by design — that is what makes them useful in a stack trace and unsuitable in a control record |

The third is the one that settles it. `PROJECT_SPEC.md` §17 treats a stored string as a log line
waiting to happen, and a validation message containing a fragment of a settlement file is payload
content travelling somewhere nobody decided it should go. The offending value is already durably
stored in `raw_payload`; the reason names the code, the line and the column, and an operator reads
the retained file for the rest.

**Scope of the exception.** Ingestion only. Pydantic remains the rule for API request and response
bodies, for configuration and for the model response schema, where the input is not a financial
artifact and the error is not persisted. The rendered reason is checked against a character allowlist
and a hard cap, both asserted by test, so the property is enforced rather than trusted.

---

## ADR-039 — Normalisation preserves references exactly; canonicalisation belongs to matching

**Status:** Accepted (M2.1)

A settlement file's references arrive imperfect — different casing, stray spacing, inconsistent
punctuation. Something has to decide how close two of them must be before they denote one movement.

**Decision.** Not here. The normaliser stores what the file said, character for character.

The only reading it applies is **empty and whitespace-only mean absent** — a required reference that
is blank is a defect, and a nullable one that is blank becomes `NULL`. No character inside a supplied
value is altered: no case folding, no punctuation stripping, no whitespace collapsing, no trimming.

Canonicalisation looks like a tidy-up and is actually a matching rule. Doing it at ingestion would
bake one particular rule into the persisted record, where M2.2 could not vary it, no test could
measure its effect, and the original would be gone — a reference is evidence, and a matcher that
turns out to need the difference between `ORD-1 ` and `ORD-1` could not recover it. Increment 2.2
owns tolerance and identity, and it can canonicalise on the way into a comparison, where the decision
is visible and reversible.

**Accepted cost.** A reference with stray whitespace persists with it, and M2.2 will have to handle
that. That is the correct place for it to be handled.

---

## ADR-040 — Quarantine is batch-level: one bad row condemns the file

**Status:** Accepted (M2.1)

FR-2 says "reject structurally invalid **batches** into quarantine with a reason", and the schema
agrees — `settlement_batch.status` carries `quarantined` with a required reason, and there is no
line-level equivalent. What the specification does not spell out is whether a single unreadable row
condemns an otherwise readable file.

**Decision.** It does. A batch is accepted whole or not at all, and a rejected batch persists no
lines.

The alternative is superficially attractive: keep the rows that parsed, quarantine the rest, lose
less. It is wrong for this system specifically. Accepting a subset manufactures a **trusted partial
settlement file**, and reconciliation over a partial file does not produce fewer results — it
produces *wrong* ones. Every movement that was in the file and not in the accepted subset becomes an
unexplained residual: an exception raised against a ledger entry whose counterpart was silently
dropped at ingestion. The system would then invite an analyst to resolve a discrepancy that does not
exist, and the audit trail would say the file was processed.

A quarantined batch is a visible, actionable stop. A partially accepted one is a quiet corruption of
the input to everything downstream.

**Proven, not asserted.** A three-row payload with one bad middle row is ingested against real
PostgreSQL and the two valid rows are shown to be absent from `settlement_line`.

**What this does not decide.** Whether a *later* increment may reprocess a corrected file is a
separate question; today a corrected file is a different payload with a different content hash, so it
ingests as the new batch it is.

---

## ADR-041 — Receipt and outcome are separate transactions, and the batch is claimed under a lock

**Status:** Accepted (M2.1)

FR-1 requires the raw payload to be persisted with its content hash **before parsing**. Taken
literally that forbids one transaction, because a parse failure inside it would roll the receipt back
and the system would have nothing to show for a file it rejected.

**Decision.** Two transactions.

1. **The receipt.** The original bytes, their hash, and status `received`. Committed before anything
   reads the contents. `INSERT ... ON CONFLICT DO NOTHING` on the unique `content_hash` index —
   never a lookup followed by an insert, which has a window in which two deliveries both find
   nothing, both insert, and the constraint ends up doing the work anyway while the lookup provided
   false reassurance.
2. **The outcome.** Either the lines plus status `parsed`, or status `quarantined` with a reason. One
   transaction, so a batch can never hold a partially trusted subset: the lines and the status that
   vouches for them commit together or not at all.

A crash between the two leaves the batch at `received` with no lines — recoverable and honest.
Re-delivering the same payload **completes** it rather than starting again, which is not duplicate
work but the work that was interrupted.

**The lock is not decoration.** The second transaction claims the batch with `SELECT … FOR UPDATE`
before deciding anything. Without it, two concurrent deliveries of one payload both observe status
`received` and both proceed to interpret: the unique constraint on
`(settlement_batch_id, line_number)` catches the second write — the guard working exactly as
intended — but the loser gets an integrity error where FR-1 says a re-delivery should be a no-op.
**Found by running two ingests concurrently**, not by reading the code; the single-threaded tests all
passed. The lock makes the second caller wait and then observe a finished batch.

The database remains the final guard. The lock turns a correct-but-ugly constraint violation into an
orderly no-op; it does not replace the constraint, and a test still shows a hand-written duplicate
being refused by the index.

---

## ADR-042 — Tolerance bands: absolute, per-currency, one minor unit (resolves OPEN-2)

**Status:** Accepted (M2.2). **Resolves OPEN-2**, which the plan required to be settled for this
increment.

OPEN-2 asked which dimensions carry tolerance, what the defaults are, and whether they are
per-currency. `IMPLEMENTATION_PLAN.md` §2.2 requires the bands to be **configurable**; neither it nor
`PROJECT_SPEC.md` states a number, and nobody on this project has measured a real settlement feed.
The values below are therefore a **declared project decision, not an empirical finding**, and they
are labelled as such in the code that carries them.

### The dimensions

| Dimension | Decision |
|---|---|
| **Amount** | Absolute, per currency. **Not** a percentage |
| **Date** | A hard eligibility window in whole days, not a band |
| **Reference** | No tolerance, because there is no reference rule at all |
| **Currency** | Never. Equality is absolute |

**Absolute, not relative.** A percentage band absorbs more money the larger the movement, which is
exactly backwards for a control: the differences that matter least are the small ones, and a
proportional band is most permissive precisely where the stakes are highest. A rounding artefact is
an absolute quantity — it does not scale with the amount that produced it.

**Per currency, because "one minor unit" is three different numbers.** 0.01 in EUR, USD and GBP; 1 in
JPY, which has no minor unit; 0.001 in BHD. A single figure would be three different policies wearing
one value.

### The defaults

`EUR/USD/GBP 0.01 · JPY 1 · BHD 0.001 · value_date_window_days 1`

**One minor unit, not two.** In this system a tolerance match means the difference is *dropped*:
there is no compensating posting, the line is marked matched, and the few cents never reach the
ledger. A residual, by contrast, is eventually booked — that is the path the rest of the project
builds. The two errors are therefore not symmetric. A band that is too tight costs an analyst a
glance; a band that is too loose leaves the ledger permanently wrong by that amount with nobody ever
shown it. One minor unit is one rounding at the precision the source itself uses. Two independent
roundings can compound to two units, and a band of two would absorb them — but it would equally
absorb a genuine two-cent shortfall, and there is no evidence here to prefer that.

**A currency with no declared band gets no tolerance at all** — not a zero band, no band. An
undeclared currency means nobody has decided what is immaterial in it, and the safe reading of
"undecided" is exact-match-only. Fail-closed.

### The exact semantics

1. **Inclusive.** `difference <= band`. The policy states the largest difference that may be
   absorbed, so that value is admissible; an exclusive reading would make the documented number the
   first one *refused*, which is not what "largest permitted" means. Tested below, at and above the
   band for every declared currency.
2. **The date window is applied first, and to every rule.** It is a hard filter, not a band: a
   candidate outside it is not considered by the exact rule either. Amount tolerance is evaluated
   only among candidates that already passed currency equality and the date window.
3. **Signs are compared, not stripped.** The comparison is `abs(line.amount - entry.amount)`, so a
   debit never matches a credit: −326.92 against +326.92 is a difference of 653.84, not zero.
4. **Zero is not special-cased.** The absolute-difference rule handles it like any other value, so
   0.00 matches 0.00 exactly and 0.01 within tolerance.
5. **Tolerance never crosses currencies.** Currency equality is a hard filter ahead of every rule.
   §3 lists a conversion policy engine as a non-goal and §13 records that rates arrive as inputs, so
   two amounts in different currencies are not near each other — they are incomparable. The
   presentment and FX columns the settlement file carries are not read by the matcher at all.
6. **Ambiguity is refused, never resolved by a wider band.** Two candidates inside the band leave the
   line unmatched (ADR-043). A tolerance band decides *whether* a difference is immaterial; it never
   decides *which* of two entries a line belongs to.

### Why this is narrow and reversible

The policy is one frozen, typed value passed as an argument — not ambient configuration, not
constants scattered through comparisons. Changing a band is a visible edit to one object, and the
absorbed difference is recorded on every tolerance match in `match_result.tolerance_applied`, so a
historical decision can be re-judged against a different policy without re-deriving it.

**Recorded limitation.** These numbers rest on reasoning about rounding, not on measurement. If this
project ever acquires a real settlement feed, the bands are the first thing that should be re-derived
from it, and this ADR should be amended rather than quietly reinterpreted.

**Measured consequence.** On the `bulk` profile at 200 instances, **81.9% of lines clear
deterministically with no model call** — 169 exactly and 7 by tolerance, with no ambiguity. The
canonical corpus clears 4 of 17, which reports the shape of a one-of-each catalogue rather than the
matcher's reach; both of its near misses differ by two minor units and therefore stay residual.

---

## ADR-043 — A match must be unique from both sides, or it is not a match

**Status:** Accepted (M2.2)

The obvious matcher walks the settlement lines and lets each take the first ledger entry it likes.
That is greedy, and greedy is order-dependent: two lines of 10.00 against one entry of 10.00 produce
a different winner depending on which line is considered first — and "first" then means whatever
order the rows came back in.

**Decision.** A pair is accepted only when the line has exactly one eligible candidate **and** that
candidate is claimed by exactly one line. Everything else is ambiguous and stays unmatched.

A financial match decided by query order is a defect even when both answers look reasonable, because
nothing in the business says the first row wins. Worse, consuming the wrong entry does not merely
mislabel one line: `match_result` is unique on `ledger_entry_id` (ADR-024), so the entry is gone, and
the line that genuinely owned it can never be reconciled. The mistake is not visible and not
recoverable by re-running.

**Ambiguity is information, not an obstacle.** Two lines competing for one entry match nothing, and
that is the honest answer: the system cannot tell which movement the entry represents. Both lines
remain unmatched and become residual work for M2.3, where a human sees them — which is precisely what
the residual path exists for.

**Precedence does not launder a guess.** Rules are applied exact-first, and a rule's accepted pairs
leave the pool before the next rule runs. There is no fallback tie-break, and none should be added
without a business rule that says which candidate wins and why.

**An unresolved contest is withdrawn from every tier below it** — the ambiguous line *and* every
entry it was contesting. Both halves matter, and the second is the one that would be a defect if
omitted: blocking only the line would release the entries it was claiming, and a *tolerance* match
could then take an entry that an *exact* claim was still arguing over. Precedence would be inverted
by the very step meant to protect it.

This was originally left implicit, on the reasoning that a line ambiguous at the exact tier is
necessarily ambiguous at the tolerance tier too — its exact candidates are a subset of its tolerance
candidates. That reasoning is correct, and it was verified rather than assumed: the adversarial case
(a line with two exact candidates and one tolerance-only candidate) matches nothing, both before and
after the rule was made explicit. But the safety was an **accident of these two rules**, holding only
while every lower tier is a superset of every higher one, and nothing in the code said so. A future
rule selecting a different candidate set would have silently begun resolving higher-tier ambiguity at
a lower tier. The block is now enforced rather than emergent; behaviour is unchanged, and the tests
pass identically either way.

**Proven, not asserted.** Every permutation of a small adversarial candidate set produces the same
pairing, and the same world built in two different insertion orders reconciles identically against
real PostgreSQL. A greedy implementation passes every other test in the suite and fails those two.

**And measured for precision, not only for clearance.** Every pair the matcher produces is graded
against the scenario each row was *constructed* for: a pair is correct when both sides come from the
same constructed scenario, and any cross-scenario pair is a false match produced by coincidence.
Across the canonical corpus and bulk corpora of 215, 1,075 and 4,300 lines: **zero false matches**.
Ambiguity rises with volume (0, 0, 2, 20) while false matches stay at zero — coincidences are
refused, not resolved, which is the whole point of the rule.

## ADR-044 — An exception may exist only for a line the ledger did not reconcile

**Status:** Accepted (M2.3)

An exception is the control record for a residual. An exception attached to a *matched* line is a
contradiction with consequences: it says a line both reconciled and needs a decision, and everything
downstream — evidence, a treatment proposal, an approval, an adjustment, a ledger posting — is built
on top of it. The posting would be for a movement the ledger already carries.

**A check constraint cannot express it**, because the fact lives in another table. Three options were
weighed and two rejected.

An **application check** was rejected on the reasoning ADR-028 arrived at the hard way: a control that
depends on application discipline is a convention that holds until someone writes a second code path.
A **trigger** was rejected because a relational constraint can express this cleanly, and a trigger
that duplicates a foreign key is a second mechanism to keep correct.

**Decision: carry the value and let a composite foreign key verify it**, the pattern ADR-028 corrected
its way into.

| Key | Effect |
|---|---|
| `settlement_line UNIQUE (id, match_state)` | Gives the key something to reference |
| `exception.line_match_state` + `CHECK (= 'unmatched')` | The column can hold exactly one value |
| `exception (settlement_line_id, line_match_state) → settlement_line (id, match_state)` | The referenced pair is `(id, 'unmatched')`, so the row exists only while the line really is unmatched |

The denormalised column is not the liability it was in ADR-028. There, `approving_principal` could
hold anything a writer asserted, and the correction was to verify it. Here the check constraint pins
it to one value, so there is nothing for a second code path to get wrong: the column is a *shape*
that makes the foreign key say what a check constraint cannot.

**The same key refuses the reverse, and that is a feature rather than a side effect.** Marking a line
matched while an exception claims it fails, because the tuple the exception references would cease to
exist. That is the invariant read from the other end. Once a residual has become an exception it has a
decision path of its own, and matching it later — after a fresh ledger snapshot, say — would silently
revoke a claim the system had already made, leaving one line with two resolutions and no record of
the reversal. Withdrawing an exception is workflow, and workflow is a later increment's; until then
the honest behaviour is to refuse.

**A foreign key resolves a race by failing, which is safe and expensive.** A line matched between
M2.3's read and its insert would abort the whole classification run over one row, and the mirror case
would abort a whole matching run. So both writers re-check under a row lock taken **in the same
order**: matching drops lines that acquired an exception, classification drops lines that acquired a
match. Whichever arrives second observes the other's decision instead of colliding with it, the
shared ordering keeps them from deadlocking over rows the other holds, and the foreign key stops
being the mechanism and becomes what it should be — the backstop for anything that bypasses both,
including direct SQL. Proven by two deliberately interleaved tests that block one writer on the
other's lock rather than gathering two calls and hoping they overlap.

**The lock covers the evidence, not only the subject**, and the first version did not. A
classification is derived from the state of *other* rows: three unreconciled rows on one order read
as a `fee_split`, and if the gross is matched before the write lands, two fee rows are persisted as a
split whose capture has gone. The composite key cannot catch that — the rows it constrains are still
unmatched, and the conclusion is wrong for a reason no constraint can see. Found by review, proven
against a real database, and fixed by locking the residuals *and* the movements that explain them,
re-reading them under that lock, and only then classifying. It is the reason classification runs in
one transaction where matching runs in two: matching's decision concerns one line and one entry and
the unique constraints arbitrate a stale proposal at write time, so it can afford to think outside a
transaction. Nothing arbitrates a stale *group*.

**The cost, stated.** Matching now reads the exception table. The scope guard that previously banned
the import outright was narrowed rather than lifted, and replaced with two that state the actual
rule: matching may observe *that* a line is under exception control, and may not write the table or
read what the control says. A blanket ban would have been a proxy for those, and the proxy stopped
being true the moment the two increments had to coexist.

---

## ADR-045 — The exception taxonomy: three classes reachable, two declared and unassigned (resolves OPEN-3)

**Status:** Accepted (M2.3), **amended by ADR-046**, which replaced this ADR's direction-based
reversal evidence with the movement type the PSP declares. The taxonomy and the reachability
analysis below still stand; the *evidence* each rule requires is stated in ADR-046.
**Resolves OPEN-3**, which the plan required to be settled before this increment.

OPEN-3 asked whether FR-4's six proposed classes survive contact with the corpus. The answer is that
**four survive as decidable outcomes and two do not**, and the reason the two fail is structural
rather than a gap in the rules.

### What a residual actually offers

After M2.2, a residual line carries what `settlement_line` persists: a PSP reference, a merchant
reference, a signed amount, a currency, a value date, and whether it matched. The ledger side offers
`external_ref`, `account_code`, an amount, a booking timestamp and a free-text `description`.

**No deterministic key links a settlement line to a ledger entry.** Neither reference appears in the
other system's record. Amount, currency and date are exactly what M2.2 already matches on — and where
they identify an entry uniquely, M2.2 has already consumed it. A line is residual *precisely because*
those three did not resolve it. Measured on the corpus, the gap is not marginal: at 4,300 lines a
residual typically shares its currency and date window with two hundred unconsumed entries. The only
remaining route is substring-matching the ledger description, which M2.2 refused for the same reason
(`matching.policy.MatchRule`) and which this increment must not introduce.

That single fact decides the taxonomy. **Any class that asserts a relationship to one particular
ledger entry is unprovable.** What *is* provable is the relationship between settlement lines
themselves, keyed on the merchant's own reference — an exact key the PSP passes through, needing no
canonicalisation and no fuzzy comparison — together with each line's match state, which is the whole
of what the ledger has said about it.

### The classes

Each is stated the same way: identifier, meaning, minimum evidence, what distinguishes it, what may
**not** be used to infer it, precedence, and the fallback when evidence runs out.

---

**`chargeback_reversal`** — rule `reversal_of_booked_debit`

*Meaning.* A credit that undoes a debit the ledger already carries, with nothing in the ledger to
match the undoing.

*Minimum evidence.* The subject is unmatched with a positive amount and a merchant reference;
**exactly one** other line on that reference and currency has the exact negated amount and is
matched.

*Distinguishes it.* Direction plus the counterpart's match state. A credit reversing a *booked* debit
is a reversal of something the ledger has; a credit beside an unbooked debit is two rows nobody has
reconciled, which is a different condition.

*Must not be used to infer it.* The ledger's `account_code` (a chart-of-accounts semantic is OPEN-4's
to define, and a second uncoordinated mapping here would pre-empt it); the ledger `description`; the
PSP reference's shape. The corpus builds reversals as `X` and `X-rev`, so a classifier reading the
PSP reference could score perfectly on this corpus while encoding one generator's naming habit.

*Precedence.* Disjoint from both other rules: from `cross_period_refund` by the sign of the
subject, and from `fee_split` because that rule requires *zero* booked offsets where this one
requires exactly one.

*Fallback.* Two booked counterparts both offsetting exactly → `unclassified`. Ambiguity refuses
rather than picks, the same discipline M2.2 applies to candidate entries.

*Superseded by ADR-046.* This rule originally fired on direction alone, and recorded as a limitation
that it could not prove the original debit was a **chargeback** rather than a fee reversal or a
correction — then assigned the class anyway, because it was the taxonomy's only reversal class. That
was the wrong call: taxonomy structure is not transaction evidence. The rule now requires the PSP's
declared movement type on both sides, and `settlement_line` persists it.

---

**`cross_period_refund`** — rule `reversal_of_booked_credit_across_periods`

*Meaning.* A refund that settles in a later accounting period than the capture it reverses.

*Minimum evidence.* The subject is unmatched with a negative amount and a merchant reference;
**exactly one** other line on that reference and currency has the exact negated amount and is
matched; the two value dates fall in different calendar months.

*Distinguishes it.* The direction is an accounting fact, not a corpus artefact: a capture is a credit
and its refund a debit, while a chargeback is a debit and its reversal a credit.

*Must not be used to infer it.* The current month, the run date, or any clock — a classification that
moved with the day it was re-run would not be a classification. Nor a day count: "different period"
is exactly a calendar-month boundary, and one day across a month end qualifies while twenty-nine days
inside one does not.

*Period definition.* `YYYY-MM` of the settlement `value_date`, the format `adjustment.period` already
commits to. M2.3 **detects that a boundary was crossed**; it does not assign a posting period, which
is OPEN-4's decision and M2.4's to make.

*Precedence.* Disjoint from both other rules, for the same two reasons as above.

*Fallback.* A refund settling in its own period → `unclassified`. The taxonomy has no in-period
refund class, and borrowing this one would make the class name false on every such row.

**This is where the review found the increment's one real defect.** Declining on the period test is
correct, but a declining rule used to leave the line available to `fee_split`, so an in-period refund
came back a fee split as soon as the order carried one more unmatched credit — a customer refund
labelled a PSP deduction, and an order's unrelated row changing the class of the refund. Fixed by
excluding any line with a booked exact offset from the group rule. See the precedence section below.

---

**`fee_split`** — rule `deductions_split_across_rows`

*Meaning.* One economic movement the PSP reported across several rows — a gross capture and its fees
— which the ledger booked as a single net entry, so no individual row equals any individual entry.

*Minimum evidence.* Among the unreconciled lines sharing the subject's merchant reference and
currency there is at least one credit and at least one debit, and **the debits together are strictly
smaller than the largest credit**.

*Distinguishes it.* Strictness is doing real work rather than tidying: it is what a deduction *is*, a
fee comes out of a capture and cannot exceed it. Without it the rule would also fire on a chargeback
and its reversal when neither reconciled — equal and opposite, which is an offset, not a deduction —
and would label a reversal pair a fee split.

*Must not be used to infer it.* The memo text ("looks like a fee"); the PSP reference stem, which the
corpus builds as `X`, `X-fee1`, `X-fee2`; and the sum of the group against a candidate ledger entry,
which would be matching by another name.

*Precedence.* Does not arise: this rule requires **zero** booked exact offsets, so a line the
reversal family has a claim on is never available to it. That is stronger than ranking it below the
reversal rules, and it is the fix for the defect recorded above — evidence about the line itself
excludes evidence about the company it keeps, rather than merely outranking it.

*Fallback.* Rows of one sign, or deductions that equal or exceed the largest credit → `unclassified`.

---

**`partial_capture`** — **declared, assigned by nothing**

*Meaning.* The merchant captured less than the ledger accrued.

*Why unreachable.* "Less than **the ledger accrued**" names a specific entry. Identifying it needs a
key that does not exist. What remains observable is a lone residual capture with a merchant
reference — a shape shared by an FX rounding difference, a near-amount miss and a line that lost a
matching ambiguity. At canonical scale a classifier could appear to resolve it, because the corpus
holds one instance of each; at volume that is a classifier that works on a toy.

*Must not be used to infer it.* "The only nearby larger entry", which is a matcher with a weaker rule
and would consume evidence M2.2 declined to consume.

*Fallback.* `unclassified`. Measured: 140 of 833 residuals at bulk 4,000, every one of them
unclassified and none given a neighbouring class.

---

**`fx_rounding`** — **declared, assigned by nothing**

*Meaning.* The settlement and the ledger converted the same movement independently and landed a
minor unit or two apart.

*Why unreachable.* Two pieces of evidence are missing, not one. The ledger's own converted amount
needs the entry to be identified, and the fact that a conversion happened at all lives in
`presentment_currency` and `fx_rate` — which M2.1 normalises and `settlement_line` does not store.
The only trace of FX in persisted data is the word inside a ledger description, and classifying on
that is the substring guess this increment is forbidden to make.

*Must not be used to infer it.* "A difference of two or three minor units is a rounding artefact." It
is not evidence of a *currency conversion*; a single-currency near-miss produces the identical shape,
and the corpus contains 158 of them. Naming this class without conversion evidence would put a cause
into a financial control record that nothing supports.

*Fallback.* `unclassified`. Measured: 55 of 833 residuals at bulk 4,000.

---

**`unclassified`** — rule `no_rule_matched`

*Meaning.* No rule could prove a condition from the available evidence.

*Why it is a feature.* Insufficient evidence is not permission to invent a class. The exception still
exists, still carries provenance, and still reaches an analyst — the system has simply not pretended
to know more than it does. `no_rule_matched` is a rule identifier of its own rather than an absence,
so a row recording it is distinguishable from one written before this classifier existed.

---

### Precedence — declared, and deliberately never exercised

`RULE_PRECEDENCE` is an explicit tuple. Every rule is evaluated for every residual and the
highest-precedence firing rule wins; evaluating all of them rather than returning at the first hit is
what makes precedence inspectable, because a rule then cannot win by being written earlier in the
file.

**The rule set is pairwise disjoint, so the order decides nothing.** The two reversal rules differ by
the sign of the subject, and the group rule requires zero booked exact offsets where both reversal
rules require exactly one. A test sweeps the shapes that could plausibly collide and asserts at most
one rule fires.

That is not how it started, and the difference is the increment's most useful finding. The rule set
originally admitted one overlap and this list resolved it correctly. What a precedence list cannot
resolve is a higher-priority rule that **declines**: it orders the rules that *fire*, so a reversal
rule examining a line and then failing its last condition left the line to be settled by the group
rule. Two reachable inputs went wrong — an in-period refund, and a subject with two booked offsets
whose reversal evidence was ambiguous. Both were named `fee_split`.

It is the same defect as ADR-043's, in a new place: **an unresolved higher-priority claim must never
be settled by a lower-priority rule.** M2.2 learned it about matching tiers and fixed it by
withdrawing contested entries; M2.3 learned it about classification rules and fixed it by excluding
any line the reversal family has a claim on from the group rule, whatever that family concludes. The
rule set is now built to have no overlap rather than to resolve one, and `RULE_PRECEDENCE` remains as
the decision a fourth rule would need rather than as something today's answers depend on.

Neither input occurs in the committed corpus — verified at all four scales — so the measured table
below was never wrong. That is exactly why it was worth finding by adversarial review rather than by
measurement: a corpus that does not contain a case cannot fail on it.

### Measured, at four scales

| Corpus | Residuals | Correct | Under-classified | **Wrong** | No declared intent |
|---|---|---|---|---|---|
| `canonical` | 13 | 9 | 3 | **0** | 1 |
| `bulk` @ 200 | 39 | 23 | 9 | **0** | 7 |
| `bulk` @ 1000 | 207 | 115 | 46 | **0** | 46 |
| `bulk` @ 4000 | 833 | 460 | 195 | **0** | 178 |

**No wrong deterministic classification at any scale**, and precision on assigned classes is exactly
1: everything that got a name got the right one. Coverage is 43% at bulk 4,000, and the shortfall is
almost entirely the two unreachable classes. That ordering is deliberate — a wrong class is the first
step of a wrong posting, while an unclassified residual is a decision a human makes.

Every instance of a reachable class is classified, which is what stops the precision figure being
cheap: a classifier that fired once and abstained forever would also report zero wrong answers.

### What would change the answer

Persisting the PSP's declared `transaction_type` turned three inferences into declarations. **Done at
the M2.3 correction — see ADR-046.**

It did **not** make `partial_capture` or `fx_rounding` reachable: those need the ledger entry
identified, which is a different and harder problem, and one this project should solve by giving the
ledger snapshot a settlement reference rather than by guessing.

## ADR-046 — A class is assigned from declared evidence, never from direction

**Status:** Accepted (M2.3, correction). **Amends ADR-045.**

ADR-045 reached `chargeback_reversal` from *direction*: a residual credit that exactly reverses a
debit the ledger already carries. It recorded the limitation honestly — "this does not prove the
original debit was a **chargeback** rather than a fee reversal or a correction" — and then assigned
the class anyway, on the reasoning that within a closed taxonomy whose only reversal class is this
one, mapping there was the sanctioned broader-class fallback.

**That reasoning was wrong, and the flaw is worth naming precisely.** Taxonomy structure is not
transaction evidence. "This is the only class that could describe it" says something about the
enumeration, not about the movement, and a control record that says `chargeback_reversal` because
nothing else was available is asserting a cause the data does not support. The same error would
justify any class that happened to be alone in its category.

**Measured, not argued.** Three credits were ingested through the real path, identical in sign,
amount, currency, value date and counterpart — each exactly reversing a booked debit on its own
order — and differing only in the movement type the PSP declared: `chargeback_reversal`,
`refund_reversal`, `adjustment`. All three came back `chargeback_reversal`. Two of those three
statements were false, and each would have carried a wrong class into a treatment, an approval and a
posting. A fourth case, a declared `chargeback_reversal` whose booked counterpart was a *capture*,
was also accepted.

### Why the fix is to persist the declared type rather than to drop the class

Dropping the rule and mapping those residuals to `unclassified` was the obvious narrow fix, and it
was rejected because **it does not stop at one rule**. The same objection applies to
`cross_period_refund` — a debit reversing a booked credit is equally a refund, a chargeback, a
clawback or a correction — and to `fee_split`, where a credit with smaller unreconciled debits could
as easily be a capture with partial refunds. Applying the safety rule consistently removes all three
and leaves a classifier that assigns nothing but `unclassified`.

That would not be an honest limitation. It would be a self-inflicted one, because the evidence
exists and the system already reads it:

| | |
|---|---|
| Is the movement type in the approved contract? | **Yes** — ADR-031 declares `transaction_type` as column 3 of the settlement format |
| Does M2.1 have it? | **Yes** — `NormalisedLine.transaction_type`, parsed and validated |
| Is it persisted? | **No** — `settlement_line` had no column, so it was validated and discarded |
| Did anything forbid the column? | **No** — §9's data model is "indicative, not final; settled at M1" |

M2.1's own decision record settles it: those fields "remain available in the immutable raw payload
**for the increments that need them**". This is that increment. FR-4's taxonomy is a taxonomy of
movement kinds — capture, fee, chargeback, refund — and a classifier without the kind can only read
the sign.

**Decision.** Persist `transaction_type` on `settlement_line` (migration `46dcf131f47d`), and require
declared evidence in every rule. Nothing else from the format is added: presentment amount,
presentment currency and FX rate would not make `fx_rounding` reachable, because that class needs the
ledger entry identified and no deterministic key does that. A column that changes no outcome is
schema for its own sake.

### The rules after the correction

| Rule | Class | Evidence |
|---|---|---|
| `reversal_of_booked_chargeback` | `chargeback_reversal` | This row is a declared `chargeback_reversal`; **exactly one** movement on the order is a declared `chargeback` the ledger reconciled; it is the exact negation |
| `refund_of_booked_capture_across_periods` | `cross_period_refund` | This row is a declared `refund`; exactly one reconciled `capture` on the order is its exact negation; different calendar months |
| `fees_deducted_from_a_capture` | `fee_split` | This row is a declared `capture` or `fee`; the order carries at least one unreconciled row of each; the deductions are strictly smaller than the largest inflow |

Both halves of each rule matter. A declared type nobody corroborates is a claim rather than a fact —
a `chargeback_reversal` reversing a booked *capture* is refused — and a corroborating shape with no
declaration is the defect this ADR corrects.

**The closed vocabulary lives in the classifier, not in the column.** `settlement_line` stores what
the file said, constrained only to be non-blank. A `CHECK` on the value set would mean a PSP adding a
product could not be ingested at all: the receipt is committed before the payload is read (ADR-041),
so a rejected INSERT would strand a batch that can never reach `parsed` or `quarantined`, and
re-delivery would reproduce it forever. Quarantining instead would condemn a whole settlement file
over one unfamiliar row, which is not what a malformed file means. So an unrecognised type ingests
normally and maps to *no evidence*: it can only ever produce `unclassified`. Fail-closed at the
decision, not at the boundary.

No case folding and no aliasing, for the reason ADR-039 gives: accepting `CAPTURE` as `capture`
would be deciding that two spellings denote the same movement, which nobody has recorded. The
conservatism costs coverage, never correctness.

### What it changed, and what it did not

**Measured results are identical** — 13, 39, 207 and 833 residuals, zero wrong at every scale, the
same per-class counts. The old rules were right on this corpus and wrong in general, which is exactly
why the defect needed an adversarial case rather than a measurement: the corpus never contained a
credit whose declared type disagreed with its shape.

`partial_capture` and `fx_rounding` remain unreachable, unchanged by this. They need the ledger entry
identified, which a movement type does not provide.

Matching is untouched and must stay so: a rule that consulted the declared type would make two
amounts reconcile or not depending on what the PSP called them, which is a matching policy nobody
has decided and ADR-042 does not contain. A scope test asserts the matching package cannot reach the
column.

---

## ADR-047 — Account mapping, period assignment, and what the calculator refuses (resolves OPEN-4)

**Status:** Accepted (M2.4). **Resolves OPEN-4**, which the plan required to be settled before this
increment.

OPEN-4 asked how a treatment maps to a ledger account and how a period is assigned for cross-period
cases, under one constraint: **configuration, not code**, and deterministic.

### The shape, and why it decides more than it looks like it does

`AccountPolicy` is a closed table keyed by the two structured values a decision is made from — the
exception's classification and the approved treatment. The calculator consults it and does not
contain it; no account code appears in a formula anywhere.

The consequence is the part worth stating: **what can be priced is configuration too.** A combination
with no configured account is not calculable, so the set of priceable cases is a table rather than a
set of branches, and widening it later is a change to data. There is no default account and no
fallback, so the failure mode is absence rather than a wrong posting.

The policy is built from a *sequence* of rules rather than a mapping literal, because a `dict`
resolves a duplicate key by keeping whichever came last — a silent choice between two ledger
accounts. Configuring a pair twice raises where it is written.

### The mapping

Account codes are **synthetic demo configuration**. Four are the fictional chart the M1.3 corpus was
built around — it was created with "enough to make account selection a real decision for M2.4" — and
`6900` is declared for the same fictional organisation because writing a residual off has to land
somewhere. A real deployment replaces the table and nothing else, which is what makes this narrow and
reversible.

| Classification | Treatment | Account | Period |
|---|---|---|---|
| `chargeback_reversal` | `rebook` | `4900` chargebacks | settlement period |
| `chargeback_reversal` | `accrue` | `4900` chargebacks | originating period |
| `chargeback_reversal` | `write_off` | `6900` write-offs | settlement period |
| `cross_period_refund` | `rebook` | `4100` revenue | settlement period |
| `cross_period_refund` | `accrue` | `4100` revenue | originating period |
| `cross_period_refund` | `write_off` | `6900` write-offs | settlement period |

`ACCRUE` and `REBOOK` share an account within a class and differ only in period. That is what the two
treatments *are* here: the same restatement, recognised either when it settled or in the period it
economically belongs to.

### The amount, the sign, and the currency — one rule for all three

**The amount is the settlement movement's own, unchanged, sign included.** An exception *is* a
movement the ledger does not carry, so restating it is restating that amount. The treatment chooses
where it lands and in which period; it never changes the number.

That is deliberately the only formula in the increment, and it is what makes the containment argument
structural rather than procedural. A model will one day influence the treatment code. If a treatment
could move an amount, that influence would reach money — it cannot, because there is no arithmetic
for it to reach.

Direction is the sign, because `adjustment` holds one signed amount against one account. There is no
debit/credit pair to reverse, and a case needing a two-legged posting is refused rather than
approximated.

Currency is the movement's own, and there is no conversion. A movement in a currency the books are
not kept in is refused: §3 lists a conversion policy engine as a non-goal and no deterministic rate
source is approved. The presentment and FX columns the settlement format carries are **not** consulted
— a rate the PSP recorded for its own conversion is not this ledger's rate, and treating it as one
would be inventing an FX policy at the moment of posting.

### Periods

`YYYY-MM`, the format `adjustment.period` already commits to, read from **business dates only**. No
clock is consulted anywhere; a guard asserts the package never reaches for one.

* `REBOOK` and `WRITE_OFF` recognise the movement when it settled — the period of the settlement
  line's value date.
* `ACCRUE` recognises it in the period it economically belongs to: the period of the movement it
  reverses, supplied as a fact because the calculator performs no I/O. **Without one it refuses**,
  rather than falling back to the settlement date — that fallback would make `ACCRUE` produce the
  same instruction as `REBOOK` while claiming to be a different treatment.

A period before `earliest_open_period` is **closed and refuses**. No approved policy says where a
movement belonging to a closed period should go instead, and "the next open period" is a decision
nobody has taken; refusing keeps it with the people entitled to take it.

### What is not priced, and why each absence is deliberate

`fee_split` is absent for every treatment. A fee split is one movement the PSP reported across
several rows and the ledger booked its *net*: pricing one row would post part of a movement whose
whole the calculator cannot see, and would double-count what is already booked. The correct treatment
is a two-legged reclassification, which one signed amount against one account cannot express.

`unclassified` is absent. The system could not say what the residual is, so it cannot say which
account restates it. An account here would be a guess wearing configuration's clothes.

`partial_capture` and `fx_rounding` are absent because no exception can carry them (ADR-045).
Configuring an account for a class nothing produces would assert a capability that does not exist.

`escalate` is refused before the policy is consulted, and cannot be configured at all: `adjustment`
forbids a row for one outright (§6.2), so an account mapped to it could never be used and its
presence would imply otherwise.

### Rounding: declared, and never applied

Every priced amount is a settlement line's own, which ingestion already constrained to four decimal
places (ADR-020), so no supported formula can produce a value needing to be rounded. The quantum and
the mode are still recorded on every result — §7 requires them alongside it, and a future formula
that does need rounding should inherit one declared rule rather than choose its own. `ROUND_HALF_UP`
rather than banker's rounding: an adjustment is a single restatement a human approved, not a long
series where bias accumulates.

An amount outside the money contract is **refused, not rounded**. Inventing a rounding rule so a
number satisfies the schema is the defect ADR-020 exists to prevent, and it would be the same defect
here.

### Refusal order, chosen from evidence

Escalate, then whether the combination is priceable at all, then the values, then the period. The
account check comes before the currency check because the first ordering reported
`currency_not_functional` for a GBP residual nobody could classify — true, and the wrong thing to
hand an operator, who would chase an exchange rate when the real blocker is that the system cannot
say what the movement is. A combination that *is* mapped falls through and reports whichever value
check actually stopped it, so the earlier check never masks a real blocker.

### Measured

Priced across the whole deterministic path — match, classify, price — and graded against what each
line was constructed for. **Zero wrong financial instructions** at every corpus size, under both
`REBOOK` and `ACCRUE`:

| Corpus | Residuals | Priced | **Wrong** | Refused: no account | Refused: currency |
|---|---|---|---|---|---|
| `canonical` | 13 | 1 | **0** | 11 | 1 |
| `bulk` @ 200 | 39 | 2 | **0** | 33 | 4 |
| `bulk` @ 1000 | 207 | 10 | **0** | 177 | 20 |
| `bulk` @ 4000 | 833 | 40 | **0** | 713 | 80 |

Coverage is 4.8% at scale and that is the honest number, not a disappointing one. Most residuals are
`unclassified` or `fee_split` and neither is priceable; of the classes that are, the corpus's
chargeback reversals settle in USD while the demo books are EUR, so they refuse rather than convert.
That last case is the most instructive in the corpus: classified, mapped, open period, everything
lining up — and still refused, because the one thing missing was a rate nobody has approved. A
calculator that quietly used the settlement number would have produced a plausible instruction that
was wrong by an exchange rate.

### Scope

M2.4 **persists nothing**. The plan's deliverable is a pure function, and no `adjustment` row can
exist before an approval authorises one (M5). No schema change, no migration, no dependency. Nothing
here derives an operation identifier, writes an attempt record or dispatches anything — the presence
of those columns in the M1.2 schema is not a reason to reach for them, and a guard asserts the
package cannot.

---

## ADR-048 — The treatment set closes into four values (M3.1 kill-test gate)

**Status:** Accepted (M3.1). **This is a gate**, not an ordinary increment: the plan states that if
real cases require the model to propose an amount, the type-level containment claim is false and must
be **dropped, not softened**. It is not.

### The question

The whole design rests on one sentence: a model may choose a *treatment*, and nothing else. That is
only true if the set of treatments is genuinely finite — if some real case needed an action outside
it, or a number inside it, the model would need a channel the architecture does not have, and the
containment claim would be marketing rather than a property.

So this increment asks the question before anything is built on the answer, and while there is still
no model in the codebase to make the answer convenient (ADR-003).

### The vocabulary

`REBOOK · ACCRUE · WRITE_OFF · ESCALATE`, exactly as `PROJECT_SPEC.md` §6.1 and
`IMPLEMENTATION_PLAN.md` §3.1 name them. Declared once, in `db.control.TreatmentCode`, and referred
to everywhere else.

| Code | Meaning | Priceable by M2.4? |
|---|---|---|
| `rebook` | Post the movement the ledger is missing, in the period it settled | Where the class has a configured account (ADR-047) |
| `accrue` | Recognise the same movement in the period it economically belongs to | Same classes, and only with a known originating period |
| `write_off` | Recognise the residual as a loss rather than as the movement it appeared to be | Where an account is configured for it |
| `escalate` | Refer to a human because it cannot be resolved deterministically | **Never.** `adjustment` refuses a row for it and the account policy refuses to map one |

**Valid and priceable are different contracts**, and conflating them is how a vocabulary grows. Every
member above is always a legitimate instruction; whether the calculator can price one depends on the
exception it is applied to. A treatment it cannot price is not an invalid treatment — it is a case
that escalates.

**Abstention is not a fifth member.** `treatment_proposal` carries a separate `abstained` flag, and a
check constraint requires an abstaining proposal to carry `escalate`. A model declining to answer has
still not chosen an action; giving that its own code would let a refusal to decide look like a
decision.

### Why four is enough — the argument

`ESCALATE` is what closes the set, and it is worth being precise about why.

Every other member names something the system *does*. `ESCALATE` names the absence of that: the case
leaves the deterministic path. Without it, every condition the system cannot price would want its own
treatment, and the action vocabulary would grow with the exception taxonomy — one for unpriceable fee
splits, one for unidentifiable partial captures, one for each new condition anybody discovers. With
it, the set of **actions** stays at four while the set of **conditions** grows freely. Those are
different axes, and keeping them separate is the whole trick.

The second half of the argument is that no case needs a *number* from the chooser. Every priced
amount is the settlement movement's own, unchanged: the treatment selects an account and a period,
never a quantity (ADR-047). A treatment like `write_off_125_50` or `adjust_by_0_7_percent` would be an
amount smuggled through the one channel a model is allowed to use, so a guard asserts no member's
name or value contains a digit.

### Measured over the corpus

Every exception the pipeline produces, at three sizes, under all four treatments:

| Corpus | Exceptions | Priced by some treatment | Escalate is the answer | Instructions produced | Amounts contributed by a treatment |
|---|---|---|---|---|---|
| `canonical` | 13 | 1 | 12 | 3 | **0** |
| `bulk` @ 200 | 39 | 2 | 37 | 6 | **0** |
| `bulk` @ 1000 | 207 | 10 | 197 | 30 | **0** |

**No case fell outside the four, and no priced amount differed from the settlement movement's own.**
Every refusal came from the enumerated set — `no_account_mapped` (531 of the 591 refusals at the
largest size) and `currency_not_functional` (60) — so no case was refused for a reason the
calculator has no name for, which is the shape a missing treatment would take.

Two corrections to how this was first written, both from adversarial review, both worth recording
because they are the difference between evidence and decoration:

*The original table had a column "Resolved inside the vocabulary: 13 / 39 / 207."* Given the
definition of resolved — priced, or refused by all four so escalate answers — that column is an
identity. It equals the exception count for any corpus, any calculator, any taxonomy. It was
presented under "Measured" as though it were an observation. The number that carries the argument is
the one it displaced: **10 of 207 exceptions can be priced at all.**

*That rate is a property of the demo account policy, not of the vocabulary.* `unclassified` and
`fee_split` are mapped to no account on purpose (ADR-047) — an exception the system cannot name must
not receive an automatic one — and every `chargeback_reversal` in this corpus is in a non-functional
currency. The 197 that escalate are resolved, and none of them needs a *model* to supply a number;
they need a human, which is what escalation means. What would have failed this gate is a case
wanting a fifth action or a number inside a treatment, and there is none.

The evidence for "no amount is contributed" therefore rests on 39 instructions from one
classification, `cross_period_refund`. That is narrower than the table's shape suggests, and it is
stated here rather than left for a reader to derive.

`escalate` is never priced, at any scale. Asserted separately and labelled a constant: the
calculator answers `escalate` before reading a fact, so no corpus can falsify it. The first version
of the exit-criterion test hid behind exactly that — `assert priced or escalate_refused` is
`assert priced or True` — and passed against a corpus of five fabricated exceptions. The assertion
now pins the measured counts, and both attacks that beat it were re-run and now fail.

### A hole the gate found, and closed

`TreatmentCode` is a `StrEnum`, so a member compares and hashes equal to its own value. A bare
`"rebook"` string therefore walked through a mapping keyed by members and **obtained a priced
financial instruction**. `"escalate"` was worse: it slipped past the identity check that exists to
stop escalation ever being priced, so the one treatment that must never produce an instruction
stopped being recognised as itself.

mypy rejects both, and that was the whole defence. mypy will not be in the room when M3.2
deserialises a provider's JSON response — at that boundary a treatment arrives as *text*, which is
exactly the shape that got through. The calculator now refuses anything that is not a genuine member,
with a closed `treatment_not_recognised` reason, checked before every other question. A refusal
rather than a raised error, because the calculator is total.

**The first fix was itself too weak, and adversarial review broke it.** It tested `isinstance`, and
`str.__new__(TreatmentCode, "accrue")` is an instance of the class without being any member of it.
It was priced — into the **rebook** period, because the escalate and period branches compare by
identity while the account table resolves by equality, so the two halves of the calculator disagreed
about what they had been handed. An instruction labelled `accrue` posting where `rebook` posts is a
worse outcome than a refusal. Membership is now `any(treatment is member for member in
TreatmentCode)`, the only test both halves agree on, and a test confirms every legitimate
construction route — the member, `TreatmentCode(value)`, `TreatmentCode[name]`, copy, deepcopy,
pickle — returns the singleton, since identity is only safe if they do.

The same review found a second one. `AccountPolicy` is a frozen dataclass holding a plain `dict`, so
assigning into `DEMO_ACCOUNT_POLICY.rules` after construction produced an instruction posting to
`NOT-AN-ACCOUNT`: every validation in `__post_init__` was an *entry* check rather than an invariant.
The table is a read-only snapshot now. That matters more than ordinary immutability hygiene, because
`adjustment.account_code` carries no database constraint — the policy is where account-code shape is
enforced at all. Giving the column its own constraint is a schema change and belongs to a later
increment; it is recorded in `PROJECT_STATUS.md`.

Finding these here rather than in M3.2 is the argument for building the gate before the model, in one
sentence.

### What makes the closure checkable rather than intended

* one declaration, found **structurally** — any class whose string members overlap the vocabulary in
  more than one place is a treatment vocabulary, whatever it is called, and a second one fails;
* no module in the money path may contain a treatment literal in code, and the two that branch on a
  treatment must reference the canonical type;
* the two hand-written SQL check constraints that spell `'escalate'` are asserted to agree with the
  enum, since they are the only place the vocabulary is repeated outside it;
* arbitrary strings — case variants, whitespace, numeric shapes — are refused by the enum and again
  at the money boundary;
* **every one of these guards is shown failing.** Each is re-run against a deliberately mutated copy
  — a rogue enum with a fifth member, one with a numeric member, a second vocabulary, a hardcoded
  string in the calculator, the same drift one directory outside the money path, a module that stops
  naming the type, a guard handed nothing to inspect, and the runtime check removed — and must
  reject it, then must accept the clean copy. Mutations are made to in-memory copies so a crashed
  test cannot leave one behind, and a further test asserts none reached disk.

The literal scan started out inspecting `money/` only, and a reviewer walked the drift one directory
over: `demo/snapshot.py` picks the treatment it prices with, so a literal there is the identical
defect somewhere the guard was not looking. Naming the modules that may hold a treatment was the
mistake. The scan is package-wide now, with `db/control.py` the single exemption, and it needs no
list to maintain.

### Scope

No model, no provider, no prompt, no proposal generation, no evidence assembly, no schema change and
no dependency. OPEN-5 remains open: which providers to implement is M3.2's decision and nothing here
anticipates it.

---

## ADR-049 — Two providers behind one port, and the closed proposal contract (M3.2)

**Status:** Accepted (M3.2). **Resolves OPEN-5.**

### The vocabulary of this increment

M3.1 proved the *treatment* set closes. This increment answers the next question: what else may a
model say, and how does anything it says get into the system? The answer is a five-field contract
and one interface, and the design goal for both is that the dangerous thing is not *guarded against*
but *unrepresentable*.

### The contract (`PROJECT_SPEC.md` §6.1, implemented verbatim)

| Field | Type | Why it is safe |
|---|---|---|
| `treatment` | `TreatmentCode` | The canonical M3.1 enum, imported not restated. Four values. |
| `confidence` | `ConfidenceBand` | `LOW · MEDIUM · HIGH`. A band, never a score. |
| `rationale` | `str` | Provenance for humans. No code parses it and none can (§6.2). |
| `evidence_refs` | `list[EvidenceRef]` | `{evidence_id: str}` — opaque pointers that carry no values. |
| `abstained` | `bool` | A flag, not a fifth treatment. |

**No numeric type anywhere in the tree**, and `extra="forbid"` on every model in it. A provider that
returns `{"treatment": "rebook", "amount": 125.50}` does not produce a proposal with an ignored
extra — it produces a validation error.

Three choices inside that are worth recording:

**`strict=True`, and `model_validate_json` on the wire path.** This boundary parses text a third
party produced. In Pydantic's lax mode `"true"` becomes `True` and `1` becomes an enum member, which
is precisely how a malformed response turns into a plausible-looking domain object. Measured against
the real contract: `treatment=1`, `treatment=true`, `treatment="REBOOK"`, `treatment=" rebook"`,
`confidence=0.9`, `abstained="true"`, `abstained=1`, `rationale=["text"]`, `evidence_refs="EV-1"`
are all refused.

**`frozen=True`.** A validated proposal is a record of what a model said, and a record later code can
edit is not provenance. It also makes the abstention rule an invariant rather than an entry check —
the M3.1 account-table lesson applied before it could be repeated.

**No maximum on `rationale`.** The specification sets none and the column behind it is `Text`.
Inventing a limit here would truncate or reject provenance the system was told to keep.

### Abstention: the implication, not the equivalence

`abstained ⇒ treatment == ESCALATE`. One direction, matching the database constraint
(`NOT abstained OR treatment = 'escalate'`) and ADR-048.

Escalating *without* abstaining stays valid, and the distinction is real: a model that read the
evidence and concluded a human must decide has **made a decision**; one that declined to answer has
not. Collapsing them would lose exactly what the audit trail exists to keep, and would reject valid
output that the table it is stored in accepts. A contradictory pair (`REBOOK` + `abstained=true`) is
refused rather than normalised — silently clearing one field would decide on the model's behalf
which half it meant.

### OPEN-5 — the two providers

**Anthropic** (`claude-opus-5`) and **OpenAI** (`gpt-5.4-mini-2026-03-17`). Pinned 2026-09-01.

Why these two:

* **Separate vendors, separate infrastructure.** Not two front doors onto one model, which OPEN-5
  explicitly warns against. A gateway and its upstream would prove nothing about portability.
* **Both do enforced structured output**, by different mechanisms — `output_config.format` against
  `response_format.json_schema` with `strict: true`. The difference is the point: the port has to
  fit both, so it is shaped around neither.
* **Genuinely different response shapes.** One returns the answer as a text block inside a content
  list; the other as a JSON string inside a message inside a choice. An abstraction that only ever
  saw one of them would look fine until the day it was swapped.
* **Deterministic offline testing.** Neither adapter performs I/O, so the whole layer is provable
  without a paid call — which is what lets the eval gate run in CI on cassettes (§20).

The identifiers are stamped differently because the vendors number things differently: OpenAI's
snapshot date is part of the identifier, Anthropic's current identifiers carry no date component and
appending one names a model that does not exist. So that pin's date is recorded here instead.

**They are not tier-matched**, and that is stated rather than hidden: a frontier model against a
small one. This pin exists so the port has two real implementations and so a measurement has
something to name. The three-arm comparison (6.3) measures accuracy, USD per 1,000 lines and p95,
and may re-pin to comparable tiers to make that table a fair comparison — a one-line change,
reviewed, with a new date.

### The port

```
TreatmentProposer (Protocol)
    provider   : ProviderId
    model_id   : str
    propose(prompt: ProposalPrompt) -> TreatmentProposal
```

What crosses it is the validated contract and nothing else — never a vendor object, a `dict`, raw
JSON or free text. A failure raises `ProviderResponseError`; there is deliberately **no** "assume
escalate" fallback, because a caller that cannot tell a real abstention from a parse failure would
record a decision no model made. What to do about the failure — queue for a human, never block the
deterministic path (NFR-11) — belongs to 3.3.

**No provider SDK is a dependency, and none is imported.** The adapters build and parse wire-level
JSON with a `Transport` injected. Three reasons, in order of weight: with no SDK in the tree there
is no vendor class anywhere that *could* leak past the adapter; JSON is the only form a cassette can
replay in CI without a key; and the alternative would add two large dependencies whose only exercised
code path in this increment would be one nobody can run offline. **No transport implementation ships
here** — the one that speaks HTTP arrives with the cassette harness at 3.4.

Authentication belongs to the transport, never the adapter. `ProviderRequest` is `{path, body}` with
nowhere to put a header, so no adapter, fixture or recorded cassette can come to hold a credential.

### Sent to the provider: structure, not prose

Pydantic folds every docstring in the tree into `description` keys. Those docstrings are the
engineering argument behind a financial control — including a worked example of the numeric escape
hatch this project refuses — and shipping them on every call would be hundreds of wasted tokens and
a needless disclosure. `proposal_wire_schema()` is the same structure with the prose removed: 714
characters, same properties, same enums, same `additionalProperties: false`, same `required`. Both
copies are checked by every guard, so the stripping step cannot become a place to hide something.

What the model should *do* stays in the prompt (3.3), which is where instructions belong.

### The guards, and the mutations that kill them

`PROJECT_SPEC.md` §23 makes two of these acceptance criteria for the whole project — the schema
guard must fail "when a numeric field is deliberately added" (§23.4) and the boundary guard "when
the calculator is made to import the proposal model" (§23.5).

Six schema guards walk the tree — root, `$defs`, `anyOf`/`oneOf`/`allOf`, array items, nested
objects — checking: no numeric type and no numeric enum value; `additionalProperties: false` at
every boundary; no property name that names money, an account, a period or a posting; no free-form
object or open map; treatment is exactly the canonical four; confidence is a closed non-numeric
band. Field **names** only, never descriptions — prose legitimately discusses the words the list
forbids, and a guard that fires on its own documentation gets weakened until it is switched off.

Each is shown failing against a deliberately broken copy, and the schema mutations are **real
Pydantic models** a developer could actually write — a class with `amount: Decimal`, one with
`confidence: float`, one with `extra="allow"`, one with `metadata: dict[str, Any]`, one with
`account_code: str`, one with a nested `adjustment_amount` — rather than doctored dictionaries. The
boundary guard is killed four ways and the vendor-import guard three, each against an in-memory copy
of the source. Nothing is written to disk.

### What this increment does not do

No live call, and nothing here could make one: no HTTP client is imported anywhere under `llm/`. No
evidence assembly, no prompt construction, no persistence — the `treatment_proposal` table stays
empty until 3.3 — no approval, no posting, no schema change, no migration, and no new dependency.

Two M3.1 scope fences were retargeted rather than deleted. `not (PACKAGE_ROOT / "llm").exists()`
forbade exactly the package this increment was for; what survives from it is the half that was
always the real claim, that no provider SDK is imported anywhere. `"TreatmentProposal(" not in
source` stopped being true the moment the response contract existed, and narrowed to what still
holds: nothing constructs the ORM row or builds proposal provenance.

---

## ADR-050 — What evidence a model may see, and how a candidate is chosen (M3.3)

**Status:** Accepted (M3.3). Supersedes nothing; records two decisions and one gap that adversarial
review forced into the open.

### Only two of FR-5's five evidence kinds can be assembled

FR-5 names dispute reason text, merchant memo, support-ticket notes, remittance references and
candidate ledger entries. The system holds two of them.

| Kind | Source | Assembled |
|---|---|---|
| `remittance_reference` | `settlement_line.psp_reference`, `merchant_reference`, `transaction_type` | **yes** |
| `candidate_ledger_entry` | `ledger_entry`, selected as below | **yes** |
| `merchant_memo` | a settlement-file column, parsed and validated at ingestion — and then dropped | no |
| `dispute_reason` | no source system | no |
| `support_ticket_note` | no source system | no |

The memo one is the uncomfortable one and is worth stating plainly: `NormalisedLine.memo` exists,
ingestion validates it, and `settlement_line` has no column to put it in, so it is discarded at
persistence. The corpus deliberately contains empty and ambiguous memos as scenario features, which
means the most discriminating evidence FR-5 names is the evidence this system throws away.

Adding the column is a migration, an ingestion change and a regeneration of the byte-hashed fixture
corpus — too much blast radius for the increment whose deliverable is the assembler, and a change
that should be made deliberately rather than as a side effect. Recorded as **OPEN-14**.

The enum keeps all five members. The taxonomy is what FR-5 asks for; `ASSEMBLED_KINDS` is what the
system can produce, and a test asserts the three unavailable kinds are never emitted so that a
future increment which starts persisting a memo has to change this decision on purpose.

### A candidate is a near miss, not a would-be match

**The first implementation had this exactly backwards, and the measurement is the argument.**

It selected entries inside the matcher's own tolerance band — same currency, amount within the
band, value date within the window. That set is by definition *the set that would have matched*: an
entry inside it which is also unique is an entry the matcher took, and a matched line cannot carry
an exception. So candidate evidence could only ever appear where the matcher was ambiguous or where
the ledger had moved since. Across the committed corpora:

| Corpus | Residuals | Residuals with candidate evidence |
|---|---|---|
| `canonical` @ 200 | 13 | **0** |
| `bulk` @ 200 | 39 | **0** |
| `bulk` @ 1000 | 207 | **2** |

The two that did fire were the harmful case: a contested entry presented to *two* exceptions as an
exact, same-day, unconsumed match, with the contest — the single fact that explains why those lines
are exceptions — mentioned nowhere. Two proposals could each recommend rebooking against one ledger
entry, which is the double-count the matcher's mutual-uniqueness rule exists to prevent, reintroduced
one layer up.

So the rule is inverted. A candidate is one of the **nearest** unconsumed entries in the same
currency within a wide date window:

* **Currency** must match. A cross-currency comparison is not a near miss, it is a different
  question, and §7 forbids implicit FX anywhere.
* **`EVIDENCE_WINDOW_DAYS = 35`**, deliberately wider than the matcher's one-day window. The
  matcher's window is an eligibility filter; this is a relevance filter, and five weeks covers the
  cross-period cases the taxonomy names.
* **Ranked by proximity** — absolute amount delta, then day delta, then `external_ref`, then the
  entry id as a total tie-break — so the order is deterministic and the closest survive the cap.
* **`MAX_CANDIDATES = 5`**, with the number omitted stated in the pack. A cap rather than a token
  budget, because the specification sets no budget and inventing one would be inventing a number.
  It exists because the fan-out is otherwise quadratic in the commonest ambiguity shape: 30
  identical charges produce 30 exceptions of 31 items each, 930 rows from one settlement file.

Every candidate carries **why the matcher did not take it** — `inside_tolerance_unmatched`,
`outside_amount_band`, `outside_date_window`, or both. The first of those is the label that fixes
the contested-entry case: an entry the matcher was eligible to take and did not take was refused,
and saying so is the difference between evidence and a trap.

The predicate calls `TolerancePolicy.band` and `.within_window` — the matcher's own API — rather
than reimplementing the arithmetic. The first version inlined it and claimed it "cannot drift from
the matcher because it is the matcher's own policy object"; a reviewer changed the matcher's
semantics through that API and the assembler carried on unaffected. The policy *data* was shared;
the predicate was a copy.

### Selection is by explicit rule, never by resemblance

There is no identifier relating a settlement line to a ledger entry in this system — `external_ref`
is a GL reference and has nothing to do with a merchant's own — so "same merchant, therefore
relevant" was not merely unacceptable, it was unavailable. Proximity in amount and date is the only
relationship there is, it is the same one the matcher uses, and every candidate states its distance
so a reader can see how weak or strong the resemblance is.

Cross-exception isolation does not rest on that, though. It rests on the evidence id: `uuid5` over
`(exception_id, kind, discriminator)`, so the same ledger entry offered to two exceptions produces
two ids and neither exception's citation can resolve to the other's evidence.

---

## ADR-051 — Replay is matched on the whole request, and a cassette fault is never a provider fault (M3.4)

**Status:** Accepted (M3.4). Supersedes nothing. Records four decisions and one withdrawn claim.

### The plan's goal in one line: CI evaluation with no live API key

The exit criterion is that the suite runs offline from cassettes. It does: no credential, no socket,
and a guard that fails the build if any module under `llm/` imports an HTTP client. The evaluation
increments that will replay in anger are 6.1 to 6.3.

### A cassette answers a request only if the bytes match

Replay is keyed on `sha256` over a domain tag, an identity version, the request path and the
canonical body — **the whole body**, not the prompt. So anything that changes what a provider is
asked changes the key: the prompt, the response schema that constrains the answer, the output
ceiling, the model, the path.

The consequence is the point. **Staleness detection is not a separate mechanism; it is the absence
of a match.** There is no list of things to remember to invalidate, and no way to add a field to the
request and forget to bump something. A harness keyed on the prompt hash alone would have replayed
happily through a changed response schema, reporting an answer produced under different constraints
as though it were the same call.

Two constants, not one. `CASSETTE_VERSION` is the on-disk format; `IDENTITY_VERSION` is what a
request identity means. Folding them together — which is how it was first written — meant that
adding a field to the *file* would silently re-key every fingerprint and every derived id, orphaning
any stored provenance pointing at them. Adding a field to a file is not a change to what was asked
of a provider.

### A cassette miss is not a provider outage

`CassetteError` is deliberately **not** a `ProviderError`, and `sent()` re-raises it untranslated —
the one exception to the rule that every vendor failure becomes one of the port's two errors.

If a miss were reported as unavailability, an offline suite would keep passing while testing
nothing, and the flow would record `UNAVAILABLE` against an event that never involved a provider.
The failure would read as weather rather than as a bug. There is no fallback from replay to a live
call: not a discouraged one, not a configurable one.

### Capture fails closed, and cannot be reached by accident

Recording requires `CASSETTE_CAPTURE=1` exactly, checked at **construction** rather than inside
`send`, so no path records first and checks afterwards. Empty, `0`, `true`, `yes` and `TRUE` all
mean no.

**The switch is deliberately outside the `LECP_` namespace.** §17 asks for it to be documented in
`.env.example`; `Settings` forbids unknown `LECP_` names and rejects them from `.env` as well as
from the process environment, so a `LECP_`-prefixed switch documented there would break startup for
anyone who copied the example into a real `.env`. Verified rather than assumed: `Settings()` raises
`extra_forbidden` on `LECP_CASSETTE_CAPTURE` in a dotenv file, and ignores the unprefixed name.

The recording transport also owns no socket. It wraps whatever transport it is handed, which is how
the recording half of the harness exists without an HTTP client entering the package — and therefore
without weakening the guard that says none may.

### The committed cassettes are synthesised, and the format says so

`Origin` is `CAPTURED` or `SYNTHESISED`, only a recording transport may claim the former, and a test
asserts it over every cassette in the tree. The committed ones are synthesised: this repository holds
no credential and nothing here has ever spoken to a provider.

What they exercise is real — the adapters' own parsing, the fingerprint, scrubbing, canonical
serialisation, determinism. What they are **not** is evidence about how any model behaves. Recording
that difference in the file matters more than usual here, because 6.3 will publish measurements
produced from cassettes, and a reader has to be able to tell which kind they came from.

The answers carry no ground truth. Treatments are assigned by sorted position over exception ids —
an arbitrary ordering — because deriving them from the fixture generator's intended classification
would bake the answer key into the artifact a later evaluation is meant to measure *against*.

For the same reason a synthesised recording carries **no `usage` block**. Both vendors return token
counts and 6.3 computes cost from those fields rather than estimating it, so a synthesised
`{"input_tokens": 0}` would read as "this call was free" rather than "nobody measured this call" —
a fabricated zero inside a published cost figure. Absence is the only encoding that cannot be
mistaken for a measurement, and a test enforces it over every synthesised interaction. Captured
cassettes will carry usage, and should. The
builder lives under `tests/` for the same reason expressed as a fence: no module in the package may
import the fixture corpus, and the M3.3 firewall failed the build when the builder was first written
inside `llm/`. Moving the file was the correct response; adding it to the firewall's allowlist would
have been weakening a guard to accommodate a file.

### Withdrawn: "scrubbing is answer-preserving"

The first version asserted that a scrubbed payload parses **identically** to an unscrubbed one. That
is false, and it is withdrawn rather than reworded. Evidence packs carry third-party text, so a model
can quote a credential-shaped string back inside its own `rationale`, and scrubbing rewrites it.

The invariant that replaces it is the one that actually matters, and both halves are asserted:

- every field a decision is made from — `treatment`, `confidence`, `evidence_refs`, `abstained` —
  survives scrubbing exactly;
- a secret never reaches a committed file, even when a model quoted it, and the free text loses it.

That trade is correct rather than merely necessary, because `rationale` is provenance for humans and
no code path parses it (§6.2).

### What review changed

Six reviewers produced thirty-eight findings. Each was reproduced before being accepted; one was
refuted by direct measurement. The corrections worth recording:

| Finding | What was wrong | Evidence |
|---|---|---|
| The network guard | Watched `socket.connect` only, and its control used a *blocking* connect — blind to every async connection, the only kind the port makes | A probe: an async connect produced **0** `socket.connect` calls under the proactor loop and 1 under the selector loop. Two reviewers confirmed, one refuted; the probe settled it. `BaseEventLoop` turned out not to define `sock_connect` at all, so the obvious fix would have hooked nothing |
| The exit criterion | Called the transport's lookup directly and asserted things true by construction | Mutants that ignored the fingerprint, fabricated an answer, or returned one recording for every request all passed the full suite |
| The §17 secret scan | Banned the substrings `sk-`, `authorization` and `x-api-key` — that is, what *correct* scrubbing leaves behind — and fired on "risk-based" | It would have failed on the first real capture. It now matches secret **values**, with an independent key check beside it |
| §17 scan coverage | `glob`, not `rglob`, and vacuous over an empty tree | A planted leaking cassette one directory down passed all three guards; so did deleting the directory |
| `evidence_refs` | `frozen=True` blocks assignment, not mutation of the list a field holds | A reviewer emptied a validated proposal's citations *after* the citation check that exists to prevent exactly that. Now a tuple — third occurrence of this shape, after `AccountPolicy` and `ProviderRequest` |
| `dict(response)` in the recorder | Ran before the port's own non-object check, so a provider that answered with an array became "unreachable" | An answering provider misreported as an outage sends an operator to the network |
| Exception messages | A client library's exception carries the request URL and its headers | A reviewer traced a key into `ProposalOutcome.detail`, which a later increment writes to an audit row. Messages are redacted; the exception is still chained |
| `Interaction.prompt_hash` | Stamped once per recorder, so every row after the first was wrong | A capture run sends many prompts through one recorder. Field removed; the hash belongs on the proposal, where the flow computes it per call |
| `finish_reason` | `content_filter` was unhandled while the module docstring claimed otherwise | It left `content` null, so a filtered answer arrived as "content is not a JSON string" — a malformed-provider diagnosis for a provider behaving as documented |

Eighteen of the defects are held by mutation tests: each is reintroduced mechanically and must turn
the suite red. All eighteen do.

### What 3.4 deliberately did not build

No transport that speaks HTTP, no captured cassette, no scorer, no threshold, no evaluation gate, and
nothing writing `treatment_proposal.cassette_id`. Carrying transport-level provenance up through the
port is a design change the plan does not ask 3.4 for; it belongs with 6.1 to 6.3, and until then an
unwritten column is more honest than a plumbed one nothing reads.

---

# Open decisions

Not yet decided. Each names what must be settled and by when.

**OPEN-1** (settlement file format) was resolved at M1.3 as ADR-031, **OPEN-2** (tolerance band
configuration and defaults) at M2.2 as ADR-042, **OPEN-3** (the exception taxonomy) at M2.3 as
ADR-045, and **OPEN-4** (account mapping and period assignment) at M2.4 as ADR-047. All four are
removed from this list rather than left with a "resolved" marker, because an open-decisions list that
accumulates closed items stops being read.

## OPEN-5 — Which two model providers — **RESOLVED at M3.2 (ADR-049)**

Anthropic (`claude-opus-5`) and OpenAI (`gpt-5.4-mini-2026-03-17`), pinned 2026-09-01. Separate
vendors, both with enforced structured output by different mechanisms, and genuinely different
response shapes — which is what makes the port's portability claim testable rather than asserted.
Not tier-matched; 6.3 may re-pin for a fair `Measured` comparison. See ADR-049.

## OPEN-13 — Recording which evidence pack a proposal was shown

**Must decide:** whether `treatment_proposal` gains a column identifying the pack sent with the
request, or whether the citation table is widened from "evidence the model cited" to "evidence the
model was shown".
**Why:** `prompt_hash` proves what was sent, and checking it later needs the pack. Evidence rows are
never rewritten — correctly, since an audit record that silently updated itself would be worse — so
a fresh assembly after the ledger moves produces a different pack from the one an older proposal
saw, and that proposal's hash is no longer re-derivable from the database. Two reviewers
demonstrated it at M3.3.
**Constraint:** a column is a migration, which M3.3 did not own.
**Needed before:** the console shows an auditor what a model was given (M7), or the scorer grades a
replayed proposal against the pack it actually saw (6.1, 6.2).

**Status after M3.4:** still open, and the harness did not force it. Replay matches on a fingerprint
of the request rather than on stored provenance, and nothing writes `treatment_proposal.cassette_id`
yet, so a cassette can be replayed without the column existing. What that does *not* solve is the
original problem — re-deriving an old proposal's `prompt_hash` from the database after the ledger
has moved — which is why the decision stands.

## OPEN-14 — Whether `settlement_line` should persist the merchant memo

**Must decide:** whether to add a `memo` column, persist it at ingestion, and extend the fixture
corpus, so `merchant_memo` becomes assemblable evidence.
**Why:** FR-5 names it, ingestion already parses and validates it, and the corpus deliberately
contains empty and ambiguous memos as scenario features — so the most discriminating evidence the
requirement names is currently discarded at persistence. See ADR-050.
**Constraint:** a migration, an ingestion change, and a regeneration of the byte-hashed corpus.
**Needed before:** the measured provider comparison (6.3) claims to be evaluating models on the
evidence FR-5 specifies.

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
