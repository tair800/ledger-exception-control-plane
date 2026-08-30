# PROJECT_STATUS — ledger-exception-control-plane

Resume point for every session. Read this after `CLAUDE.md`, then check `git status` and recent
commits before doing anything.

**Current milestone:** M2.2 complete. **Next:** M2.3 — exception creation and classification.
**Residual work is identified but not yet described.** A settlement file is ingested, normalised and
either accepted or quarantined; its lines are then matched deterministically against ledger entries,
with tolerance. What does *not* exist: any classification of the lines that fail to match. Nothing
creates an exception, assigns a taxonomy class, assembles evidence or computes an adjustment.

---

## Milestone progress

| Increment | Status | Notes |
|---|---|---|
| **0.1 Repository skeleton and tooling** | **DONE** | uv, ruff, mypy strict, pytest + coverage, CI |
| **0.2 Local stack and health endpoints** | **DONE** | Compose stack, liveness/readiness, typed config, structured JSON logging + correlation ids |
| **1.1 Core reconciliation schema** | **DONE** | SQLAlchemy 2.x + Alembic; `settlement_batch`, `settlement_line`, `ledger_entry`, `match_result` |
| **1.2 Exception, resolution and reliability schema** | **DONE** | 11 tables, append-only audit trigger, least-privilege role script |
| **1.3 Seeded fixture generator** | **DONE** | 12 scenarios, byte-identical regeneration, loads into real PostgreSQL |
| **2.1 Normalisation and quarantine** | **DONE** | Parser, normaliser, batch-level quarantine with closed reason codes |
| **2.2 Matching engine with tolerance bands** | **DONE** | Two rules, per-currency bands, mutual-uniqueness ambiguity refusal |
| 2.3 – 12.1 | NOT STARTED | See `IMPLEMENTATION_PLAN.md` (31 increments total) |

## What M0.2 delivered

- **Local stack** — `docker-compose.yml` with PostgreSQL 16, Redis 7 and the application, each with a
  healthcheck, `depends_on: service_healthy`, and localhost-only port bindings on non-default host
  ports (`15432`, `16379`, `8000`).
- **Container** — `Dockerfile` on `python:3.12-slim`, dependencies installed from the lockfile, runs
  as a non-root user (uid 10001), no secrets baked in, with a `.dockerignore`.
- **Typed configuration** — `pydantic-settings`, `LECP_` prefix, `extra="forbid"`, DSNs held as
  `SecretStr`, invalid values rejected at startup.
- **Health** — `/healthz` (liveness, no external dependencies) and `/readyz` (readiness, concurrent
  bounded read-only probes, `503` when a dependency is down, no diagnostic detail in the response).
- **Structured logging** — line-delimited JSON with a stable field set, correlation id bound per
  request, inbound ids validated against `[A-Za-z0-9_-]{1,128}`.
- **Tests** — 51 unit tests (Docker-free, deterministic) plus 4 integration tests behind an
  `integration` marker.

## Completed and verified in M0.2

Every item was verified by running it. **Docker Desktop crashed during the first verification
attempt**; the engine was restored manually and the complete verification was rerun from scratch.

| Check | Result |
|---|---|
| Clean `uv sync --frozen` (venv wiped) | PASS |
| `ruff format --check` | PASS — 12 files |
| `ruff check` | PASS |
| `mypy` strict | PASS — 12 source files |
| `pytest` + coverage | PASS — 51 passed, 97.65% (gate 90%) |
| `git diff --check` | PASS |
| Container build from scratch (`--no-cache`) | PASS — `lecp-app:latest`, 430 MB |
| Compose stack start (`--wait`) | PASS — all three healthy |
| PostgreSQL healthy / Redis healthy | PASS |
| Liveness | PASS — HTTP 200 |
| Readiness (both deps up) | PASS — HTTP 200, both `healthy` |
| PostgreSQL stopped → readiness fails, liveness stays healthy | PASS — `/readyz` 503 (`postgres: timed_out`), `/healthz` 200 |
| PostgreSQL restored → readiness recovers | PASS — recovered in ~2 s |
| Redis stopped → readiness fails, liveness stays healthy | PASS — `/readyz` 503 (`redis: timed_out`), `/healthz` 200 |
| Redis restored → readiness recovers | PASS — recovered in ~2 s |
| Bounded probe timeout, measured on the real stack | PASS — see known issues |
| JSON logs inspected | PASS — stable fields present |
| Valid correlation id propagated | PASS |
| Generated correlation id when absent | PASS |
| Invalid / oversized correlation id replaced | PASS — oversized, whitespace and injection attempts all replaced |
| Secrets absent from logs and health responses | PASS — 0 occurrences across 132 log lines |
| Integration suite against the real stack | PASS — 4 passed |
| Stack cleanly stopped, unrelated containers untouched | PASS |

## Known issues and findings

### M1.1 correction — silent decimal rounding (2026-08-29)

**A defect shipped in `70a8888` and was corrected in a follow-up commit.** The schema used
`NUMERIC(20, 4)` and the original ADR-020 asserted that satisfied the project's no-silent-rounding
rule. Empirical testing disproved it: inserting `Decimal("1.23456")` stored `1.2346` with no error
and no warning, leaving the application unable to tell the value had changed.

Monetary columns are now unconstrained `NUMERIC` with a check that rejects over-precise or
over-large values. `trunc(v, 4) = v` is used rather than `scale(v) <= 4`, because `scale` is
representation-based and would reject `1.230000` — numerically identical to `1.2300`, and lossless
to store. See ADR-020 for the measurements.

