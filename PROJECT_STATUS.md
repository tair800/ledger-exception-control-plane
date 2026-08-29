# PROJECT_STATUS — ledger-exception-control-plane

Resume point for every session. Read this after `CLAUDE.md`, then check `git status` and recent
commits before doing anything.

**Current milestone:** M1.1 complete. **Next:** M1.2 — exception, resolution and reliability schema.
**No business functionality is implemented.** The schema has tables; nothing writes to them.

---

## Milestone progress

| Increment | Status | Notes |
|---|---|---|
| **0.1 Repository skeleton and tooling** | **DONE** | uv, ruff, mypy strict, pytest + coverage, CI |
| **0.2 Local stack and health endpoints** | **DONE** | Compose stack, liveness/readiness, typed config, structured JSON logging + correlation ids |
| **1.1 Core reconciliation schema** | **DONE** | SQLAlchemy 2.x + Alembic; `settlement_batch`, `settlement_line`, `ledger_entry`, `match_result` |
| 1.2 Exception, resolution and reliability schema | NOT STARTED | exception, evidence, proposal, approval, adjustment, outbox, posting_attempt, dlq, recovery_queue, audit_event |
| 1.3 – 12.1 | NOT STARTED | See `IMPLEMENTATION_PLAN.md` (31 increments total) |

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
  `match_result`. Exactly the M1.1 scope; no M1.2 table exists.
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

## Next tasks (M1.2 — do not start without instruction)

1. Migrations for `exception`, `evidence`, `treatment_proposal`, `approval`, `adjustment`
   (unique `operation_id`), `outbox`, `posting_attempt`, `dlq`, `recovery_queue`, `audit_event`
2. Unique-constraint test on `operation_id`
3. A test asserting the application role cannot `UPDATE` or `DELETE` `audit_event`

## Deployment state

Not deployed. Deployment is increment 10.1 (Fly.io + Neon). No cloud resources exist.

## Last verification results

```
80 passed, 16 deselected           coverage 98.14% (required 90%)
ruff format: all files formatted
ruff check:  All checks passed!
mypy:        Success: no issues found in 18 source files
schema:      12 passed against real PostgreSQL (migrations, drift, constraints)
```

Python 3.12.13, Windows, uv 0.11.15, Docker 27.4.0 / Compose v2.31.0, recorded 2026-08-29.

## Open decisions carried from planning

`DECISIONS.md` holds 26 ADRs and 12 OPEN items. None blocks M1.2. Still relevant:

- **LICENSE copyright holder** is `tair800` (the configured Git identity). Replace with a legal name
  if that matters for a public repository.
- **Coverage threshold of 90%** was a judgement, not a specified requirement; revisit as real code
  lands.
- **OPEN-6** (evaluation threshold) and **OPEN-7** (measurement load profile) remain unanswerable
  until a baseline exists, exactly as recorded.
