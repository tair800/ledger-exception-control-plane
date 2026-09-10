# Operator runbook

Procedures for a deployed instance, **written for the Fly.io deployment `deployment.md` describes**
— an instance with a release hook, a scheduler for the bounded passes, and a database holding
something worth reconciling.

**The live public demonstration is not that instance.** It runs on free tiers, it is described in
[`demo-deployment.md`](demo-deployment.md), and the procedures below apply to it only where they do
not assume `flyctl` or a scheduled pass. Its own limitations — no scheduler, one instance,
migrate-on-boot, a consumable fault target with a reset button — are listed there rather than here,
because a runbook that quietly mixed the two would send an operator to a command that does not
exist on the host they are on.

**Every command below exists in this repository or in `flyctl`.** Nothing is invented; where a
capability is missing, this document says so rather than describing a command that would be
convenient.

Two rules that override anything convenient:

- **Never re-send an ambiguous financial write on the assumption it failed.** An `UNKNOWN` outcome
  means nobody knows whether the ledger applied the posting. Re-sending is safe only under a
  declared and verified adapter capability; otherwise a human establishes what happened from the
  ledger's own records. Retrying an ambiguous write is the exact defect this system exists to
  prevent.
- **Read before you write.** Every procedure here begins with an inspection command that changes
  nothing, and most incidents end there.

---

## Where to run things

Three places, and the difference matters.

| Where | How | Use for |
| --- | --- | --- |
| Inside a machine | `fly ssh console --app <app> -C "<command>"` | Anything reading or writing the deployed database. The machine already holds the credentials, so no DSN passes through your shell. |
| Your own machine | `uv run <command>` with `LECP_POSTGRES_DSN` exported | Only when a machine is unavailable. You are handling a production credential in a shell; prefer the row above. |
| The API | `curl` with a bearer token | Anything a human decision is recorded for — approvals and recovery resolutions. These are deliberately **not** available as CLI commands. |

`fly ssh console -C` runs one command and exits. Drop `-C` for an interactive shell.

---

## Reading the state

```bash
# Is it up, and is it reachable?
fly status --app lecp-production
python scripts/smoke/smoke.py --base-url https://lecp-production.fly.dev --environment production

# Logs. Structured JSON, one object per line, every line carrying a correlation id.
fly logs --app lecp-production

# Which image is running? Needed for a rollback and for scheduled machines.
fly image show --app lecp-production

# Release history.
fly releases --app lecp-production
```

`/readyz` reports per-dependency status and returns `503` when a dependency is unavailable. It never
returns a DSN, a host or a stack trace, so the answer to "why" is in the logs, not in the payload.

`/healthz` is liveness and probes nothing external. It stays green while the database is down, on
purpose (ADR-015): a health check coupled to PostgreSQL turns a database blip into a restart storm
that removes the capacity needed to recover.

---

## Replaying a dead-letter entry

A dead letter is an operation whose transport failed in an allowlisted way — DNS, TCP connect, TLS
handshake, or a connect timeout **before any request byte was written** — until both retry bounds
were exhausted. Its envelope is persisted, and a replay re-reads the stored posting instruction
rather than rebuilding one.