Autogenerate detected only the new check constraints, **not** the required column type change.
Adding the checks without widening the columns would have left the typmod rounding values before
the checks ever ran — a correction that appears to succeed while changing nothing. The migration
was written by hand.

An adversarial review of the correction found three further issues, all fixed before commit:

1. **Read-back canonicalisation was lost with the typmod.** `NUMERIC(20,4)` normalised every read
   to four places; unconstrained `NUMERIC` preserves the writer's scale, so `1.23` and `1.230000`
   would return as different `Decimal` objects for one economic value — and ADR-004b hashes the
   amount into `operation_id`, so that would silently defeat idempotency. A `Money` type decorator
   now quantises on read; writes still pass through unquantised so the constraint can reject.
2. **`NaN` passed the scale rule.** `trunc('NaN',4) = 'NaN'` is true in PostgreSQL, so `NaN` was
   excluded only incidentally by the magnitude comparison. Now excluded explicitly. Note the usual
   `col = col` idiom does **not** detect it — PostgreSQL `numeric` `NaN` compares equal to itself,
   unlike IEEE floats — so `col <> 'NaN'` is used. Verified.
3. **The downgrade was broken.** `op.drop_constraint` applies the naming convention just as create
   does, so passing an already-expanded name doubled the prefix and the DROP failed against a
   constraint that did not exist. Caught by actually running the downgrade, not by reading it.

Two constraints per column, not one, so a rejection names its cause: asyncpg reports the
constraint name but not the column. The downgrade now refuses rather than silently rounding.

**Known, accepted limitation.** An unconstrained `NUMERIC` can store a value with a very large
trailing-zero scale (e.g. `1.23` followed by thousands of zeros), which renders far wider than it
stores. Storage and indexing are unaffected — PostgreSQL omits trailing zero groups — and the read
path canonicalises to four places, so this is bounded in practice. A `scale(col) <= 8` ceiling would
close it at the database; judged not worth the extra constraint surface for an obscure case.

### M0.2 findings

Three defects were found *by the real-stack verification* and fixed. All are recorded as ADRs.

1. **Sequential probes made readiness latency the sum of timeouts** (ADR-016). Measured 3.07 s with
   PostgreSQL down. Now concurrent, bounded by the slowest single probe. Re-measured ~2.4 s, with
   samples ranging 2.36–3.43 s on Docker Desktop for Windows — the design property is the claim, not
   a precise speedup.
2. **Uvicorn's access log carried `correlation_id: null`** (ADR-017), because it is written after the
   middleware unbinds the context variable. The application now emits its own request line.
3. **`configure_logging` cleared all root handlers** (ADR-018), removing pytest's `caplog` and making
   a secret-leak test pass against an empty record list. It now replaces only its own handler.

Outstanding, not blocking: `starlette.testclient` emits a deprecation warning recommending `httpx2`.
Left alone — changing HTTP client libraries is not an M0.2 or M1.1 concern.

**M1.1 note.** `pytest -m integration` now spans two different requirements: the M0.2 stack tests
need the full Compose stack (`make up`), while the M1.1 schema tests need PostgreSQL only. Running
the whole integration marker with just PostgreSQL up therefore fails the four stack tests. Use
`make schema-verify` for schema work and `make up && make smoke` for the full set. Splitting the
marker was judged scope creep for this increment.

## Blockers

None.

## What M1.1 delivered

- **Persistence stack** — SQLAlchemy 2.x declarative models with async engine construction, and
  Alembic as the production migration path. `create_all()` is not used for schema management.
- **Four tables** — `settlement_batch` (unique `content_hash`), `settlement_line`, `ledger_entry`,
  `match_result`. Exactly the M1.1 scope — the M1.2 tables were added in the next increment, not
  pre-built in this one.
- **Money** — unconstrained `NUMERIC` mapped to `Decimal`, guarded by a check that **rejects**
  over-precise values rather than rounding them; every amount paired with an explicit currency; no
  floating point anywhere in the metadata.
- **Two migrations** — `cf6581793e0c` (schema) and `a72a14e6f51f` (precision correction), both
  reviewed by hand after autogenerate.
- **Tests** — 87 unit (Docker-free) plus 28 schema tests against real PostgreSQL.

## M1.1 verification

| Check | Result |
|---|---|
| Clean `uv sync --frozen` | PASS |
| `ruff format --check` / `ruff check` | PASS |
| `mypy` strict | PASS — 18 source files |
| Unit suite + coverage | PASS — 80 passed, 98.14% (gate 90%) |
| Alembic upgrade zero → head on a clean database | PASS — `cf6581793e0c` |
| Exactly the four M1.1 tables created | PASS — plus `alembic_version`, nothing else |
| No M1.2 tables present | PASS |
| Downgrade → re-upgrade | PASS |
| Model/migration drift (`alembic check`) | PASS |
| Constraints reject bad rows | PASS — duplicate hash, bad hash format, quarantine without reason, lower-case currency, double-consumed ledger entry, unpaired tolerance currency |
| **Over-precise money rejected, not rounded** | PASS — `1.23456`, `-1.23456`, `0.00005` all raise `ck_*_precision` |
| **Valid money persists exactly** | PASS — `1.2345`, `-1.2345`, `100`, `0`, `1.2300`, `1.230000`, both magnitude boundaries |
| **No monetary column carries a fixed-scale typmod** | PASS — regression guard against reintroducing silent rounding |
| `Decimal("0.1234")` round-trips exactly | PASS |
| `created_at` populated by the database, timezone-aware | PASS |
| `git diff --check` | PASS |

