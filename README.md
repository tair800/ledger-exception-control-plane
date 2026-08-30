# ledger-exception-control-plane

PSP settlement files against the general ledger. Deterministic matching clears the bulk; the model
proposes a treatment from a closed enum whose type has no numeric field. A chaos suite proves no
double-post against a RED baseline that does.

> ## Status: milestone M2.2 of 31
>
> **What exists:** the local Docker Compose stack (PostgreSQL, Redis, app), typed configuration,
> liveness and readiness endpoints with bounded dependency probes, structured JSON logging with
> correlation-id propagation, the tooling baseline, a green CI gate, the **complete database
> schema** with Alembic migrations, a **deterministic synthetic fixture corpus**, and — as of
> M2.1 — **settlement ingestion**: a settlement file is received, hashed, persisted immutably,
> parsed, normalised, and either accepted as typed settlement lines or quarantined with a reason;
> and as of M2.2 — **deterministic matching**: those lines are reconciled against ledger entries by
> exact amount and by a per-currency tolerance band, with ambiguity refused rather than guessed.
>
> **What does not exist: everything after the match.** A line that fails to match is left unmatched
> and nothing describes *why*. Nothing detects a residual as a business event, classifies an
> exception, assembles evidence, proposes a treatment, computes an adjustment, obtains an approval
> or posts anything. There is no LLM integration, no ledger adapter, no dispatcher, no retry, no
> DLQ replay, no recovery workflow, no audit emission and no chaos suite. An `outbox` table is not a transactional outbox; a
> `posting_attempt` table is not a write-ahead protocol; **a fixture labelled `fee_split` is a
> constructed input, not evidence that anything can classify a fee split.** Everything below that
> is not listed as existing is a *specification of intended behaviour*.
>
> No measurement here is a result — the `Measured` table is an obligation the build must produce
> from a committed script, and it will not appear until it does.
>
> [`PROJECT_STATUS.md`](PROJECT_STATUS.md) tracks exactly what is built;
> [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) lists all 31 increments.

---

## The problem

A payments-heavy marketplace reconciles daily PSP settlement files against its general ledger. Most
lines match cleanly. A small residual never does — partial captures, fee splits, chargeback
reversals, FX rounding, refunds crossing period boundaries.

Finance operations resolves that residual by hand at month-end. The expensive failures are silent: an
adjustment posted twice because a retry fired, or posted against the wrong account, surfaces months
later when it has already flowed into reported revenue. By then the close is signed and the fix is an
audit finding rather than a correction.

## Target user

Finance operations or controllership lead at a payments-heavy marketplace or platform, roughly
100–1,000 people. Three roles use the system:

- **Finance operations analyst** — triages the exception queue, approves or rejects proposed treatments.
- **Controller** — sets thresholds, reviews the audit trail.
- **System operator** — works the dead-letter queue and runs replays.

## Business value

Unreconciled residual is a closing delay and an audit finding. The two countable outcomes are **hours
per close** and **adjustments posted in error**. Both are measurable against a documented baseline;
neither will be claimed in this README until a committed script produces the number.

## High-level architecture

```
settlement file
      │
      ▼
[1] ingest + normalise ─────────────► deterministic
      │
      ▼
[2] match with tolerance bands ─────► deterministic   (clears the bulk)
      │
      ▼  residual only
[3] exception created + evidence assembled
      │
      ▼
[4] model proposes a TREATMENT CODE ─► the only AI step
      │                                 closed enum · no numeric field · may abstain
      ▼
[5] human approves / edits / rejects ─► mandatory gate on every write
      │
      ▼
[6] amount computed ────────────────► deterministic, from ledger data only
      │
      ▼
[7] posting claimed under a retry-independent operation id
      │
      ▼
[8] transactional outbox ──► dispatcher ──► ledger adapter
      │                          │
      │                          ├── CONFIRMED → recorded
      │                          ├── REJECTED  → terminal → DLQ → replay CLI
      │                          ├── THROTTLED → scheduled retry (not a declination)
      │                          ├── NOT_SENT  → bounded retry → DLQ
      │                          │              (allowlisted transport errors only,
      │                          │               no byte written)
      │                          └── UNKNOWN   → capability branch:
      │                                          enforced key → re-send, within window+scope
      │                                          queryable    → reconcile, bounded
      │                                          neither      → manual recovery, no re-send
      ▼
[9] audit event + correlation id
```