Everything else is `UNKNOWN` and never reaches this queue. If the operation you are looking at is
ambiguous rather than dead-lettered, go to [working the recovery queue](#working-the-recovery-queue).

### 1. Look

```bash
fly ssh console --app lecp-production \
  -C "python -m ledger_exception_control_plane.operations list"
```

Prints the pending entries, up to 100, and says on stderr whether more remain — a bounded page, so a
backlog is never mistaken for an empty queue.

### 2. Rehearse

```bash
fly ssh console --app lecp-production \
  -C "python -m ledger_exception_control_plane.operations replay --id <uuid> --dry-run"
```

`--dry-run` reports what would be replayed and sends nothing. It needs no `--principal`, because
nothing irreversible happens.

### 3. Replay

```bash
fly ssh console --app lecp-production \
  -C "python -m ledger_exception_control_plane.operations replay --id <uuid> --principal <your-principal-id>"
```

`--principal` is **required** and is not defaulted. A re-send is an irreversible financial write and
the audit trail has to say who ordered it; the command refuses rather than recording `system`.

`--id` is repeatable. `--all` takes the whole queue, bounded to 100 entries per invocation — an
unbounded replay across a large backlog is a decision nobody took explicitly. `--adapter` selects
the ledger adapter and today has one value, `simulated`.

### 4. Confirm

A replay of an operation that already reached a terminal outcome applies nothing further; that is
the property the chaos suite proves. Read the result back:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  https://lecp-production.fly.dev/api/v1/exceptions/<exception-id> | jq '.outbox, .attempts'
```

`attempts` lists every posting attempt with its state, outcome and posting reference. Two attempts
with one `confirmed` outcome is a successful replay. Two `confirmed` outcomes for one operation is an
incident — capture the response and stop.

---

## Working the recovery queue

A recovery item is an operation whose outcome nobody knows and whose adapter cannot settle it
automatically: it can neither suppress a duplicate under a declared key nor answer a query by
operation identifier. §13.5 routes these to a human, and that human is you.

### 1. Look

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://lecp-production.fly.dev/api/v1/recovery?limit=50" | jq

# only items past their SLA — LECP_RECOVERY_SLA_HOURS, 24 by default
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://lecp-production.fly.dev/api/v1/recovery?stale=true" | jq
```

Readable by every configured role: seeing that an operation is stuck is not the same authority as
judging what happened to it, and an analyst who cannot see it cannot escalate it either.

Each item carries `reason`, `operation_id`, `sla_due_at`, `overdue`, the principal who approved the
underlying resolution, and — the field to read first — **`evidence_procedure`**. That is the queue
telling you what evidence this particular kind of ambiguity needs. Follow it rather than improvising.

### 2. Establish what the ledger did

Outside this system. Query the ledger for the `operation_id`, or read the counterparty's statement.
**Do not infer the outcome from our own records** — that is what made the operation ambiguous in the
first place, and §19.1 forbids it by name.

### 3. Record the judgement

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resolution":"confirmed_by_evidence","posting_ref":"<the ledger'"'"'s own reference>"}' \
  https://lecp-production.fly.dev/api/v1/recovery/<recovery-id>/resolve
```

Three resolutions, and the third is not a synonym for either of the others:

| `resolution` | Means | `posting_ref` |
| --- | --- | --- |
| `confirmed_by_evidence` | The ledger applied it, and here is its reference. | **required** |
| `rejected_by_evidence` | The ledger did not apply it, established from evidence. | must be absent |
| `resolved_unverified` | No obtainable evidence. A judgement was made anyway. | must be absent |

`resolved_unverified` deliberately does **not** settle the operation to a terminal outcome. The
operation stays ambiguous and permanently closed to the automatic path, with a name and a timestamp
against the judgement. That is what makes an unverifiable decision visible to an auditor instead of
indistinguishable from a verified one.

**Only the `operator` role may resolve an item.** An analyst or controller is refused, and the
refusal is audited with the authority they actually held. A principal who can both authorise a
posting and adjudicate what happened to it is not a separation of duties.

**This never causes a posting.** Resolving a recovery item records what was found; it does not send
anything.

### 4. Note what is missing

There is no CLI for the recovery queue, by design — a recovery resolution is a human decision and
belongs behind an authenticated principal. There is also **no alerting**: `LECP_RECOVERY_SLA_HOURS`
sets when an item becomes an alertable condition, and nothing alerts, because no `/metrics` endpoint
or telemetry export exists yet (increment 8.1). Until it does, `?stale=true` is checked by a human
on a schedule they keep themselves.

---

## Rolling back a release

Reach for this when the production smoke checks fail, or when a release is serving and wrong.

**Ask first whether the schema moved.** A rollback returns the *code*; it does not return the
database. If the release included a migration, read
[rolling back across a migration](#rolling-back-across-a-migration) before doing anything.

### 1. Find the previous image

```bash
fly releases --app lecp-production        # release history
fly image show --app lecp-production      # what is running now
```

The registry also holds one tag per commit, `ghcr.io/<owner>/<repo>:<sha>`, so the previous good
commit's SHA is enough to name its image.

### 2. Redeploy the previous digest

```bash
fly deploy \
  --config deployment/fly.production.toml \
  --image ghcr.io/<owner>/<repo>@sha256:<previous digest> \
  --app lecp-production \
  --wait-timeout 900