## What M1.2 delivered

- **Eleven tables** — the ten `IMPLEMENTATION_PLAN.md` §1.2 assigns, plus
  `treatment_proposal_evidence`, which realises the `evidence_refs` field `PROJECT_SPEC.md` §6.1
  requires on a proposal. A `UUID[]` column would have kept the count at ten but cannot be
  referentially checked (ADR-025).
- **Model containment, enforced in the schema** — `treatment_proposal` has no numeric column of any
  kind and no amount-like column name; both are asserted by tests, and the numeric guard was
  falsified by injecting `Integer`, `Float`, `SmallInteger` and `Money` columns and confirming each
  is detected.
- **Ambiguity is representable but not settleable** — `unknown`, `throttled` and `partially_applied`
  can be stored; none can appear on a `settled` outbox row (ADR-027).
- **Write-ahead attempt record** — `posting_attempt`, unique on `(adjustment_id, attempt_no)`, with
  `state`/`outcome`/`resolved_at` constrained so they cannot drift apart.
- **Authorisation is referential** — `adjustment` references
  `(approval.id, approved_treatment, principal)`, so an adjustment cannot be authorised by a
  rejection, cannot claim a treatment the approval did not give, and cannot be created at all for an
  `escalate` treatment (ADR-030).
- **Segregation of duties in the database** — the approving principal reaches the recovery item
  through composite foreign keys from `approval` via `adjustment`, so the check constraint compares
  against a value the database verified rather than one the writer supplied (ADR-028, as corrected).
- **Append-only audit trail** — a trigger refusing `UPDATE`, `DELETE` and `TRUNCATE` from any role
  including the owner, plus an insert-only grant to a least-privilege `lecp_app` role provisioned by
  `scripts/sql/provision_app_role.sql` (ADR-026).
- **One migration**, `133ac0053abd`, autogenerated and then corrected by hand.
- **Tests** — 111 unit (Docker-free) plus 76 schema tests against real PostgreSQL.

## M1.2 verification

| Check | Result |
|---|---|
| `ruff format --check` / `ruff check` | PASS |
| `mypy` strict | PASS — 19 source files |
| Unit suite + coverage | PASS — 111 passed, 97.88% (gate 90%) |
| Alembic upgrade base → head on a clean database | PASS — 3 migrations |
| Exactly the 15 expected tables created | PASS — plus `alembic_version`, nothing else |
| No later-increment tables present | PASS |
| Downgrade → base leaves no residue | PASS — trigger function dropped too, not only the tables |
| Downgrade → re-upgrade | PASS |
| Model/migration drift (`alembic check`) | PASS |
| Schema suite against real PostgreSQL | PASS — 76 passed |
| Migration-built schema identical to model-built schema | PASS — 132 columns, 104 constraints, 41 indexes, 0 differences |
| Adjustment authorised by a rejection | REJECTED — foreign key, for all three treatments |
| Adjustment claiming an unauthorised treatment | REJECTED |
| Adjustment naming a principal who did not approve it | REJECTED |
| `escalate` treatment posted | REJECTED — and an approved treatment is still permitted |
| Recovery item inventing its approving principal | REJECTED |
| Attempt record carrying a foreign `operation_id` | REJECTED |
| Duplicate `operation_id` rejected | PASS |
| `UNKNOWN` storable while pending | PASS |
| `UNKNOWN`/`throttled`/`partially_applied`/NULL cannot be settled | PASS |
| `confirmed`/`rejected` can be settled | PASS — the complement, so the rule is not simply "never" |
| Attempt state and outcome cannot drift apart | PASS — 3 incoherent combinations rejected |
| Approver cannot resolve their own `UNKNOWN` | PASS — and a different principal can |
| One open recovery item per adjustment; reopen after close allowed | PASS |
| Monetary key in a DLQ envelope rejected | PASS — `amount`, `total`, `fee`, `rate` |
| `adjustment.amount` over-precision rejected, not rounded | PASS |
| `audit_event` `UPDATE`/`DELETE`/`TRUNCATE` refused for the table owner | PASS |
| Application role cannot `UPDATE`/`DELETE` `audit_event`, but can `INSERT` | PASS |
| Role provisioning script is idempotent | PASS — applied twice |
| Attribution / secret scan of the diff | PASS |
| `git diff --check` | PASS |

## Known issues and findings

### M1.2 — adversarial review: two controls rested on unverified values (2026-08-29)

The increment was reviewed adversarially before commit: five independent lenses over the new schema,
each finding verified by a separate skeptic prompted to refute it. **42 findings were raised; 3
survived verification**, and two of those were the same defect found independently by two lenses.

1. **An adjustment could be authorised by a rejection.** `adjustment.approval_id → approval.id`
   proves an approval row exists; it does not prove the approval said yes. A `decision='rejected'`
   approval could back a fully formed, dispatchable adjustment carrying an `operation_id`. FR-7 is
   one of the project's headline claims, and the audit trail would have answered "who approved this?"
   by pointing at someone who declined. Closed by referencing
   `(approval.id, approved_treatment, principal)` — a rejection has `approved_treatment IS NULL` and
   the referencing column is `NOT NULL`, so the row is unreachable (ADR-030).
2. **The segregation-of-duties check compared against a copied value.** ADR-028 argued that a control
   depending on application discipline is not a control — and then had the application supply the
   value being compared against. The discipline had moved from the comparison to the copy, not
   disappeared. Closed by a composite-foreign-key chain from `approval` through `adjustment` to
   `recovery_queue` (ADR-028, as corrected).