Backend Python 3.12 / FastAPI / Pydantic v2 / SQLAlchemy + Alembic, PostgreSQL, Redis with arq
workers, a Next.js + TypeScript operations console, OpenTelemetry into self-hosted Langfuse, Docker
Compose, GitHub Actions, deploying to Fly.io with Neon Postgres.

The PSP settlement feed and the ledger API sit behind adapters, so the repository runs locally
against simulated sources with no third-party account.

## The AI / deterministic boundary

This is the centre of the design, so it is stated precisely rather than summarised.

| Concern | Owner |
|---|---|
| Normalisation, matching, tolerance bands | **Deterministic** |
| Which residual becomes an exception | **Deterministic** |
| Reading unstructured evidence and proposing a treatment | **Model** |
| Rationale and evidence references for a human to read | **Model** |
| Whether to act at all | **Human** |
| **Every monetary amount** | **Deterministic** |
| Idempotency, outbox, retry, DLQ, replay, recovery | **Deterministic** |
| Audit trail | **Deterministic** |

The model exists because the residual carries evidence no normalisation rule captures — PSP dispute
reason text, merchant memo lines, support-ticket notes, free-text bank remittance references. It
reads that evidence and proposes one of a fixed set of treatments, or abstains.

**The model cannot emit an amount.** Not because it is instructed not to, and not because a human
reviews it — because its output type has no numeric field anywhere in the schema tree, and because
the amount calculator's signature accepts a treatment code and ledger data, with no parameter through
which model text could reach it. A hallucinated amount is not unlikely; it is unrepresentable.

This repository also carries the portfolio's written **"why we did NOT use an agent here"**: the same
labelled exception set run through the deterministic matcher, an LLM-as-matcher baseline, and the
shipped hybrid, reporting accuracy, USD per 1,000 lines and p95. The expectation is that the LLM
matcher loses on all three. If it wins, that result is published as-is.

## Strongest differentiator

Commercial platforms already read ambiguous settlement data and route exceptions for approval, across
far more sources than this repository ever will — **Ledge.co** does this with 11,000+ bank connections
and 150+ native integrations, and on coverage and time-to-value this repository loses on every axis.

What those platforms do not let a reader verify is the property this repository exists to prove:

**Under the injected failures enumerated in the spec, the same approved resolution cannot produce two
ledger adjustments: this system initiates no duplicate write, and against the *simulated* reference
adapter — which both declares and honours the contract below — no duplicate is applied. Where an
adapter declares neither capability, the system refuses to act and escalates to a human, and that
refusal is itself the guarantee.** The proof is a committed chaos suite run against a deliberately
naive branch which *does* double-post. A suite that passes on both branches proves nothing; the RED
baseline is what makes the green result mean something.

Two honest notes on what that demonstrates. The unit is the **approved resolution**, not the exception
— one exception can legitimately produce a second operation if a resolution is superseded, which is why
supersession is interlocked while a prior operation is unresolved. And under the strong adapter the
suppression is performed by a simulated ledger written in this repository, so that branch proves the
dispatcher behaves correctly *given* an enforcing ledger — not that any particular real ledger enforces
anything.

### The guarantee is conditional, and the condition is published

"Exactly-once" is not achievable across a process boundary, and "effectively-once" is a *conditional*
property. Five guarantees are separated in [`PROJECT_SPEC.md` §13](PROJECT_SPEC.md). Only one holds
unconditionally in the strong sense; one holds unconditionally but is deliberately at-least-once; one
is ours but bounded by what we can know; one is an admission rule we impose on adapters; and the fifth
— the financial side effect — is conditional on the ledger:

| # | Guarantee | Holds |
|---|---|---|
| 1 | One claim per residual; one adjustment per operation id | **Unconditionally** — ours |
| 2 | Transactional outbox: intent is never lost | **Unconditionally** — and deliberately *at-least-once*, not once |
| 3 | No second dispatch for a known terminal outcome | **Ours, bounded by knowledge** — silent when the outcome is `UNKNOWN` |
| 4 | Adapter declares its capabilities; outcome is three-valued | **By contract** — an adapter that cannot express `UNKNOWN` is rejected |
| 5 | **Effectively-once financial side effect** | **Only when** the adapter enforces an idempotency key **or** exposes a queryable posting identity |