```

The same command the pipeline runs, with an older digest. `alembic upgrade head` runs again as the
release command and is a no-op when the schema is already at head.

Or, equivalently and with the approval gate intact: Actions → **Deploy** → *Run workflow* → choose
the previous good commit. That rebuilds the image from that commit rather than reusing the digest,
which is slower but leaves an ordinary audit trail of who released what.

### 3. Verify

```bash
python scripts/smoke/smoke.py --base-url https://lecp-production.fly.dev --environment production
```

### Rolling back across a migration

Alembic can reverse one revision at a time, and every migration in this repository is required to
apply and roll back cleanly (a CI job proves it against real PostgreSQL). What it cannot do is
restore data a downgrade drops.

```bash
# inspect first
fly ssh console --app lecp-production -C "alembic current"
fly ssh console --app lecp-production -C "alembic history --verbose"

# one revision back
fly ssh console --app lecp-production -C "alembic downgrade -1"
```

Order matters and getting it wrong is how you serve new code against an old schema:

- **Rolling back a release that added a migration:** downgrade the database **after** the old image
  is serving, not before. The old code does not know about the new column; the new code does not
  work without it.
- **Take a Neon branch first.** Neon branches are instant and cheap, and a branch is the only thing
  that makes a data-destroying downgrade reversible. Do it before the downgrade, not after
  discovering you needed it.
- **If the downgrade fails**, stop. Do not hand-edit `alembic_version`. Go to
  [a failed migration](#a-failed-migration).

---

## A failed migration

The release command failed, which means **the release aborted and the previous version is still
serving.** That is the intended behaviour and it is why migrations run there rather than in the
application process. You have a working service and a schema that did not move.

### 1. Establish where the schema actually is

```bash
fly logs --app lecp-production                                    # the Alembic error
fly ssh console --app lecp-production -C "alembic current"        # the recorded revision
fly ssh console --app lecp-production -C "alembic heads"          # where head is
```

Alembic runs each migration in a transaction, so a failed migration normally leaves the schema and
`alembic_version` both unmoved. Confirm it rather than assume it: a migration containing DDL
PostgreSQL cannot run transactionally is the case that breaks the assumption.

### 2. Decide which failure this is

- **The migration is wrong.** Most common. Fix it in the repository, let CI's `schema` job prove it
  applies and reverses against real PostgreSQL, and release again. The `schema` job runs every
  migration from zero to head and back, which is exactly this failure caught earlier.
- **The migration is right but the data is not** — a constraint the existing rows violate. The
  migration needs a data step, and that step belongs in a migration rather than in a shell.
- **The database was unreachable, or the role lacks a privilege.** Nothing is wrong with the
  migration. Check `fly secrets list --app lecp-production` (names only; values are never shown),
  confirm the Neon project is awake, and confirm the DSN's role can perform DDL — see
  `docs/deployment.md` on which role the deployment connects as.
- **The schema moved and `alembic_version` did not**, or the reverse. The only case that needs a
  human at a `psql` prompt. Take a Neon branch first. Then bring the two into agreement by running
  the missing DDL or by `alembic stamp <revision>` — never by editing the table by hand.

### 3. Do not work around it

Two things that are always wrong here:

- **Never** make the application create its own schema. `create_all()` is not the migration
  mechanism (ADR-021), and a serving process running DDL turns a rollout into a race between
  instances.
- **Never** deploy with the release command removed to "get it out and fix the schema after". That
  is how a version ends up serving against a schema it was never tested on.

---

## Verifying integrity after any incident

The trail is append-only and enforced by triggers that apply to every role including the table
owner, so it survives whatever happened. Three questions worth asking afterwards:

```bash
# Does every posting attempt reconcile to at most one applied posting?
curl -sS -H "Authorization: Bearer $TOKEN" \
  https://lecp-production.fly.dev/api/v1/exceptions/<exception-id> | jq '.attempts'

# Is anything still ambiguous or past its SLA?
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://lecp-production.fly.dev/api/v1/recovery?stale=true" | jq

# Is anything still dead-lettered?
fly ssh console --app lecp-production \
  -C "python -m ledger_exception_control_plane.operations list"
```

Two `confirmed` outcomes for one `operation_id` is the one finding that matters more than the
incident that produced it. Capture the exception detail and the logs for the correlation id before
changing anything: that is the claim this whole system makes, and a counterexample must be preserved
rather than tidied.
