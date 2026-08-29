# ledger-exception-control-plane

PSP settlement files against the general ledger. Deterministic matching clears the bulk; the model
proposes a treatment from a closed enum whose type has no numeric field. A chaos suite proves no
double-post against a RED baseline that does.

> ## Status: planning
>
> **No application code exists yet.** This repository currently contains planning documents only:
> [`PROJECT_SPEC.md`](PROJECT_SPEC.md), [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) and
> [`DECISIONS.md`](DECISIONS.md).
>
> Nothing described below is implemented. Everything is a specification of intended behaviour.
> No measurement in this repository is a result — the `Measured` table is an obligation the build
> must produce from a committed script, and it will not appear here until it does.

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

**Current state:** the tooling baseline and CI gate exist and pass. There is no application code yet —
the package contains a version and nothing else, and the tests verify the packaging baseline rather
than any behaviour. See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for exactly what is and is not built.

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