The reference adapter here declares both, so guarantee 5 holds and is claimed. Point this at a ledger
that declares neither and **the claim is withdrawn, not reworded** — the system records the outcome as
`UNKNOWN`, refuses to re-send an irreversible write automatically, and routes it to manual recovery.
The chaos suite runs that weak-adapter configuration too, because degrading correctly is part of the
demonstration.

Note the phrasing throughout: *effectively-once effect*, never "exactly-once" — and even that only
where the capability table permits it. The mechanism is a retry-independent operation identifier, a
unique constraint, a transactional outbox, and an adapter contract that can say "I don't know".

## Development

Python 3.12, managed with [uv](https://docs.astral.sh/uv/). The interpreter version is declared in
`.python-version` and read from there by both local tooling and CI, so the two cannot drift.

```bash
uv sync                          # install exactly what uv.lock pins
uv run ruff format --check .     # formatting
uv run ruff check .              # lint
uv run mypy                      # strict type check
uv run pytest                    # tests + coverage (gate: 90%)
```

The full gate, in the order CI runs it:

```bash
uv sync --frozen && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

### Local stack

```bash
make up            # build + start postgres, redis and the app; waits for health
make ps            # status
make logs          # tail structured application logs
make smoke         # integration tests against the running stack
make down          # stop and remove this project's containers
make down-volumes  # also delete its data volume
```

The stack binds to localhost only, on **non-default host ports** (`15432` for PostgreSQL, `16379`
for Redis, `8000` for the app) so it does not collide with a locally installed PostgreSQL or Redis.
The application never uses those mappings — inside the Compose network it reaches its dependencies
by service name — so they exist purely for attaching a local client.

Credentials in `docker-compose.yml` are **development-only placeholders** scoped to this stack. No
deployed environment may reuse them; deployment supplies its own values via `LECP_*` variables.

### Database schema and migrations

Persistence is SQLAlchemy 2.x with Alembic. `create_all()` is **not** the schema-management
mechanism — every change goes through a reviewed migration.

```bash
make db-up          # start PostgreSQL only (no Redis, no app)
make migrate        # alembic upgrade head
make migrate-down   # alembic downgrade -1
make schema-verify  # migrations + constraint enforcement against real PostgreSQL
```

Alembic reads its database URL from the application's `Settings`, not from `alembic.ini` — so no
connection string is committed and migrations cannot run against an environment the application
itself is not configured for.

**Tables at M1.2** — fifteen. The core reconciliation domain:

| Table | Purpose |
|---|---|
| `settlement_batch` | A settlement file as received, stored immutably with a unique `content_hash` |
| `settlement_line` | One normalised line within a batch, with explicit currency |
| `ledger_entry` | A general-ledger row available for matching |
| `match_result` | A line matched to an entry, with the rule and any tolerance applied |

…and the decision path and the machinery that is meant to make it safe:

| Table | Purpose |
|---|---|
| `exception` | A residual line needing a decision — one per line, classified, correlated |
| `evidence` | An addressable evidence record attached to an exception |
| `treatment_proposal` | Model output. **No numeric column of any kind** — see below |
| `treatment_proposal_evidence` | Which evidence a proposal cited, as a checked relation |
| `approval` | The human decision, versioned so a superseded resolution is a different operation |
| `adjustment` | The computed amount, with a **unique `operation_id`** |
| `outbox` | Dispatch intent, its attempt count and its last outcome |
| `posting_attempt` | Write-ahead record of one attempt: sent-at, `in_flight` or resolved |
| `dlq` | An exhausted dispatch and the envelope needed to replay it |
| `recovery_queue` | An ambiguous outcome awaiting reconciliation or an operator decision |
| `audit_event` | Append-only, contract v1 |

Six properties are enforced by the database rather than described in prose, because each is a claim
the project makes, and a claim asserted only in application code is a claim on trust:

- **The model cannot carry money.** `treatment_proposal` has no `NUMERIC`, no `INTEGER`, no numeric
  column at all — confidence is a closed band, not a score — and no amount-like column name. A test
  walks the table and fails on either. The response-schema guard arrives with the provider port
  in 3.2; this is the persistence half of the same rule.
- **An ambiguous outcome cannot be filed as a finished one.** `unknown`, `throttled` and
  `partially_applied` are all representable, and a check constraint forbids any of them — or a
  missing outcome — on an outbox row marked `settled`.
- **An attempt record cannot half-exist.** `(state = 'resolved') = (outcome IS NOT NULL AND
  resolved_at IS NOT NULL)`, so an attempt is either "sent, nothing known" or fully resolved.
- **`audit_event` is append-only**, enforced by a trigger that refuses `UPDATE`, `DELETE` and
  `TRUNCATE` from *any* role including the table owner. The insert-only grant to the least-privilege
  application role is defence in depth, not the primary control: a grant does not constrain the
  owner, and the owner is the identity a migration or a maintenance script runs as.
- **An adjustment cannot be authorised by a rejection.** `adjustment` references
  `(approval.id, approved_treatment, principal)`, not just `approval.id`. A plain foreign key proves
  an approval *exists*; it does not prove the approval said yes. A rejection carries
  `approved_treatment IS NULL` and the referencing column is `NOT NULL`, so the bad row is
  unreachable rather than merely discouraged. An `escalate` treatment cannot be posted either —
  escalation is what happens when no amount can be computed.
- **The segregation-of-duties check compares against a verified principal.** The approver's identity
  reaches `recovery_queue` through composite foreign keys from `approval` via `adjustment`, so it
  cannot be invented by whichever code path writes the recovery item. The same idiom binds a
  `posting_attempt` to its adjustment's `operation_id`.

No monetary value may hide in JSONB either — a check constraint rejects amount-like top-level keys
in the dead-letter envelope, which would otherwise bypass every money rule below.

**Money.** Every monetary column is an *unconstrained* `NUMERIC` mapped to Python `Decimal` —
deliberately **not** `NUMERIC(20, 4)`. A fixed-scale typmod does not reject an over-precise value, it
*rounds* it: measured on PostgreSQL 16, `Decimal("1.23456")` was stored as `1.2346` with no error.
Removing the typmod lets the original value reach a check constraint that rejects it instead:

```sql
CHECK (trunc(amount, 4) = amount AND abs(amount) < 10000000000000000)
```

Values with up to 4 decimal places are stored exactly; anything more precise, or beyond
±9999999999999999.9999, is **rejected, never rounded**. `trunc` rather than `scale`, because
`scale(1.230000)` is 6 and a scale-based rule would reject a value identical to `1.2300`.

Binary floating point is absent from the schema, asserted across all metadata rather than just the
known money columns. Every amount is paired with an explicit currency column under a
both-present-or-both-absent check.

### Settlement ingestion

The boundary that makes an untrusted file safe to build on. Raw bytes in; either typed settlement
lines or a quarantined batch out, and nothing in between.

```
raw bytes  ->  receipt (hash + immutable payload, committed)  ->  parse  ->  normalise
                                                                     |
                                            lines + status `parsed`  |  status `quarantined` + reason
```

**The receipt commits before anything reads the file** (FR-1). A malformed payload therefore leaves
behind exactly the bytes it was rejected for — the alternative is a quarantine record referring to a
file nobody kept. The content hash is taken from the original bytes, before decoding and before a
byte-order mark is stripped, so two different artifacts can never share one.

**Quarantine is batch-level.** One unreadable row condemns the file. Accepting the rows that happened
to parse would manufacture a trusted *partial* settlement file, and reconciliation over a partial file
does not produce fewer results — it produces wrong ones, because every dropped movement becomes an
unexplained residual. The reason is a code from a closed set of 15, plus a line and a column: bounded,
deterministic, and carrying neither the offending value nor an exception message. The payload is
already retained for the rest.

**Money comes from text and never touches a float.** `Decimal` straight from the string, after a
regex that admits only a plain signed decimal — `NaN`, `Infinity` and `1E+3` all construct perfectly
well as `Decimal` and are refused here. Over-precision is rejected, never quantised, using the same
value-based rule as the column: `120.450000` is accepted because four decimal places hold it exactly,
`1.23456` is not. `float` appears nowhere in the package and an AST guard enforces that.

**References are preserved exactly** — no case folding, no punctuation stripping, no whitespace
collapsing. How close two references must be before they denote one movement is a matching decision,
and M2.2 owns it; deciding it here would bake it into the persisted record where no later test could
vary it.

**Re-delivery is a no-op the database arbitrates.** `INSERT … ON CONFLICT DO NOTHING` on the unique
content hash rather than a lookup followed by an insert, with the batch claimed under
`SELECT … FOR UPDATE` before its outcome is decided. Two concurrent deliveries of one payload produce
exactly one batch and one set of lines, proven under real concurrency.

**Still absent, deliberately:** matching, tolerance, residual detection and classification. The
ingestion package imports nothing that would let it reach a ledger entry, and a test walks its AST to
keep it that way.

```bash
make db-up
make ingest-verify   # ingestion and quarantine against real PostgreSQL
```

### Deterministic matching

Settlement lines against ledger entries, by rule, with no model anywhere in the path.

| Rule | When it applies | Recorded |
|---|---|---|
| `exact_amount` | Same currency, inside the date window, identical amount | `rule_id`, no tolerance |
| `amount_within_tolerance` | Same currency, inside the date window, difference within the band | `rule_id`, the absorbed difference and its currency |

Exact outranks tolerance, and an accepted pair leaves the pool before the next rule runs.

**The tolerance band is one minor unit of the currency, inclusive** — 0.01 EUR/USD/GBP, 1 JPY,
0.001 BHD — with a one-day value-date window as a hard eligibility filter. Narrow on purpose: in this
system a tolerance match *drops* the difference, so an over-wide band leaves the ledger permanently
wrong by that amount with nobody ever shown it, whereas an over-tight one costs an analyst a glance.
Currency equality is absolute and no conversion happens anywhere; a currency with no declared band
gets exact matching only. The numbers are a declared project decision, not a measurement — see
ADR-042, which records that and what would have to change to improve on it.

**Ambiguity is refused, not resolved.** A pair is accepted only when it is the unique choice from
*both* sides: a line with two candidates matches nothing, and two lines competing for one entry match
nothing. That is what makes the result independent of the order rows arrive in — a greedy matcher
would let the query plan decide which line takes a shared candidate, and consuming the wrong entry is
not recoverable, because `match_result` is unique on the ledger entry.

An unresolved contest is withdrawn from every tier below it — the ambiguous line *and* the entries it
was contesting — so a tolerance match can never take an entry that an exact claim was still arguing
over.

**Measured on the `bulk` fixture profile at 200 scenario instances: 81.9% of lines cleared
deterministically**, 169 exactly and 7 by tolerance, with no ambiguity and no model call. The
canonical corpus clears 4 of 17 — it holds one instance of every condition, so its rate describes the
catalogue rather than the matcher.

**Clearance is not the interesting number; precision is.** Every pair is graded against the scenario
each row was constructed for, so a pair is correct only when both sides come from the same one.
Across corpora of 17, 215, 1,075 and 4,300 lines: **zero false matches**. Ambiguity rises with volume
while false matches stay at zero — as coincidences become more likely the matcher refuses more rather
than pairing more, which is the safe direction, because a consumed ledger entry is never released.

```bash
make db-up
make match-verify   # matching, tolerance, ambiguity and races against real PostgreSQL
```

**Still absent, deliberately:** anything that says *why* a line did not match. Residual detection,
the exception taxonomy and evidence assembly are M2.3, and the matching package imports nothing that
would let it reach an exception table — a test walks its AST to keep it that way.

### Deterministic fixture corpus

Later milestones need realistic input that is identical on every machine and every run. The generator
produces it from a seed alone:

```bash
make fixtures         # regenerate the committed canonical corpus
make fixtures-check   # fail if the committed corpus has drifted from the generator
make db-up
make fixtures-load    # load it into the disposable lecp_test database
make fixtures-verify  # prove it loads with every constraint enforced
```

The committed corpus lives in `fixtures/canonical/`: two settlement files, a ledger snapshot, the
records the loader consumes, scenario metadata, four deliberately malformed files, and a manifest with
a content hash. **Everything in it is synthetic** — no real customer, PSP, ledger or account data, and
a test asserts no artifact contains credential-shaped material.

**Twelve scenarios**, each stating what condition it represents, which later milestone needs it, and
what distinguishes it from its neighbours. Three are constructed to match; the rest cover every class
of FR-4's taxonomy, plus the awkward cases the corpus is deliberately built to contain: missing
merchant references, empty and ambiguous memos, a three-row fee split against one combined ledger
entry, opposing signed chargeback rows, a repeated PSP reference within one file, a refund settling in
the month after its capture, and a foreign presentment currency with a recorded FX rate.

Determinism is not a hope. Draws are `SHA-256(domain ‖ seed ‖ label)` rather than a random stream, so a
value depends on nothing but its own label; identifiers are UUIDv5, deterministic and visibly distinct
from the version 4 the application generates; time comes from a fixed epoch, and a test walks the
package's AST to prove no clock or random source is read. The corpus regenerates **byte-identically**,
and CI fails if the committed files drift.

**The mix is an apportionment rule, not an approximation.** The declared distribution is stated in
parts per 200, and integer counts come from Hare quota with largest remainder plus a floor that
guarantees every declared condition appears at least once. A corpus sized at a multiple of 200
reproduces the declared percentages exactly; above 200 every class is within one instance of its
ideal; below it the floor's cost is confined to the dominant class by construction. `--instances N`
produces exactly N. See ADR-037.

**The scenario labels are construction intent, not answers.** A scenario is a fee split because the
generator *built* it as one, never because anything ran a matcher over it — that matters, because this
metadata is what M2's matcher will later be judged against, and an oracle produced by the system under
test would measure only its own self-consistency. Where the honest answer depends on a decision nobody
has taken yet, the metadata says so: a line differing by one to three minor units is recorded as
`tolerance_policy_dependent` rather than as matched or residual.

The loader refuses any database whose name is not `lecp_test`, `lecp_demo` or `lecp_fixtures`, resets
by identifier rather than `TRUNCATE`, and never disables a constraint — a corpus that needed integrity
switched off to load would not be a loadable corpus.

**Timestamps** are `TIMESTAMP WITH TIME ZONE` throughout. `created_at` is generated by the
*database*; business timestamps (`received_at`, `booked_at`, `matched_at`) are supplied by the
*application*, which is the only party that knows the real event time.

### Health endpoints

| Endpoint | Meaning | Depends on PostgreSQL/Redis? |
|---|---|---|
| `GET /healthz` | **Liveness** — the process is alive and serving | **No, deliberately** |
| `GET /readyz` | **Readiness** — dependencies are reachable, so work can be accepted | Yes |

They are separate on purpose. An orchestrator restarts a container that fails liveness, so a liveness
probe coupled to the database would turn a brief outage into a restart storm. `/readyz` returns `503`
with per-dependency status when either dependency is unavailable, probes them concurrently under a
bounded timeout, never mutates them, and returns no DSN, credential or stack trace.

### Configuration

Environment-driven and typed (`pydantic-settings`), prefixed `LECP_`. Connection strings are held as
`SecretStr`, so they render as `**********` in logs, reprs and validation errors; reading the real
value requires an explicit `.get_secret_value()`. Unknown variables and invalid values fail at
startup rather than at the first request. See [`.env.example`](.env.example); `.env` is git-ignored.

### Correlation ids

Every response carries `X-Request-ID`. An inbound value is trusted only if it matches
`[A-Za-z0-9_-]{1,128}`; anything else — oversized, whitespace, newline, control characters — is
replaced with a generated id rather than rejected, so a header can never become a log-injection
payload. The id is bound for the request and appears on every application log line, including the
per-request line carrying method, path, status and duration. Bodies are never logged.

Logs are line-delimited JSON with a stable field set: `timestamp`, `level`, `event`, `logger`,
`service`, `environment`, `correlation_id`, plus any `extra` nested under `context` so application
data cannot overwrite a stable field.

**Current state:** the local stack, health endpoints, typed configuration and structured logging
exist and are verified. **There is still no business functionality** — no settlement ingestion, no
reconciliation, no financial calculation, no treatment proposals, no ledger adapter, no idempotency
or outbox, no audit events. See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for exactly what is and is
not built, and [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for what each later increment adds.

## Documents

| File | Purpose |
|---|---|
| [`PROJECT_SPEC.md`](PROJECT_SPEC.md) | Implementation-grade specification and acceptance criteria |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | Ordered milestones, tests, exit criteria, commit boundaries |
| [`DECISIONS.md`](DECISIONS.md) | ADR-style decision log, including what is still open |
| [`PROJECT_STATUS.md`](PROJECT_STATUS.md) | Current milestone, verified results, next tasks |
| [`CLAUDE.md`](CLAUDE.md) | Engineering rules that bind work in this repository |

## Licence

MIT — see [`LICENSE`](LICENSE).
