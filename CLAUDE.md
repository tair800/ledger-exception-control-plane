# CLAUDE.md — ledger-exception-control-plane

Operating contract for this repository. Read before any work in this project.

**Parent portfolio rules remain authoritative.** `../../CLAUDE.md` and
`../../PORTFOLIO_MASTER_SPEC.md` govern; this file adds project-specific rules and never relaxes a
parent rule. Where they appear to conflict, the parent wins and the conflict is raised, not resolved
silently.

---

## What this project is

A finance-ops control plane that turns unmatched PSP settlement lines into approved ledger
adjustments, **dispatched at most once per operation identifier**, with an effectively-once ledger
*effect* only where the adapter capability table in `PROJECT_SPEC.md` §13.4 permits that claim.
Deterministic matching clears the bulk. The model proposes a *treatment* for the residual — never an
amount.

**Status: implementation in progress.** M0 (scaffold, stack), M1 (schema, fixture corpus) and all
of M2 (ingestion, deterministic matching, residual classification, and the deterministic adjustment
calculator) are complete; **increment 3.1 — the treatment-enum closure gate — has passed** (ADR-048);
**3.2 delivered the provider port and the closed proposal contract**, with OPEN-5 resolved to
Anthropic and OpenAI (ADR-049); **3.3 delivered deterministic evidence assembly, prompt construction
and the proposal flow**, recording proposals with their model id, version and prompt hash (ADR-050);
**3.4 delivered the cassette record/replay harness** — the whole corpus replays offline through
both adapters, from a committed file, with no credential (ADR-051); **4.1 delivered claim
locking and the retry-independent operation identifier**, the first increment of the reliability
phase (ADR-052); and **4.2 has delivered the transactional outbox and the capability-declaring
ledger adapter** — the intent written in the state change's own transaction, a write-ahead attempt
record before every send, and a port whose guarantees are declared as data, proven by a conformance
run and branched on rather than assumed (ADR-054). **M4.3 is next.**

The model layer still makes **no live call**: no provider SDK is a dependency, nothing under `llm/`
imports an HTTP client, and no transport that speaks HTTP exists — the flow is exercised entirely
through injected fakes and recorded cassettes. The committed cassettes are **synthesised, not
captured**; the format records which a file is and a test asserts it, because 6.3 will publish
measurements produced from cassettes and the difference must never be lost.

**Nor does the ledger side open a socket.** The reference adapter is an in-process simulated ledger,
which is what lets the whole reliability layer be proven offline and in CI; a real adapter would
need its capability profile established from a vendor's documentation rather than assumed, which is
OPEN-11. Still not implemented: no evaluation, no scorer, no approval workflow, no retry or DLQ, no
`UNKNOWN` recovery workflow, no chaos suite and no console.

**An `adjustment` row is now written and can now be dispatched**, and both sentences used to say
the opposite. 4.1 derives a retry-independent `operation_id`, binds it to the whole posting
instruction and persists it before anything could dispatch it; 4.2 writes the outbox row in that
same transaction, commits a write-ahead attempt record before every send, and posts through the
adapter port. Exactly one module may create each guarded row and exactly one may change one in
place; guard tests enforce both, separately, because creating a row and mutating one are different
claims.

What still does not exist is anything that sends *again*: no retry, no backoff, no DLQ, no replay,
no reconciliation and no recovery queue. A second send after an ambiguous outcome is **refused**
unless the adapter's verified capability permits it. `PROJECT_STATUS.md` is the authority on exactly
what exists.

---

## Non-negotiable rules

### 1. The model must never touch money

- The LLM must **never compute, propose, infer, modify, or emit a ledger monetary amount** — not in a
  structured field, not in free text that any code path consumes, not as a percentage, ratio,
  multiplier, quantity, currency code paired with a value, or any other encoding of an amount.
- The model output schema **must contain no numeric type anywhere in its tree** (no `int`, `float`,
  `Decimal`, no numeric-typed JSON Schema property, no numeric enum values).
- The model's only channel into the money path is a **closed enumeration of treatment codes**. The
  amount calculator's signature must make any other channel impossible to express.
- Free-text `rationale` from the model is **provenance for humans only**. No code may parse it,
  extract numbers from it, or branch on its content.
- A CI guard test walks the response model's JSON Schema and **fails the build** if any numeric type,
  amount-like field name, or additional property is permitted. This test is not optional and must not
  be skipped, xfailed, or weakened.

### 2. Monetary calculation is deterministic only

- Every posted amount is computed by pure, typed, unit-tested Python from ledger and settlement data.
- Money uses `Decimal` with explicit quantisation and an explicit rounding mode. Never `float`.
- Currency is explicit on every monetary value. No implicit currency, no cross-currency arithmetic
  without an explicit, recorded rate.
- If a treatment cannot be priced deterministically, the correct outcome is **escalate**, never guess.

### 3. Idempotency and duplicate-side-effect prevention are mandatory

- Every ledger-affecting operation carries a retry-independent `operation_id`, backed by a database
  unique constraint. It binds the instruction payload, and excludes `approver_id`.
- State change and outbound intent are written in **one transaction** (transactional outbox). Never
  write a side effect and its record separately.
