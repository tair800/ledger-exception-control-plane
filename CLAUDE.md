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

**Status: PORTFOLIO MVP COMPLETE.** Every repository-side engineering task is done; three items
are pending owner action and none of them is code. `PROJECT_STATUS.md` is the authority on exactly
what exists, and `DECISIONS.md` on why.

The deterministic core, the reliability layer, the human gate and the audit trail were delivered
across M0–M5 (ADR-048 to ADR-058). **The 4.5 kill-test gate then passed** — 52 scenario runs against
real PostgreSQL, both branches, all three adapter capability configurations: `naive/` commits the
same financial effect twice in five of the seven scenarios, `main` applies at most once in all
twenty-one cells, and every one of the forty-two observed cells matches an expectation declared
before the run (ADR-059). It is now a standing CI step rather than a one-off measurement.

Since then: **6.1 to 6.3** delivered the golden set, the scorer and the evaluation gate — with the
identity migration that made the evaluation joins mean anything, and a scorer that reports the
constant-answer baseline beside every accuracy figure because 214 of 250 labels are `escalate`
(ADR-060, ADR-063). **M7** delivered the operations console, which never lets the bearer token reach
the browser, performs no arithmetic on a monetary value, and makes §19.1 reachable from a button
(ADR-062). **8.1** delivered the observability conventions, §18's metrics and redaction, degrading to
a no-op with no SDK installed (ADR-064). **10.1** delivered the gated deployment pipeline, validated
against the built image and real PostgreSQL, deploying nothing (ADR-065). **11.1** rewrote the README
recruiter-first and added the two documentation checks §11.1 names.

**A critical authorisation defect was found and fixed in that window.** `APPROVAL_ROLES` held both
`ANALYST` and `CONTROLLER`, so an analyst could authorise a ledger posting — contradicting ADR-056's
own table, which states they may not. It shipped at 5.1 and survived four increments because no test
asserted anything about the analyst in either direction. Recording a decision and authorising a
posting are now separate rights (ADR-061).

The model layer still makes **no live call**: no provider SDK is a dependency, nothing under `llm/`
imports an HTTP client, and no transport that speaks HTTP exists — the flow is exercised entirely
through injected fakes and recorded cassettes. The committed cassettes are **synthesised, not
captured**; the format records which a file is and a test asserts it, because 6.3 will publish
measurements produced from cassettes and the difference must never be lost.

**Nor does the ledger side open a socket.** There are now three reference adapters, all in-process,
which is what lets the whole reliability layer be proven offline and in CI. The second — required by
§4.4 and configured `idempotency=NONE, posting_identity_query=NONE` — **genuinely double-books**, so
every "applied exactly once" assertion against it measures our restraint rather than the double's
forgiveness. The third arrived at 4.5 for §19's middle configuration: queryable by operation
identifier, enforcing nothing, and **also genuinely double-booking** — because configuring the
reference adapter `NONE`/`NONE` would have produced a configuration whose behaviour was stronger
than its label, and the label is what an auditor reads. A real adapter would need its capability
profile established from a vendor's documentation rather than assumed, which is OPEN-11. The transport classifier added at 4.3 is
exercised by handing it exceptions directly, for the same reason. The evaluation gate (6.2), the
three-arm comparison (6.3) and the console (M7) have since shipped; this paragraph described the
state before them and the sentence that said otherwise has been removed rather than left to
contradict the status above.

**An `adjustment` row is now written and can now be dispatched**, and both sentences used to say
the opposite. 4.1 derives a retry-independent `operation_id`, binds it to the whole posting
instruction and persists it before anything could dispatch it; 4.2 writes the outbox row in that
same transaction, commits a write-ahead attempt record before every send, and posts through the
adapter port. Exactly one module may create each guarded row and exactly one may change one in
place; guard tests enforce both, separately, because creating a row and mutating one are different
claims.

4.3 added the way back: an allowlisted transport failure — DNS, TCP connect, TLS handshake,
connect-timeout before first byte — is retried with exponential backoff and jitter, bounded by both
an attempt ceiling and a wall-clock budget, and then dead-lettered with its envelope for the replay
command. **Everything else defaults to `UNKNOWN` and is never retried**; an operation whose last
outcome is ambiguous, or which has an unresolved in-flight attempt, is not merely skipped by the
retry path but invisible to it.

4.4 added the only thing that can be done about a send whose outcome nobody knows. Where the adapter
can be queried, reconciliation asks — bounded, scheduled, and never resolving a `NotFound` to
`REJECTED` before N consecutive negatives and both declared windows. Where it can only suppress, a
re-send is permitted **only** inside the declared window and a scope proven against the endpoint the
original send recorded. Where it can do neither, the automatic path stops and an operator takes it.
Every query is appended as evidence, `UNKNOWN` is never overwritten in place, and the transitions are
held by database triggers rather than by application discipline.