The same idiom was applied to `posting_attempt.operation_id`, which was likewise duplicated but
unverified. That one was raised by a verifier rather than as a finding in its own right; it is the
same defect class and recovery reads that row to decide whether an irreversible write may be
repeated.

The remaining 39 findings were refuted on verification — most commonly as out of scope for a
structure-only increment, or as behaviour a later increment owns.

**Method note.** All three surviving findings are of one kind: a value the schema *carried* but did
not *check*. Reading the models does not surface that class, because each individual column and
constraint is correct in isolation. It shows up when someone asks what an attacker or a careless
second code path could write.

### M1.2 — two three-valued-logic holes in check constraints (2026-08-29)

Both were written, both read correctly, both passed review by reading, and both were caught only by
a test that inserted the row.

```sql
-- as written: NULL IN (...) is NULL, and PostgreSQL treats a NULL check result as SATISFIED
state <> 'settled' OR last_outcome IN ('confirmed', 'rejected')
```

An outbox row could therefore be marked `settled` carrying **no outcome at all** — a dispatch
recorded as finished with no record of what the ledger said. That is the same defect as filing an
`UNKNOWN` as done, reached by a different route. `posting_attempt.posting_ref` had the identical
hole: with the outcome still NULL, an `in_flight` attempt could carry a posting reference for a
response that had not arrived. Both now spell out `IS NOT NULL` explicitly, and both have tests.

### M1.2 — a test that passed for the wrong reason

`test_the_application_role_cannot_update_or_delete_audit_event` passed on the first run against a
database where the role held **no privileges at all**, because an earlier test in the same module
drops and recreates every table — which discards every grant. A denial proves nothing if the role
was never granted anything. The test now asserts the role actually holds `INSERT` before asserting
the denial, and the migration/downgrade test re-runs provisioning, mirroring the real release order
(migrate, then provision).

### M1.2 — the default DSN pointed at the developer's own PostgreSQL

Found while running migrations. `Settings.postgres_dsn` defaulted to `localhost:5432`, so an
unconfigured `alembic upgrade head` targeted whatever PostgreSQL happened to be on the default port.
It failed on authentication — luck, not design. Defaults are now the stack's published ports
(ADR-029), and `.env.example`, which still claimed no variables were needed, lists them.

### Carried forward, not blocking

`README.md` line 181 contains the banned phrase in quotation marks while explaining that it is
banned. `IMPLEMENTATION_PLAN.md` §11.1 specifies a test asserting the phrase appears nowhere in
`README.md`, and excludes only `CLAUDE.md`, `PROJECT_SPEC.md`, `DECISIONS.md` and the plan. Either
the sentence is rephrased or the allowlist grows; that is an 11.1 decision and is recorded here so
it is not discovered as a surprise then.

## What M1.3 delivered

- **A deterministic generator** in `src/ledger_exception_control_plane/fixtures/`, producing a corpus
  from a seed alone. Draws are `SHA-256(domain ‖ seed ‖ label)` rather than a random stream, so a value
  depends on nothing but its own label and adding a scenario cannot perturb the others (ADR-032).
- **Twelve scenarios** covering every class of FR-4's taxonomy plus the awkward cases the plan names —
  missing references, ambiguous memos, cross-period refunds — each recording what it represents, which
  milestone needs it, and what distinguishes it from its neighbours.
- **The settlement file format**, resolving OPEN-1: a synthesised composite CSV, one movement per row,
  with FX rates as recorded strings rather than computed values (ADR-031).
- **A committed canonical corpus** (`fixtures/canonical/`, 11 files) pinned by a drift test in both the
  unit suite and CI.
- **Four deliberately malformed files** for M2.1's quarantine path, labelled and provably outside the
  loadable set.
- **A loader** that refuses any database not named `lecp_test`/`lecp_demo`/`lecp_fixtures`, resets by
  identifier rather than `TRUNCATE`, and disables no constraint (ADR-035).
- **Tests** — 200 unit (Docker-free) plus 8 fixture-load tests against real PostgreSQL.

## M1.3 verification

| Check | Result |
|---|---|
| `ruff format --check` / `ruff check` | PASS |
| `mypy` strict | PASS — 31 source files |
| Unit suite + coverage | PASS — 200 passed, 94.48% (gate 90%) |
| Same seed regenerates byte-identically | PASS |
| Committed corpus matches the generator | PASS — asserted in the suite and as a CI step |
| Drift check fails on a tampered artifact | PASS — verified by tampering; exits 1 and names the paths |
| A different seed changes references and amounts | PASS |
| A different seed does **not** change scenario structure, dates or the mix | PASS |
| Every identifier is UUIDv5, unique, name-derived | PASS |
| No clock or random source read anywhere in the package | PASS — AST walk, verified by injecting `datetime.now()` |
| No forbidden import | PASS — allowlist, verified by injecting `import random` |
| No function named after an M2 action | PASS — verified by injecting `match_lines` |
| Every amount is an exact `Decimal` at its currency's real minor unit | PASS |
| Amounts serialise as JSON strings, never numbers | PASS |
| Generated records fit the live column limits | PASS — limits read from the metadata, so they cannot drift |
| Every metadata reference resolves to real data | PASS |
| Declared distribution pinned as literals; catalogue must match | PASS — a weight change fails 13 tests |
| Allocation matches hand-computed literals | PASS — sizes 12, 100, 200, 400 |
| Allocation matches an independent `Fraction`-based reimplementation | PASS — 12, 13, 17, 47, 100, 199, 200, 201, 400, 1000, 4321 |
| A clean size reproduces the declared percentages exactly | PASS — 200, 400, 1000, 4000 |
| Deviation from ideal is under one instance once the floor stops binding | PASS — measured worst case 0.76 over N = 200…3000 |
| Below the threshold, the floor's cost is confined to the donor | PASS — 12, 13, 17, 47, 100, 199 |
| Generated residual mix matches the declared apportionment end to end | PASS — 12, 47, 200, 401 |
| Exactly N instances produced, every scenario present | PASS — swept N = 12…3000, 2,989 sizes, zero violations |
| Invalid artifacts labelled and outside the loadable set | PASS |
| No credential-shaped material in any artifact | PASS |
| Manifest carries no path, timestamp or machine identity | PASS |
| Corpus loads into real PostgreSQL with all constraints on | PASS — 2 batches, 17 lines, 10 ledger entries |
| Amounts read back exactly; `match_state` still `unmatched` | PASS |
| Stored `raw_payload` hashes to its `content_hash` | PASS |
| Loading twice without reset is refused by the database | PASS |
| Reset leaves an unrelated row untouched | PASS |
| Loader refuses a non-disposable target | PASS |
| `git diff --check` | PASS |