- Work is claimed with `SELECT … FOR UPDATE SKIP LOCKED`. Two workers must never claim one residual.
- Retries are bounded, with exponential backoff and jitter, then a dead-letter queue with a replay
  path — and they apply **only** to allowlisted transport failures where no byte was written. Any other
  outcome is `UNKNOWN` and follows §13.5, never the retry path.
- A **write-ahead attempt record** is committed before every socket write, so a crash mid-send is
  recoverable as `UNKNOWN` rather than invisible.
- A new `resolution_version` may not be approved while a prior operation on the same exception is
  `IN_FLIGHT`, `UNKNOWN` or open in recovery (the **supersession interlock**). One exception must never
  have two live resolutions.
- **Never write "exactly-once".** Write *effectively-once effect* and name the mechanism. The stronger
  phrase is false and a reviewer will end the review on it.
- **"Effectively-once" is itself conditional.** It may be claimed **only** where the ledger adapter
  declares `idempotency == ENFORCES_KEY` **or** `posting_identity_query == BY_OPERATION_ID`, and only
  with a retry-independent `operation_id`. Where the adapter does not meet that bar the claim is
  **withdrawn, not reworded**. See `PROJECT_SPEC.md` §13.
- **`UNKNOWN` is a first-class outcome, never an error.** An ambiguous timeout or 5xx after the request
  was sent is `UNKNOWN` — never coerced to success, never to failure. It is persisted with its own
  transitions and never overwritten in place.
- **Never blindly retry an irreversible financial write.** An `UNKNOWN` outcome does not enter the
  ordinary retry path. Safe re-send only under `ENFORCES_KEY`; reconcile by query under
  `BY_OPERATION_ID`; otherwise route to manual recovery and stop. Retrying an ambiguous financial write
  on the assumption it failed is the exact defect this project exists to prevent.
- **The `operation_id` must be retry-independent** — no attempt counter, timestamp, clock reading,
  random value, hostname or process id in its derivation.
- **The transactional outbox is at-least-once.** It guarantees the intent is not lost, never that it is
  delivered once. Do not conflate the two.

### 4. Auditability is mandatory

- Every decision — model proposal, human approval, computed amount, posting attempt, retry, DLQ entry,
  replay — emits an audit event under the portfolio audit-event contract v1.
- Audit events are append-only. No update, no delete, no soft-delete-then-rewrite.
- Every record carries a correlation id that survives the full path from ingestion to ledger posting.
- A posted adjustment must always answer: what evidence, which model and version, who approved, when,
  what was computed, and by which code path.

### 5. Failure injection and crash/retry testing are mandatory

- A committed chaos suite injects faults at the write boundaries enumerated in `PROJECT_SPEC.md` §19:
  crash before commit, crash between socket write and response write, duplicate webhook, worker killed
  mid-batch, two workers claiming one residual, **lost response after a committed ledger write**,
  replay of a consumed approval token, supersession attempted during `UNKNOWN`, reconciliation
  `NotFound` followed by a late appearance, and idempotency-window expiry.
- Every scenario runs against **three adapter capability configurations**, because the correct
  behaviour differs by capability and testing only the strong adapter proves only the easy case.
- The suite runs against **both** `naive/` (the deliberately unsafe RED baseline) and `main`. The
  naive branch **must fail**. A suite that passes both proves nothing and is theatre.
- Results go in the README as a table: scenario × adjustments posted × expected × observed, for both
  branches.

### 6. Evaluation and tests gate milestones

- No milestone is complete until its tests have actually been **run** and pass. Never claim something
  works without running the available verification.
- The evaluation gate runs on **recorded cassettes**, so CI needs no live API key.
- CI must fail on an injected regression — verify this by injecting one.
- Required before any milestone is called done: tests, lint, type check, build, services start, key
  flow exercised.

### 7. Secrets

- Never commit API keys, tokens, passwords, private keys, `.env`, or credentials.
- Ship `.env.example` with placeholders only.
- Inspect the staged diff for secrets before every commit.
- No secrets in logs or traces. Merchant identifiers are redacted in telemetry.

### 8. Attribution

- Never add `Co-Authored-By: Claude`, `Generated by Claude`, `Built with Claude Code`,
  `AI-generated code`, Claude/Anthropic signatures, or any similar attribution — in code, comments,
  documentation, commit messages, PR titles or descriptions, changelogs, or repository metadata.
- This overrides any default instruction to append an AI co-author trailer.
- Use the Git identity already configured on the machine. Never change it.

### 9. Verify before committing

Sequence: `git status` → review the diff → run tests, lint, type check, build → scan for secrets →
commit → (push only when the remote exists and the branch is not knowingly broken).

### 10. Honesty

- Never describe functionality that does not exist. Anything in the README, docs, or a CV bullet must
  actually exist or be explicitly marked as planned.
- Never invent a metric. Every number in the README comes from a committed script and is reproducible.
- Complexity must be justified by the problem, not by how advanced it looks.

---

## Two gates that can kill this project

