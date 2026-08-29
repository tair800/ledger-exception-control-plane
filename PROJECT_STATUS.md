# PROJECT_STATUS — ledger-exception-control-plane

Resume point for every session. Read this after `CLAUDE.md`, then check `git status` and recent
commits before doing anything.

**Current milestone:** M0.1 complete. **Next:** M0.2 — local stack and health endpoints.
**No business functionality is implemented.**

---

## Milestone progress

| Increment | Status | Notes |
|---|---|---|
| **0.1 Repository skeleton and tooling** | **DONE** | uv, ruff, mypy strict, pytest + coverage, CI |
| 0.2 Local stack and health endpoints | NOT STARTED | Docker Compose, FastAPI health, structured logging |
| 1.1 – 12.1 | NOT STARTED | See `IMPLEMENTATION_PLAN.md` (31 increments total) |

## Completed and verified in M0.1

Every item below was verified by running it, not by inspection:

- Clean dependency install from the lockfile on Python 3.12.13 — `uv sync`
- Formatting check — `uv run ruff format --check .` → 2 files already formatted
- Lint — `uv run ruff check .` → all checks passed
- Strict type check — `uv run mypy` → no issues in 2 source files
- Tests with coverage — `uv run pytest` → 3 passed, 100% coverage (gate: 90%)
- `git diff --check` → clean

## Known issues

None outstanding at this milestone.

One was found and fixed during M0.1: coverage emitted a `module-not-measured` warning on every
run because the measurement source was declared twice — once via `--cov` in `addopts` and again
via `source_pkgs`. Removing the duplicate and adding a `[tool.coverage.paths]` mapping between
`src/` and `site-packages/` cleared it. A warning nobody acts on trains people to ignore CI output,
so it was treated as a defect rather than noise.

## Blockers

None.

## Next tasks (M0.2 — do not start without instruction)

1. `docker-compose.yml` with PostgreSQL and Redis
2. FastAPI application exposing `/healthz` and `/readyz`
3. Structured JSON logging with correlation-id middleware
4. `Makefile` targets
5. Integration test against the composed stack

## Deployment state

Not deployed. Deployment is increment 10.1 (Fly.io + Neon). No cloud resources exist.

## Last test results

```
3 passed in 0.17s
coverage: 100.00% (required 90%)
ruff format: 2 files already formatted
ruff check: All checks passed!
mypy: Success: no issues found in 2 source files
```

Python 3.12.13, Windows, uv 0.11.15, recorded on 2026-08-29.

## Open decisions carried from planning

`DECISIONS.md` holds 12 OPEN items. None blocks M0.2. Two are newly relevant:

- **LICENSE copyright holder** is currently `tair800` (the configured Git identity). Replace with a
  legal name if that matters for a public repository.
- **OPEN-6** (evaluation threshold) and **OPEN-7** (measurement load profile) remain unanswerable
  until a baseline exists, exactly as recorded.