## Known issues and findings

### M1.3 phase close — two integrity corrections (2026-08-30)

**An undeclared direct dependency.** `pydantic` is imported by name in `config.py`, `api.py` and the
fixture artifact schema, but was only ever present transitively through `fastapi` and
`pydantic-settings`. It resolved and therefore worked, which is exactly why it went unnoticed for
three milestones: an undeclared direct dependency breaks only when an intermediary drops or bounds it,
and that break lands far from its cause. Now declared at `==2.13.5`, the version already locked — the
lockfile gained two lines (the dependency edge and its specifier) and no package was added, removed or
upgraded. An AST audit of every third-party import in `src/` found no other case.

**The declared mix was under-tested and over-claimed.** The plan requires the residual mix to match the
declared distribution; the tests asserted the total, the coverage and the ordering, and this document
described that as verifying the mix. The distribution is now a stated apportionment rule (ADR-037) with
tests that compare the exact integer allocation against hand-computed literals and against an
independent `Fraction`-based reimplementation. One of the hand literals was wrong when first written
and the reimplementation caught it, which is the argument for having both.

**Falsified, not assumed.** Reversing the remainder tie-break fails 5 tests; making the coverage floor
add units instead of moving them fails 7; changing a single catalogue weight fails 13. A sweep over
N = 12…3000 found zero violations of the contract and a worst-case deviation of 0.76 instances against
a bound of 1. The committed corpus is byte-unchanged by any of this.

### M1.3 — adversarial review: a fixture writer that could delete a repository (2026-08-29)

Five independent lenses over the fixture system, each finding verified by a separate skeptic prompted
to refute it. **30 findings raised; 7 survived**, and one of them was serious.

1. **`write_corpus` was a deletion tool with no guard.** It unlinks every file under its target that
   is not one of the artifacts it is about to write, and `rglob` matches dotted entries. `--out` is a
   free-form path, so `generate --out .` from the repository root would have unlinked the source
   tree, the tests, the migrations and `.git` with them. The inverse of this project's own stated
   principle: ADR-035 guards the *loader*, whose writes are additive and reversible, while the
   destructive writer had nothing. It now refuses any directory that is non-empty and contains no
   manifest — verified by pointing it at a fake working tree and confirming nothing was touched.
2. **The disposability check ran after the destructive step.** The integration fixtures run
   `alembic downgrade base`, dropping all fifteen tables, taking their DSN from the environment
   unvalidated; ADR-035's allowlist was only reached later, inside `load()`. A guard that fires after
   the damage is not a guard. Both the fixture suite and the pre-existing schema suite now check
   first.
3. **The scope guards had a hole shaped like the thing they guard.** `fixtures_module_paths()` used
   `glob` rather than `rglob`, so a `fixtures/matching/` subpackage would have been invisible to all
   three AST guards while the docstring claimed the opposite.
4. **The FX scenario's own numbers disagreed by two orders of magnitude.** Presentment amount, rate
   and settled amount were drawn independently, so the corpus asserted that JPY 683,880 at 0.00658
   settled as EUR 40.77. An FX *rounding* fixture whose arithmetic is wrong by 100× is not a rounding
   fixture. The settled amount is now constructed from the recorded rate with an explicit, audited
   quantisation, and the ledger differs by the one to three minor units that make it a rounding case.
5. **A refusal message could echo credential material.** `urlsplit` ends the netloc at the first `/`,
   so a password containing an unencoded slash pushes the rest of the credential into what looks like
   a database name — and straight into the error message. §17 treats an error message as a log line
   waiting to happen. The name is now printed only if it is shaped like a database name.
6. **This document overstated a test.** The verification table claimed the declared mix was exact at
   seven different sizes; only the count and the coverage were checked at six of them, and the mix
   only at the default. Corrected above, and the allocator now has tests for the properties that
   actually hold at every size — plus strict proportionality at the sizes where it can hold.

The remaining 23 findings were refuted on verification.

**Method note.** The dangerous one was not a subtle bug. It was a `for … unlink()` loop that reads
exactly as intended and is fine every time the tool is used correctly. Reading the code does not
surface that class; asking "what does this do if someone types the wrong path" does.

### M1.3 — `--instances` was a suggestion, not a count (2026-08-29)