5.2 made the trail complete. Every ledger-affecting action emits at least one event under one of ten
verbs; a refused action records the authority it actually **held** rather than the one it attempted;
a reconciliation emits twice, separating what the ledger answered from what was concluded; and two
of §11's ten fields are deliberately null here, with the reason recorded rather than the field
quietly left blank — this system is not an agent, and it makes no model call whose region could be
recorded.

**Two of the three owner-pending items are now discharged.** The human-labelled hold-out slice is
**CONFIRMED** — 25 records labelled by the owner on 2026-09-09, agreeing with the derived table on
all 25, so OPEN-15 is closed (ADR-068); read what that does and does not establish before quoting
it, because the derived label is a pure function of the classification and the slice is therefore
four independent judgements rather than twenty-five. **The demonstration is LIVE** at
<https://ledger-exception-control-plane-livid.vercel.app>, on Vercel Hobby, Render Free, Neon Free
and Upstash Free at zero cost, with no process merged and no semantic weakened to fit — ADR-069 and
`docs/demo-deployment.md`, which also states every limitation. `docs/deployment.md` describes the
Fly.io path and is **not** the live one. OPEN-10 closes.

**One remains, and must still be reported as pending:** live model quality, cost and latency are
**NOT MEASURED** — no provider credential is configured anywhere, including on the four deployed
services, and the committed cassettes are synthesised. Deploying the system did not measure the
model.

**What is genuinely absent:** no orchestration wires the stages into a running service — the demo
seeder composes them and says so; §18's Langfuse trace is not discharged; and there is no real
ledger adapter (OPEN-11). `PROJECT_STATUS.md` is the authority on exactly what exists.

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
- **`audit.emit` is the only thing that builds an event**, and a guard test enforces it. One shape
  means one constructor: the contract is copied by six later repositories, so a second builder is a
  second shape in the one table that cannot be corrected afterwards.
- **A new `tool` verb needs a specification clause behind it.** §11's list is a gloss, not a closed
  set, but widening it is a portfolio decision — add a verb only when a clause requires an event for
  an action the existing verbs cannot name (ADR-058).
- **Never record a field the system cannot know.** `region_jurisdiction` stays null while no model
  call is made; `agent_identity` stays null because this system is not an agent. Name the gap, do
  not fill it.
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
- **`naive/` must stay a legitimate baseline.** Every failure it exhibits is a failure of
  *omission*, and `naive/README.md` maps each omission to the increment that closed it in `src/`.
  Making it absurd to force a red result would forfeit the whole claim; so would writing up a
  *lost* effect as a duplicated one.
- **Every number in the results table comes from the ledger's own applied-count**, recorded by the
  scenario that drove it. §19.1 forbids inferring an outcome from our own records by name, and the
  renderer refuses to produce a table when any of the forty-two cells was not observed.
- Results go in the README as a table: scenario × adjustments posted × expected × observed, for both
  branches — **generated** by `make chaos-table`, never hand-written, and `make chaos-check` fails
  if what is committed there has drifted from what the code does.

### 6. Evaluation and tests gate milestones

- No milestone is complete until its tests have actually been **run** and pass. Never claim something
  works without running the available verification.
- The evaluation gate runs on **recorded cassettes**, so CI needs no live API key.
- **A score carries the origin of the responses it was computed over, and the scorer will not call a
  synthesised run a model measurement.** The committed cassettes are synthesised; a score over them
  measures the harness. `score()` takes a required `origin` with no default, because a default would
  be supplied by every caller that forgot it and the wrong one publishes a fabricated result.
- **Never report accuracy on the golden set without the constant-answer baseline beside it.** 214 of
  250 labels are `ESCALATE`, so answering that every time scores 85.6% while deciding nothing. The
  figure that means something is the accuracy on the 36 priceable records, where the same constant
  scores zero (ADR-060).
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

1. **Before M4 — discharged at increment 4.5. PASSED** (ADR-059). The `naive/` branch had to be
   demonstrated to actually double-post under the chaos suite, and it was: five of the seven
   scenarios, in every capability configuration, while `main` applies at most once in all
   twenty-one cells. The placement accepted a real cost — 4.2, 4.3 and 4.4 were built before the
   claim was proven, and a failed gate would have discarded them — and that cost was not incurred.
   The gate is now a standing CI step rather than a one-off: `make chaos-check` re-runs it and fails
   if the published table has drifted from what the code does.
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

Bounded retry, the DLQ and replay (M4.3). The classifier and the backoff bounds are pure functions
and run in the default suite; the scheduling, dead-lettering and replay behaviour needs a server:

```bash
make retry-verify      # bounded retry, the dead-letter queue and replay, end to end
uv run python -m ledger_exception_control_plane.operations list
uv run python -m ledger_exception_control_plane.operations replay --id <uuid>
```