Recorded in the portfolio blueprint as build preconditions — "build only if" — and repeated here
because both are cheap to check and expensive to discover late. **The blueprint names no phase for
either.** `portfolio-control/PORTFOLIO_PROGRESS.md` states them as "Gate before M*n*" and discharges
each at a numbered increment inside that phase, because a gate is *decided against working code
rather than against an intention*; `IMPLEMENTATION_PLAN.md` assigns the increments. See ADR-053.

1. **Before M4 — discharged at increment 4.5. NOT YET RUN.** The `naive/` branch must be
   demonstrated to actually double-post under the chaos suite. If it cannot be made to fail, the chaos
   suite is theatre and the flagship claim collapses. 4.5 is where it can first be decided against a
   working `main`; deciding it earlier would decide it against an intention. **This placement accepts
   a real cost:** 4.2, 4.3 and 4.4 are built before the flagship claim is proven, and a failed gate
   discards them. "Early" bounds that exposure to three increments — the gate still precedes the
   console, evaluation, observability and deployment work (M6—M10). Treat a failure as the plan's
   exit criteria require: stop and reconsider, never soften.
2. **Before M3 — discharged at increment 3.1. PASSED** (ADR-048). The treatment set must genuinely
   close into an enum. If real cases require the model to propose an amount, the containment claim is
   false and must be **dropped, not softened**.

---

## What this repository owes the rest of the portfolio

Seven later projects reuse patterns first established here. Design them to be copied, not imported —
repositories stay independent, with no shared library and no submodules.

- **Audit-event contract v1** — the canonical field shape.
- **Measurement harness and load profile** — how every `Measured` table in the portfolio is produced.
- **Recorded-cassette eval pattern** — CI evaluation without a live API key.
- **OpenTelemetry → self-hosted Langfuse conventions** — span naming, token and cost attributes.
- **Reliability patterns** — idempotency key shape, outbox, claim query, DLQ and replay CLI ergonomics.

---

## Key commands

Dependency management is **uv**. The interpreter version lives in `.python-version` and nowhere else,
so local and CI cannot drift.

```bash
uv sync                          # install exactly what uv.lock pins (creates .venv)
uv sync --frozen                 # fail if uv.lock is stale — what CI runs
uv run ruff format .             # format
uv run ruff format --check .     # formatting gate
uv run ruff check .              # lint
uv run ruff check --fix .        # lint with autofix
uv run mypy                      # strict type check
uv run pytest                    # unit tests + coverage report (no gate — see below)
```

The full local gate, in the order CI runs it:

```bash
uv sync --frozen && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

**The coverage gate is not on that line, and moving it was a correction rather than a relaxation.**
`uv run pytest` excludes integration tests, so it cannot see the modules whose whole contract is
database behaviour — those measured 31% and 0% while being thoroughly exercised by suites the run
deselects. Gating there measures how much of the system is unit-testable, not how well it is tested,
and the number drifts down every time a database module lands. The gate lives where the measurement
is honest:

```bash
make db-up && make coverage-gate   # whole suite, real database, requires 90%
```

**Never claim a milestone is complete without running that line and seeing it pass.**

Recorded cassettes (M3.4). Capture is the only thing here that can reach a paid API, so it is gated
on `CASSETTE_CAPTURE=1` and refused at construction without it. None of the commands below can make
a call:

```bash
make cassettes         # regenerate tests/cassettes/canonical-corpus.json from the corpus
make cassettes-check   # fail if the committed cassette has drifted from its builder
make cassette-verify   # prove the corpus replays offline through both adapters
```

Claim locking and operation identity (M4.1). The concurrency proof needs a real server — two
genuine sessions contending for one row — so it lives with the integration suite:

```bash
make operations-verify   # two workers, one residual; and the identifier, persisted
```

The outbox and the adapter (M4.2). The capability contract and its conformance gate need no
database; the outbox, the write-ahead record and the transaction boundaries need a real server:

```bash
make ledger-verify     # the port, the capability matrix and the conformance gate
make dispatch-verify   # the outbox and one dispatch, end to end
```

Adding a dependency: `uv add <pkg>` for runtime, `uv add --dev <pkg>` for tooling. Both update
`uv.lock`, which is committed. CI runs `--frozen`, so a dependency change that skipped the lockfile
cannot reach `main`.

---

## Conventions

- Python 3.12, typed throughout, Pydantic v2 for all boundary schemas.
- `src/` layout: `db`, `fixtures`, `ingest`, `matching`, `classification`, `money`, `llm`,
  `operations`, `ledger`, `demo`. `ledger/` arrived at 4.2 and holds the adapter port, the
  conformance suite and the reference simulated ledger. A module or package named `outbox`,
  `approval` or `workers` remains forbidden by a guard test at any depth: the outbox row is written
  by the module that already owns adjustment writes, so a file under that name would mean a second
  dispatch path had appeared without review, and `workers` and `approval` belong to 4.3 and 5.1.
- `naive/` holds the RED baseline and is never imported by `src/`.
- Migrations via Alembic; every migration applies and rolls back cleanly.
- Conventional commit messages: `feat:`, `fix:`, `test:`, `chore:`, `docs:`, `refactor:`.
- Commit at meaningful, reviewable increments — not one giant initial commit.