The bulk profile allocates the declared weights proportionally, which zeroes the rare scenarios at small
sizes; a floor raised each to one. The floor *added* the units, so asking for 24 instances produced 30 —
and quietly distorted the declared mix at the same time. Caught by a test that asserted the requested
size and got a larger one. The floor now takes its unit from the largest bucket, so coverage is
guaranteed without changing the size the caller asked for.

### M1.3 — an interpretation worth flagging

`IMPLEMENTATION_PLAN.md` §1.3 names only "settlement batches" among the deliverables, but its goal is a
*controlled residual mix*, and a residual is defined relative to a ledger. The corpus therefore also
generates a `ledger_entry` snapshot, without which the residual mix would be neither constructible nor
checkable and the "committed sample loads" criterion would be satisfied by inserting a single row. This
is an under-specification in the plan's wording resolved by its own stated goal, not a contradiction
between documents — recorded here so the decision is visible rather than assumed.

## What M2.1 delivered

- **The ingestion boundary** in `src/ledger_exception_control_plane/ingest/`: parser, normaliser,
  quarantine vocabulary and persistence orchestration. Callable without HTTP; no endpoint and no CLI
  were added, because §2.1 asks for neither.
- **Raw before parse (FR-1)** — the receipt commits in its own transaction before anything reads the
  payload, so a malformed file leaves behind the bytes it was rejected for (ADR-041).
- **Batch-level quarantine (FR-2)** — one bad row condemns the file. A trusted partial settlement
  file would turn every dropped movement into an unexplained residual downstream (ADR-040).
- **A closed reason vocabulary** — 15 codes plus a line and a column, bounded, allowlisted, and
  carrying neither the offending value nor an exception message (ADR-038).
- **Money from text, never through a float** — `Decimal` straight from the string, value-based
  precision matching the column's `trunc(amount, 4) = amount`, over-precision rejected rather than
  quantised. `float` appears nowhere in the package, enforced by an AST guard — the plan's exit
  criterion for this increment.
- **References preserved exactly** — no case folding, no punctuation stripping, no whitespace
  collapsing. Canonicalisation is a matching decision and M2.2 owns it (ADR-039).
- **Re-delivery is a no-op the database arbitrates** — `ON CONFLICT DO NOTHING` on the unique
  content hash, never check-then-insert, with the batch claimed under `SELECT … FOR UPDATE`.
- **Tests** — 114 unit (Docker-free) plus 21 ingestion tests against real PostgreSQL.

**No schema change was required.** The M1 tables express the whole contract; the columns the file
declares but `settlement_line` does not hold — transaction type, presentment amount and currency, FX
rate, memo — are carried in the typed representation and remain in the immutable raw payload for the
increments that need them. **No dependency was added**; the standard library parses the format.

## M2.1 verification

| Check | Result |
|---|---|
| `ruff format --check` / `ruff check` / `mypy` strict | PASS — 38 source files |
| Unit suite + coverage | PASS — 350 passed, 93%+ (gate 90%) |
| Canonical settlement files normalise to the recorded values | PASS — both periods, amounts compared as strings so scale is proven too |
| Money never passes through a float | PASS — AST guard, verified by injection |
| Over-precise amount rejected, never rounded | PASS — and trailing zeros beyond four places accepted, matching ADR-020 |
| Locale-dependent numbers and dates refused | PASS — comma decimals, Arabic-Indic digits, `03/06/2026` |
| Normalisation is deterministic across runs | PASS |
| Content hash taken from the original bytes | PASS — a BOM makes it a different artifact |
| Receipt survives a parse failure | PASS — asserted against stored rows |
| Malformed committed fixtures reach the right quarantine code | PASS — all four |
| One bad row leaves no line from the batch | PASS — asserted against `settlement_line` |
| Quarantine reason bounded, allowlisted, free of input and internals | PASS |
| Exact re-delivery creates no second batch or line | PASS |
| Two concurrent deliveries produce exactly one batch | PASS — real concurrency, separate engines |
| Unique index still refuses a hand-written duplicate | PASS |
| Interrupted attempt is completed rather than restarted | PASS |
| Integrity failure during line persistence leaves no partial state | PASS — batch stays `received` |
| No constraint disabled | PASS — `session_replication_role` still `origin` |
| No row written to any later increment's table | PASS |
| Existing schema and fixture suites | PASS — 76 + 8 |
| `alembic check` | PASS — no schema change |
| `git diff --check`, secret scan, attribution scan | PASS |

## Known issues and findings

### M2.1 — adversarial review: a single byte could jam a batch permanently (2026-08-30)

Five lenses, each finding verified by a separate skeptic. **23 findings raised; 8 survived**, and four
of the eight were the same defect found independently by four different lenses.

1. **A NUL in a reference was a poison pill.** U+0000 is valid UTF-8, survives `decode`, survives
   `csv.reader`, is not whitespace and is under the length limit — so it normalised cleanly and then
   failed the INSERT, because PostgreSQL cannot store it in a character type. That alone would be an
   unhandled error instead of a quarantine. What made it serious is that the receipt is already
   committed and the payload is immutable, so **every re-delivery reproduced it** and the batch could
   never reach `parsed` or `quarantined`. Text fields are now checked for characters the destination
   column cannot hold.
2. **The precision rule contradicted the ADR it cited.** The check counted digits after the decimal
   point; ADR-020 explicitly chose a *value-based* rule and records `1.230000` as accepted. So
   `120.450000` — which the column stores exactly — quarantined an entire batch, and the stated
   rationale was falsified by the code's own silent handling of leading zeros. Now value-based, and a
   test asserts ingestion never accepts something the column would refuse.