The human gate (M5.1) and the ambiguous-outcome branch (M4.4). The gate's exit criterion is a
composite foreign key refusing a write, and 4.4's transitions are triggers, so both need a server:

```bash
make approval-verify   # roles, countersignature, single use, and the gate blocking the write
make reconcile-verify  # the UNKNOWN branch across all three capability configurations
make audit-verify      # contract v1, the correlation span, and the provenance read
```

The kill test (M4.5). The flagship gate: §19's scenarios on both branches, across all three adapter
capability configurations, against a real server. The falsifiability battery needs no database and
runs in the default suite, which is why `chaos-verify` runs it first — a gate whose ability to fail
is checked by a command nobody runs is a gate on trust:

```bash
make chaos-verify      # the gate: naive/ must double-post, main must not
make chaos-table       # re-run it and render §19's results table into README.md
make chaos-check       # re-run it and fail if the committed table has drifted
```

The golden set and the scorer (M6.1). Generated by running the shipped deterministic stages over a
seeded corpus, so no database and no model are involved and the drift check is cheap enough for
every build:

```bash
make golden            # regenerate tests/golden/treatment-golden.jsonl
make golden-check      # fail if the committed golden set has drifted from its generator
make eval-verify       # the golden set's schema, and the scorer's arithmetic and reporting
uv run python -m tests.evaluation score <proposals.jsonl> --origin synthesised
```

The local demonstration (M7 support). `demo` is repeatable — it resets before it seeds — and
`demo-api` is deliberately not `make up`: the Compose stack targets the `lecp` database, which the
seeder is forbidden to touch, so pointing the console there renders correct empty states over a
database nobody is reading.

```bash
make demo         # migrate and seed the disposable database
make demo-api     # serve it on 127.0.0.1:8000 with demo mode on and the demo principals loaded
make demo-reset   # empty every table the demonstration writes, leaving the schema
```

Sign in as `demo-controller`, `demo-operator` or `demo-analyst`. Those tokens are published in the
`Makefile` beside their hashes and a test asserts they appear nowhere else.

Adding a dependency: `uv add <pkg>` for runtime, `uv add --dev <pkg>` for tooling. Both update
`uv.lock`, which is committed. CI runs `--frozen`, so a dependency change that skipped the lockfile
cannot reach `main`.

---

## Conventions

- Python 3.12, typed throughout, Pydantic v2 for all boundary schemas.
- `src/` layout: `db`, `fixtures`, `ingest`, `matching`, `classification`, `money`, `llm`,
  `operations`, `ledger`, `demo`. `ledger/` arrived at 4.2 and holds the adapter port, the
  conformance suite, the three reference simulated ledgers and the transport classifier; `4.5` added
  `ledger/faults.py`, the fault-injection port §19 requires. A module or
  package named `outbox` or `workers` remains forbidden by a guard test at any depth: the outbox row
  is written by the module that already owns adjustment writes, so a file under that name would mean
  a second dispatch path had appeared without review; and **`workers` stays forbidden** — neither the
  retry runner nor the reconciliation pass is a background process, each does one bounded pass and
  returns, and what drives them is a deployment decision (10.1). `operations/approval.py` arrived at
  5.1 and is the only module permitted to record a human decision; `operations/reconcile.py` and
  `operations/recovery.py` arrived at 4.4.
- `naive/` holds the RED baseline and is **never imported by `src/`**. The dependency runs one way:
  the baseline reads the ledger port, the reference adapters and the fault vocabulary; the shipped
  package may not know the directory exists. A guard test in
  `tests/test_kill_test_falsifiability.py` parses the package's import statements and enforces it —
  deliberately in a module the *default* suite runs, because a guard behind the `integration` mark
  would leave ruff, mypy and `make gate` green while an `import naive` sat in `src/`.
- `naive/` has its **own tables** (`naive_*`, created by `naive/schema.py`, not by a migration).
  `main`'s constraints would otherwise protect the baseline from its own defect and the comparison
  would be rigged in its favour.
- `tests/evaluation/` holds §20's golden-set generator, its label declaration and the scorer, and
  `tests/golden/` the committed artefact. **Under `tests/` because it reads the answer key**, which
  the fixture-truth firewall keeps out of the decision path; `tests/cassette_builder.py` set the
  precedent for the same reason. Putting it in `src/` would have meant adding a third exemption to
  that guard, and exempting a module from a firewall in order to build the thing the firewall exists
  to keep out is the wrong direction (ADR-060).
- Migrations via Alembic; every migration applies and rolls back cleanly.
- Conventional commit messages: `feat:`, `fix:`, `test:`, `chore:`, `docs:`, `refactor:`.
- Commit at meaningful, reviewable increments — not one giant initial commit.
