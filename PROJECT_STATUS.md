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
- **Money** — `NUMERIC(20, 4)` mapped to `Decimal` everywhere, every amount paired with an explicit
  currency, and no floating point anywhere in the metadata.
- **One migration** — `cf6581793e0c`, reviewed by hand after autogenerate.
- **Tests** — 80 unit (Docker-free) plus 12 schema tests against real PostgreSQL.

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
