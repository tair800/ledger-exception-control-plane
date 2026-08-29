# PROJECT_STATUS — ledger-exception-control-plane

Resume point for every session. Read this after `CLAUDE.md`, then check `git status` and recent
commits before doing anything.

**Current milestone:** M0.2 complete. **Next:** M1.1 — core reconciliation schema.
**No business functionality is implemented.**

---

## Milestone progress

| Increment | Status | Notes |
|---|---|---|
| **0.1 Repository skeleton and tooling** | **DONE** | uv, ruff, mypy strict, pytest + coverage, CI |
| **0.2 Local stack and health endpoints** | **DONE** | Compose stack, liveness/readiness, typed config, structured JSON logging + correlation ids |
| 1.1 Core reconciliation schema | NOT STARTED | Alembic, settlement/ledger/match tables |
| 1.2 – 12.1 | NOT STARTED | See `IMPLEMENTATION_PLAN.md` (31 increments total) |

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
Left alone — changing HTTP client libraries is not an M0.2 concern.

## Blockers

None.

## Next tasks (M1.1 — do not start without instruction)

1. Alembic setup
2. Migrations for `settlement_batch` (unique `content_hash`), `settlement_line`, `ledger_entry`,
   `match_result`
3. Migration up/down tests and a unique-constraint test

## Deployment state

Not deployed. Deployment is increment 10.1 (Fly.io + Neon). No cloud resources exist.

## Last verification results

```
51 passed, 4 deselected            coverage 97.65% (required 90%)
ruff format: 12 files already formatted
ruff check:  All checks passed!
mypy:        Success: no issues found in 12 source files
integration: 4 passed against the live Compose stack
```

Python 3.12.13, Windows, uv 0.11.15, Docker 27.4.0 / Compose v2.31.0, recorded 2026-08-29.

## Open decisions carried from planning

`DECISIONS.md` holds 21 ADRs and 12 OPEN items. None blocks M1.1. Still relevant:

- **LICENSE copyright holder** is `tair800` (the configured Git identity). Replace with a legal name
  if that matters for a public repository.
- **Coverage threshold of 90%** was a judgement, not a specified requirement; revisit as real code
  lands.
- **OPEN-6** (evaluation threshold) and **OPEN-7** (measurement load profile) remain unanswerable
  until a baseline exists, exactly as recorded.