3. **A `ParseResult` docstring stated an invariant the code violates in both directions.** It claimed
   "either rows or defects, never both, never neither"; a file with one good row and one short row
   produces both, and a header-only file produces neither. A caller trusting it would have consumed
   rows from a condemned file.
4. **No test asserted the *content* of a quarantine reason** — only its length, ordering and
   character set. A reason could have degraded to something well-formed and useless.

The remaining 15 findings were refuted on verification.

### M2.1 — a concurrency race the single-threaded tests could not see

Two simultaneous deliveries of one payload both observed the receipt at `received` and both proceeded
to interpret it. The unique constraint on `(settlement_batch_id, line_number)` caught the second
write — the guard working exactly as designed — but the loser got an integrity error where FR-1 says
a re-delivery is a no-op. Found by running two ingests concurrently on separate engines; every
sequential test passed. Closed with `SELECT … FOR UPDATE` (ADR-041).

### M2.1 — a guard that failed its own guard

The quarantine reason has a length cap and a character allowlist. The truncation branch appended
`"..."`, and the allowlist contains no full stop — so a reason long enough to be truncated would have
raised instead of being stored, leaving the batch stuck at `received`. Unreachable with today's codes,
and found by a test that exercised the branch rather than reasoning about it.

## What M2.2 delivered

- **A deterministic matcher** in `src/ledger_exception_control_plane/matching/`: the policy, a pure
  decision function, and the persistence that writes a match and the line state together.
- **OPEN-2 resolved** (ADR-042). Absolute per-currency bands, one minor unit each, inclusive; a
  one-day value-date window as a hard eligibility filter; no tolerance ever across currencies; a
  currency with no declared band gets exact matching only.
- **Two rules, explicit precedence** — `exact_amount` then `amount_within_tolerance`, recorded in
  `match_result.rule_id`, with the absorbed difference in `tolerance_applied`.
- **Ambiguity refused, not resolved** (ADR-043). A pair is accepted only when it is the unique choice
  from *both* sides, which is what makes the result independent of the order rows arrive in.
- **Concurrency arbitrated by the constraints** — `ON CONFLICT DO NOTHING` against the two unique
  indexes; a worker that loses a race writes nothing and leaves the line cleanly retryable.
- **Tests** — 48 unit (Docker-free) plus 23 matching tests against real PostgreSQL.

**No schema change and no new index.** `match_result` already carries the rule, the absorbed
tolerance and its currency. The access path is `settlement_line` filtered by `match_state` (served by
`ix_settlement_line_batch_match_state` when a batch is given) and `ledger_entry` filtered by currency
with a `NOT EXISTS` against `match_result`; at this corpus's scale no index is justified by evidence,
and adding one on a guess is the kind of premature optimisation this project has avoided elsewhere.
Recorded here so a later increment with real volume knows where to start. **No dependency added.**

### Measured clearance

| Corpus | Lines | Matched | By tolerance | Ambiguous | Cleared |
|---|---|---|---|---|---|
| `bulk` @ 200 instances | 215 | 176 | 7 | 0 | **81.9%** |
| `canonical` | 17 | 4 | 0 | 0 | 23.5% |

The exit criterion is measured on the **bulk** profile. The canonical corpus holds exactly one
instance of every condition — three of its twelve scenarios are matched-intent — so its rate reports
the shape of the catalogue, not the matcher's reach. Every one of those matches is deterministic,
with no model call anywhere in the path.

## M2.2 verification

| Check | Result |
|---|---|
| `ruff format --check` / `ruff check` / `mypy` strict | PASS — 44 source files |
| Unit suite + coverage | PASS — 409 passed, 92.19% (gate 90%) |
| Exact rule, tolerance rule, and their precedence | PASS |
| Band boundary: strictly inside, exactly on, one step past | PASS — all five declared currencies |
| Boundary table self-check (the differences are what they claim) | PASS |
| Currency mismatch never matches, however close | PASS |
| Date window: inside, on the edge, outside; window configurable | PASS |
| Signed amounts match only their own sign; zero not special-cased | PASS |
| Unknown currency gets no tolerance (fail-closed) | PASS |
| Two exact candidates → unmatched, not guessed | PASS |
| Two tolerance candidates → unmatched | PASS |
| Two lines competing for one entry → neither matches | PASS |
| Ambiguity is never rescued by falling to a lower-priority rule | PASS |
| Result independent of input order | PASS — every permutation of an adversarial set |
| Result independent of row insertion order | PASS — against real PostgreSQL |
| Result independent of the database's `TimeZone` setting | PASS — verified under `Europe/Berlin` |
| Match result and line state persist atomically | PASS |
| Consumed ledger entry unavailable; matched line not reconsidered | PASS |
| Repeated runs write nothing further and rewrite no `matched_at` | PASS |
| Two workers cannot double-match one line | PASS — real concurrency, separate engines |
| Two workers cannot double-consume one ledger entry | PASS |
| A lost race leaves the line unmatched and retryable | PASS |
| Direct SQL cannot duplicate a match or reuse an entry | PASS |
| No row written to any later increment's table | PASS |
| No `float` anywhere in the matching package | PASS — AST guard |
| Fixture ground truth unreachable from production matching code | PASS — AST guard |
| Existing ingestion, fixture and schema suites | PASS — 21 + 8 + 76 |
| All four PostgreSQL suites in one session | PASS — 128 passed in 10:32 |
| `alembic check` | PASS — no schema change |
| `git diff --check`, secret scan, attribution scan | PASS |

## Known issues and findings

### M2.2 — adversarial review: a hard filter that depended on a server setting (2026-08-30)

Five lenses, each finding verified by a separate skeptic. **29 findings raised; 7 survived**, covering
three distinct defects.

1. **The date filter depended on the database's `TimeZone`.** `booked_at` is `TIMESTAMPTZ`, and a
   plain `::date` cast is resolved by PostgreSQL in the *session's* zone — which nothing in this
   project pins. The same rows would therefore reconcile differently on two servers, and the Python
   side computed the UTC date while production computed whatever the server was set to. Because a
   consumed ledger entry can never be released (ADR-024), a divergence would be permanent and
   unrecoverable. The cast is now pinned to UTC explicitly, and a test runs the matcher against a
   database set to `Europe/Berlin` with a candidate at 23:30 UTC — the instant that falls on the next
   day there — and asserts the outcome is unchanged.
2. **The "below the band" test leg was a zero difference.** All five rows of the boundary
   parametrisation put the base amount in the "below" column, so that leg exercised the *exact* rule
   while claiming to test the band. No test anywhere exercised a non-zero difference strictly inside
   a band. The table now uses the four-decimal storage headroom to produce a real sub-unit
   difference, asserts the match came from the tolerance rule, and a second test checks the table's
   own arithmetic — a test table is code, and this one was wrong.
3. **A docstring claimed an idempotence the code does not have.** `run_matching` said a second run
   "considers no lines and returns a run of zeroes". It is idempotent in what it *writes*, but
   residual lines stay unmatched by design and are reconsidered every run: on the canonical corpus a
   second pass returns `considered=13, matched=0`. Repeated matching is safe, not free.

A fourth finding was addressed by narrowing a claim rather than changing code: the double-consume
concurrency test asserted only that one row survived, which would hold even if the two workers
serialised. It now also asserts that exactly one worker claimed the entry, and states explicitly that
it does not require the interleaving to occur — the deterministic loser-side behaviour is proven by a
separate sequential test rather than by hoping for a race.

### M2.2 — a flaky integration suite that was the harness, not the product

Running all four PostgreSQL suites in one pytest session began failing intermittently: a different
test each time, always an `asyncio` timeout rather than an assertion, with wall time inflated from
about five minutes to as much as ninety-four. Each suite passed alone, and CI runs them as four
separate processes, so nothing was ever wrong with the product.

The cause was connection churn in the test harness. The matching module's engine fixture is
function-scoped, and a pooled engine leaves up to five idle sockets behind per test; across four
modules those accumulated until asyncpg's sixty-second *connection establishment* timeout began
firing. Diagnosed by elimination rather than guessed at — PostgreSQL was idle at 0.02% CPU with 46 MB
resident, no catalog bloat (`pg_attribute` under 1 MB, dead tuples in single figures) and a database
of 9.5 MB, so the database was never the constraint. The fixture now uses `NullPool`, which holds no
idle connections. All four suites together: **128 passed in 10:32**, repeatably.

### M2.2 — and one the fix for the review introduced

The timezone-independence test was written using `ALTER DATABASE lecp_test SET TimeZone`. That is
persistent, applies to every other connection, and left the rest of the suite waiting on a lock — one
test timed out and the run took fourteen minutes instead of four. A test that reconfigures the server
to make its point is a worse problem than the one it is demonstrating. The zone is now set through
`connect_args={"server_settings": {"timezone": ...}}` on that test's own engine, and the test asserts
the shifted zone is actually in effect before drawing any conclusion from it.

### M2.2 — two defects the tests caught before the review

`MatchRun` exposed a `cleared_fraction: float` for reporting, and the package's own no-float guard
refused it. The guard was right: a ratio on a business result invites something downstream to branch
on it, so the property was removed rather than the guard weakened.

The read and the write also collided — SQLAlchemy's autobegin opened a transaction on the first
`SELECT`, and the explicit write transaction then failed with "a transaction is already begun". Every
path that actually persisted a match failed; the ones that matched nothing passed. Now the read holds
its own explicit transaction and the decision is computed with none held.

## Deployment state

Not deployed. Deployment is increment 10.1 (Fly.io + Neon). No cloud resources exist.

## Last verification results

```
409 passed, 131 deselected         coverage 92.19% (required 90%)
ruff format: all files formatted
ruff check:  All checks passed!
mypy:        Success: no issues found in 44 source files
schema:      76 passed against real PostgreSQL (migrations, drift, constraints, grants)
fixtures:     8 passed against real PostgreSQL (corpus loads with constraints enforced)
ingest:      21 passed against real PostgreSQL (receipt, quarantine, re-delivery, concurrency)
matching:    23 passed against real PostgreSQL (persistence, ambiguity, races, timezone)
```

Python 3.12.13, Windows, uv 0.11.15, Docker 27.4.0 / Compose v2.31.0, recorded 2026-08-29.

## Open decisions carried from planning

`DECISIONS.md` holds 45 ADRs and 10 OPEN items. OPEN-2 was resolved at M2.2 (ADR-042). None blocks M2.3; **OPEN-3** (the final form of the exception taxonomy) is the next one due. Still relevant:

- **LICENSE copyright holder** is `tair800` (the configured Git identity). Replace with a legal name
  if that matters for a public repository.
- **Coverage threshold of 90%** was a judgement, not a specified requirement; revisit as real code
  lands.
- **OPEN-6** (evaluation threshold) and **OPEN-7** (measurement load profile) remain unanswerable
  until a baseline exists, exactly as recorded.
