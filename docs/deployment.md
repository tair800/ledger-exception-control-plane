# Deployment — the Fly.io path (not the live one)

> **This is not what is deployed.** The public demonstration runs on **Vercel + Render + Neon +
> Upstash** and is live at <https://ledger-exception-control-plane-livid.vercel.app>. For the
> architecture that is actually running, its URLs and its limitations, read
> [`demo-deployment.md`](demo-deployment.md).
>
> This document describes the **Fly.io** path: what this system would get if it were deployed the
> way something that mattered gets deployed — one image per commit promoted by digest, a
> staging→production pipeline behind an approval gate, and migrations applied as a release command
> rather than from the application process.
>
> It is kept for two reasons. It is the better architecture, and the free tier's compromises are
> only legible against it — `demo-deployment.md` §5 explains why migrations moved to the container
> entrypoint by pointing at the reasoning here. And `deploy.yml` still implements it, gated on
> secrets that are not set, so it stays green and inert.
>
> **No Fly app exists and no deploy on this path has ever run.**

Read this document end to end before deploying on this path. The [prerequisites](#0-prerequisites-that-block-the-first-deploy)
section lists three changes outside this lane's ownership that the first deploy fails without;
`docs/runbook.md` is what to do once it is live.

---

## Architecture

```
GitHub Actions ──▶ ghcr.io/<owner>/<repo>@sha256:…    (one image per commit, built once)
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
      Fly app: lecp-staging       Fly app: lecp-production
      (deploy.yml, no gate)       (GitHub Environment: required reviewer)
              │                           │
              │  release_command: alembic upgrade head
              ▼                           ▼
      Neon project: staging       Neon project: production
      database lecp_demo          database lecp_demo
```

Per the blueprint's deployment spread and ADR-013: **Fly.io** for the application, **Neon** for
PostgreSQL. One `shared-cpu-1x` / 512 MB machine per environment, scaled to zero when idle and
resumed on the first request.

There is **no worker process, and there must not be one.** Neither the retry runner nor the
reconciliation pass is a daemon — each does one bounded pass and returns — and a guard test forbids
a module named `workers` at any depth. What drives them is a scheduled invocation; see
[scheduled bounded passes](#scheduled-bounded-passes).

### What the deployed HTTP surface can and cannot do

Verified against the code, not assumed, because it is what makes a public link safe to send:

- **No route reaches a paid provider.** Nothing outside `src/ledger_exception_control_plane/llm/`
  imports that package, and no route imports it at all. There is no HTTP path from the internet to a
  model call, so there is no unrestricted paid endpoint to expose — the property §22 requires holds
  by construction today rather than by configuration.
- **No route dispatches a financial write.** `routes.py` imports `operations.approval` and
  `operations.recovery` and not `operations.dispatcher`. Approval records a decision; the posting is
  driven separately.
- **The fault-injection endpoint exists and is disabled by default.** `POST
  /api/v1/demo/exceptions/{exception_id}/inject-fault` and `GET /api/v1/demo/fault-targets` both
  answer **404** — not 403 — unless `LECP_DEMO_MODE` is true, and both additionally require operator
  authority. 404 rather than 403 because 403 confirms the route exists and invites a search for the
  credential. Neither Fly configuration sets `LECP_DEMO_MODE`, so neither deployment publishes them.
  This is §16's "the demo fault-injection endpoint is disabled unless demo mode is explicitly
  configured", discharged.
- **No webhook endpoint exists.** §16 requires rate limiting on the settlement webhook; there is no
  such route, so there is nothing to rate limit. Ingestion is driven by the CLI.
- **Every route but `/healthz`, `/readyz` and `/docs` requires a bearer token** resolving to a
  configured principal. With `LECP_PRINCIPALS` empty, every one of them refuses: a control plane
  with no configured humans fails closed.

---

## 0. Prerequisites that block the first deploy

These three are outside this lane's ownership. The first two are hard blockers — the release will
fail. The third is a gap that opens as soon as the operations console ships.

### P1 — the image must contain `alembic.ini` and `migrations/` (blocker)

`release_command = "alembic upgrade head"` runs inside the deployed image. The root `Dockerfile`
copies `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE` and `src/` and nothing else, so the
release machine has no `alembic.ini` and no migration scripts.

Confirmed against the built image rather than inferred — `/app` contains exactly
`LICENSE README.md pyproject.toml src uv.lock`, and although the `alembic` binary is installed at
`/app/.venv/bin/alembic`, running it produces:

```
FAILED: No 'script_location' key found in configuration.
```

The release aborts on that, which means the *first* deploy fails and the app is never released.
Required change, after the `COPY src/ ./src/` line:

```dockerfile
COPY alembic.ini ./
COPY migrations/ ./migrations/
```

`.dockerignore` excludes neither, so no change is needed there.

### P2 — the image must contain `fixtures/canonical/` to seed the demo (blocker for seeding)

`python -m ledger_exception_control_plane.fixtures load` reads `fixtures/canonical`, which is also
not copied into the image. Without it the demo database is empty and the console has nothing to
show. Required change:

```dockerfile
COPY fixtures/ ./fixtures/
```

### P3 — a `demo_mode` setting (blocker for the console, not for this deploy)

`Settings` has no `demo_mode` field. Nothing today needs one — see
[what the deployed HTTP surface can and cannot do](#what-the-deployed-http-surface-can-and-cannot-do) —
but §16 requires the fault-injection control to be disabled unless demo mode is explicitly
configured, so the field must exist before the console adds that control. Required shape:

```python
#: Whether this instance is the seeded public demo. Gates the fault-injection control (§16) and
#: any route that would otherwise be an unrestricted paid endpoint. Defaults to False: a mode that
#: enables extra controls must be opted into, never inherited.
demo_mode: bool = False
```

Named `demo_mode`, so the environment variable is `LECP_DEMO_MODE`. The Fly configs in
`deployment/` deliberately **do not set it**. Absent means false, false means the demo routes answer
404, and a variable that has to be present and correct to keep a fault injector off a deployment is
one edit away from being wrong. The local demonstration sets it explicitly, through `make demo-api`
and nowhere else.

---

## The environment contract

Every variable the application reads, derived from `Settings` in
`src/ledger_exception_control_plane/config.py` — which is the authority, and which rejects any
`LECP_` name not on this list at startup. A test (`tests/test_config.py`) asserts this list and
`.env.example` stay in step with the code.

| Variable | Secret | Meaning |
| --- | --- | --- |
| `LECP_POSTGRES_DSN` | **yes** | PostgreSQL connection string, credentials included. Read by the app and by Alembic. |
| `LECP_REDIS_DSN` | **yes** | Redis connection string. Used by the readiness probe and by nothing else — see [the Redis question](#the-redis-question). |
| `LECP_PRINCIPALS` | **yes** | The authentication table: a JSON object keyed by principal id, each entry carrying a role (`analyst`, `controller`, `operator`) and the SHA-256 of that principal's bearer token. Holds hashes, never tokens. Empty means nobody can authenticate. |
| `LECP_ENVIRONMENT` | no | `local`, `ci`, `staging` or `production`. `production` suppresses the interactive API browser. |
| `LECP_LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `LECP_SERVICE_NAME` | no | Service identity on every log line. |
| `LECP_READINESS_TIMEOUT_SECONDS` | no | Upper bound on each individual readiness probe. `8.0` deployed, `2.0` locally. |
| `LECP_CORRELATION_ID_HEADER` | no | Header carrying an inbound correlation id. `X-Request-ID`. |
| `LECP_DEMO_MODE` | no | Whether this instance is a seeded demonstration. **Leave unset in any deployment.** True publishes the two `/api/v1/demo/` routes, which dispatch a real financial write against whatever ledger adapter is configured. |
| `LECP_CORS_ALLOW_ORIGINS` | no | Comma-separated browser origins permitted to call the API directly. Empty by default, and empty is correct for the shipped console: it talks only to its own origin and forwards server-side, so no allowlist is needed and none is configured. |
| `LECP_RETRY_BASE_DELAY_SECONDS` | no | First delay before a re-send, in seconds. |
| `LECP_RETRY_MULTIPLIER` | no | Growth factor per attempt. |
| `LECP_RETRY_CAP_SECONDS` | no | Ceiling on the computed delay, before jitter. |
| `LECP_RETRY_MAX_ATTEMPTS` | no | Maximum sends per operation, the first included. **Decides how many times an irreversible financial write is offered to a ledger.** |
| `LECP_RETRY_TIME_BUDGET_SECONDS` | no | Wall-clock budget for the whole operation, from the first send. The second, independent bound. |
| `LECP_RECONCILE_CONSECUTIVE_NOT_FOUND` | no | How many consecutive `NotFound` answers a negative resolution needs. Minimum 2. |
| `LECP_RECONCILE_MAX_QUERIES` | no | Reconciliation passes before an operator takes the operation. |
| `LECP_RECOVERY_SLA_HOURS` | no | How long an operator has before a recovery item becomes alertable. |

Not application configuration, and deliberately outside the `LECP_` namespace so they cannot break
startup if they end up in a `.env` file:

| Variable | Secret | Meaning |
| --- | --- | --- |
| `SMOKE_TOKEN` | **yes** | Bearer token for the principal the smoke checks read the queue as. |
| `SMOKE_BASE_URL` | no | Base URL the smoke checks probe. |
| `SMOKE_ENVIRONMENT` | no | Which environment is being checked; `production` additionally asserts the API browser is not served. |
| `SMOKE_WAIT_SECONDS` | no | How long to wait for liveness before checking anything. |
| `SMOKE_CORRELATION_HEADER` | no | Override if the deployment changes the correlation header. |
| `CASSETTE_CAPTURE` | no | Pre-existing. The only switch that can reach a paid API, and only the exact value `1` enables it. **Never set in any deployed environment.** |

### Secret NAMES the owner must create

Values are never written down here, in the repository, or in any commit message.

**Fly secrets, per app** (`fly secrets set NAME=... --app <app>`):

| Name | Applies to |
| --- | --- |
| `LECP_POSTGRES_DSN` | both apps, different values |
| `LECP_REDIS_DSN` | both apps, different values |
| `LECP_PRINCIPALS` | both apps, **different principals and different tokens** |

**GitHub repository secrets** (Settings → Secrets and variables → Actions → Secrets):

| Name | Purpose |
| --- | --- |
| `FLY_API_TOKEN_STAGING` | deploy token scoped to `lecp-staging` |
| `FLY_API_TOKEN_PRODUCTION` | deploy token scoped to `lecp-production` |
| `SMOKE_TOKEN_STAGING` | bearer token for a staging principal, read-only use |
| `SMOKE_TOKEN_PRODUCTION` | bearer token for a production principal, read-only use |

Both tokens for an environment must be present or that environment is skipped. The pipeline reports
which one is missing and does not fail.

**GitHub repository variables** (optional; the workflow falls back to `https://<app>.fly.dev`):

| Name | Purpose |
| --- | --- |
| `BASE_URL_STAGING` | staging base URL, if a custom domain is used |
| `BASE_URL_PRODUCTION` | production base URL, if a custom domain is used |

---

## Owner setup steps, in order

Everything below needs an account, a payment method, a login or a GitHub UI setting, so none of it
can be done from this repository.

### 1. Apply the prerequisites

P1 and P2 above. Verify locally before continuing:

```bash
docker build -t lecp-check .
docker run --rm --entrypoint sh lecp-check -c 'ls alembic.ini migrations/versions | head'
```

### 2. Neon — two projects, one database each

1. Create a Neon project for staging and a second for production. **Separate projects, not two
   branches of one**, so a production incident cannot be caused by a staging action.
2. In each, create a database named **`lecp_demo`**.

   The name is not cosmetic. `fixtures/loader.py` refuses to load the corpus into any database whose
   name does not match `^lecp_(test|demo|fixtures)$` (ADR-036), so a database named `neondb` cannot
   be seeded. `lecp_demo` is also an accurate label: this is a database whose entire contents are
   generated fixtures, and **it must never be pointed at anything holding real data.**
3. Copy the pooled connection string for each. Keep them out of any file.

### 3. Fly.io — two apps

```bash
fly auth login
fly apps create lecp-staging
fly apps create lecp-production
```

The names are fixed in `deployment/fly.staging.toml` and `deployment/fly.production.toml`. If either
is taken, change the `app` line in the corresponding file — nothing else refers to the name.

Then set the secrets on each app, by name only:

```bash
fly secrets set LECP_POSTGRES_DSN=... LECP_REDIS_DSN=... LECP_PRINCIPALS=... --app lecp-staging
fly secrets set LECP_POSTGRES_DSN=... LECP_REDIS_DSN=... LECP_PRINCIPALS=... --app lecp-production
```

`LECP_PRINCIPALS` carries **hashes, not tokens**. Generate a token out of band, hash it, and put the
hash in the registry:

```bash
# the token itself never enters this repository, a commit, or a log
python -c "import hashlib,secrets;t=secrets.token_urlsafe(32);print(t);print(hashlib.sha256(t.encode()).hexdigest())"
```

The first line is the bearer token — this is what goes into `SMOKE_TOKEN_*` and into a reviewer's
password manager. The second is what goes into `LECP_PRINCIPALS`. Give each environment its own
principals: a staging token that works in production is not environment separation.

The principal the smoke checks use needs no special role — the exception queue is readable by every
configured role — so give it `analyst`, which holds no operations authority.

### 4. Redis

See [the Redis question](#the-redis-question) before doing this. If you provision one, Fly's
Upstash integration is the shortest path:

```bash
fly redis create           # note the connection string; set it as LECP_REDIS_DSN on both apps
```

### 5. GHCR — make the package public

The pipeline pushes the image to `ghcr.io/<owner>/<repo>`. Fly pulls it itself and has no GHCR
credentials, so the package must be anonymously pullable.

After the first `image` job succeeds: GitHub → your profile or organisation → Packages → the
package → Package settings → Change visibility → **Public**.

The pipeline checks this for you and fails with a message naming this step rather than letting Fly
produce an authentication error that says nothing about the cause.

### 6. GitHub — the production approval gate

**This is the one step that cannot be expressed in YAML.** GitHub stores environment protection
rules on the environment, not in the workflow, so until this is done `deploy-production` deploys
without waiting for anyone.

Settings → Environments → **New environment** → `production`:

- **Required reviewers** → add yourself (and anyone else who may approve a release).
- **Deployment branches and tags** → *Selected branches* → `main`.
- Optionally a **wait timer**.

Repeat for a `staging` environment (no reviewers — it exists so the deploy shows a URL and so the
branch restriction applies there too).

### 7. GitHub — the credentials

Add the four repository secrets and, if using custom domains, the two variables from
[the environment contract](#secret-names-the-owner-must-create).

### 8. GitHub — platform-side security controls

Settings → Code security:

- **Secret scanning** and **push protection** on. This is the control for provider-key shapes in
  general; `deployment/checks/scan.py` is a narrower, repository-specific check and is not a
  substitute for it.
- **Dependabot alerts** on, so the dependency audit in CI is not the only thing watching.

### 9. Branch protection

Settings → Branches → protect `main` → require these checks:
`Preflight`, `Lint, type check and test`, `Security checks`, `Migrations and schema integrity`,
`Build container image`.

---

## The first deploy

1. Merge to `main`. CI runs.
2. When CI is green the `Deploy` workflow starts on its own. On the very first release, or to
   re-release a known-good commit, run it by hand: Actions → **Deploy** → *Run workflow* → pick the
   ref.
3. `guard` names the target commit and says which environments are configured.
4. `image` builds and pushes one image, and verifies it is publicly pullable.
5. `deploy-staging` releases it. `alembic upgrade head` runs as the release command, in a temporary
   machine on the new image, before any machine takes traffic. A failed migration aborts the release
   and leaves the previous version serving.
6. `smoke-staging` runs the seven post-deploy checks against staging.
7. `deploy-production` waits for a reviewer, then deploys **the same digest** — not a rebuild.
8. `smoke-production` runs the same checks, plus the assertion that the interactive API browser is
   not served.

### Seeding the demo data

The database is empty after the first release. Migrations create the schema; they do not create
rows. After P2 is applied:

```bash
fly ssh console --app lecp-staging \
  -C "python -m ledger_exception_control_plane.fixtures load --reset"
```

`--reset` deletes this corpus's own rows by identifier before inserting — never `TRUNCATE`, so it
cannot remove anything the corpus did not put there. Safe to re-run.

`OPEN-9` (how much seeded data the public demo carries) is still open; the committed canonical
corpus is what this command loads, and choosing a different volume means passing `--instances` to
`fixtures generate` and committing the result, which is a repository decision rather than a
deployment one.

### Verifying by hand

```bash
SMOKE_TOKEN=<staging token> python scripts/smoke/smoke.py \
  --base-url https://lecp-staging.fly.dev --environment staging --require-queue-read
```

The same script the pipeline runs. Standard library only, so there is nothing to install.

---

## Scheduled bounded passes

Two operations need to happen on a timer, and **neither is a daemon**:

- `operations.retry.run_due_once` — one bounded pass over adjustments whose backoff has elapsed.
- `operations.reconcile.reconcile_once` — one bounded reconciliation of an `UNKNOWN` operation.

Both are functions that do one pass and return. `CLAUDE.md` records why a `workers` module is
forbidden and why the driver is a deployment decision.

**Blocked, and this is the one functional gap in this lane.** Neither function has a command-line
entry point, and adding one means editing `src/`, which this lane does not own. The required change,
in `src/ledger_exception_control_plane/operations/__main__.py`, is to widen the existing `command`
choices from `("list", "replay")` to include:

- `retry-pass` — calls `run_due_once`, prints the `RetryReport`, exits non-zero only on an
  unexpected error rather than on a retry having happened.
- `reconcile-pass` — calls `reconcile_once` for each operation due a query, prints the
  `ReconciliationReport`s.

Both must be safe to invoke concurrently with themselves — the claim lock already guarantees that —
and neither may take a `--principal`, because neither records a human decision.

Once those exist, Fly scheduled machines drive them. Scheduled machines are created by the CLI, not
by `fly.toml`:

```bash
# one bounded retry pass per hour
fly machine run --app lecp-production --schedule hourly \
  --region ams --vm-size shared-cpu-1x --vm-memory 512 \
  <image> python -m ledger_exception_control_plane.operations retry-pass

# one bounded reconciliation pass per hour
fly machine run --app lecp-production --schedule hourly \
  --region ams --vm-size shared-cpu-1x --vm-memory 512 \
  <image> python -m ledger_exception_control_plane.operations reconcile-pass
```

A scheduled machine inherits the app's secrets, so it reaches the same database with the same
configuration. Use the digest the app is running (`fly image show --app lecp-production`).

Until then, the queues are worked by hand — `docs/runbook.md` covers that — and the honest statement
is that the deployment has no automatic retry or reconciliation driver.

Today's one available scheduled check is a read-only canary, which reports dead-letter depth into
the app's log stream and writes nothing:

```bash
fly machine run --app lecp-production --schedule hourly \
  <image> python -m ledger_exception_control_plane.operations list
```

---

## Gaps, and what each one costs

Recorded rather than described as covered.

### Request quotas are concurrency limits, not rate limits

`[http_service.concurrency]` bounds concurrent in-flight requests per machine — 25 hard in
production, 40 in staging. That stops one caller occupying the machine. It is **not** per-caller
rate limiting: a steady trickle passes freely, and a single caller can make an unbounded number of
requests over time.

This is enough for the demo because there is no paid endpoint behind it and every data route needs a
bearer token. It is not enough the moment either changes. The required change is application-level
middleware, which is not this lane's to write:

> Per-IP and per-principal request limiting on the `/api/v1/**` routes, bounded and configurable,
> returning `429` with `Retry-After`, exempting `/healthz` and `/readyz` so a rate limiter cannot
> cause a false outage, and never keyed on an unvalidated header. Fly sets `Fly-Client-IP`;
> `X-Forwarded-For` behind it is caller-controlled and must not be trusted.

### The deployed application connects as a migration-capable role

`scripts/sql/provision_app_role.sql` creates a least-privilege `lecp_app` role with no `UPDATE` or
`DELETE` on `audit_event` (§16), and says in as many words that the deployment order is *migrate,
then run this*. But `LECP_POSTGRES_DSN` is a single setting read by both the application and Alembic,
and one setting cannot be two roles — so with migrations running as the release command, the value
must be the migration-capable owner.

What this costs, precisely: the grant-level defence in depth on `audit_event` and
`reconciliation_query` does not apply to the deployed connection. **The primary control still
does** — the append-only triggers apply to every role including the table owner, which is the reason
the SQL file itself calls the grants "defence in depth, not the primary control".

To close it, `Settings` needs a second DSN used only by migrations:

> `migration_postgres_dsn: SecretStr | None = None`, defaulting to `None` and falling back to
> `postgres_dsn` when unset, read by `migrations/env.py` only. Then `LECP_POSTGRES_DSN` becomes the
> `lecp_app` DSN and `LECP_MIGRATION_POSTGRES_DSN` the owner's.

Run the provisioning script against each Neon database after the first release, so the role exists
and the grants are in place regardless:

```bash
psql "<owner DSN>" -f scripts/sql/provision_app_role.sql
```

### The Redis question

`LECP_REDIS_DSN` is read by exactly one thing: the readiness probe. No other module in `src/`
imports Redis. So a deployment provisions a Redis instance in order to satisfy a health check for a
dependency nothing uses, and `/readyz` answers `503` — failing the smoke checks and the deploy —
without one.

Two honest options:

1. **Provision it** (step 4 above). Readiness stays truthful about the declared dependency set, and
   an Upstash free tier costs nothing. This is the recommended path, because it keeps the deployment
   matching the documented architecture.
2. **Stop declaring it**, which is a repository change and not a deployment one: drop the Redis
   probe from `/readyz` and the setting from `Settings` until something actually uses Redis. A
   readiness probe for an unused dependency is a false outage waiting to happen.

Do not take a third option of pointing `LECP_REDIS_DSN` at something that merely answers `PING`.
That makes the probe pass while proving nothing.

### `superfly/flyctl-actions/setup-flyctl@master` is unpinned

Fly's documented installer, referenced by a moving branch, in a job that holds a deploy token. The
exposure is bounded — the job runs only on `main` after CI passed, and each token is scoped to one
app — but it is a real supply-chain gap. Hardening step, once you have seen a successful run: replace
`@master` with the commit SHA that run used, in both deploy jobs.

### No `/metrics` endpoint

§18 names counters (retries, DLQ depth, approvals, abstentions, quarantines) and histograms
(approval, dispatch and end-to-end latency). None is exposed: there is no `/metrics` route and no
OpenTelemetry export, which is increment 8.1's work. The deployment therefore has structured JSON
logs and nothing else to alert on, and the recovery SLA in §13.5 has no mechanism behind it.

If a `/metrics` route is added it must **not** be publicly reachable — bind it behind the same
principal check as the data routes, or serve it on a separate internal port, because dead-letter
depth and approval counts are business information.

### Cost

`OPEN-10` asks for a monthly ceiling for Fly and Neon and whether the demo sleeps when idle. The
second half is decided here: it sleeps. `auto_stop_machines = "suspend"` with
`min_machines_running = 0` means an idle environment costs nothing and the link still works, at the
price of a cold start on the first request after idle — which is why the smoke checks wait for
liveness.

The ceiling itself is a payment decision and stays open. What the configuration commits to: two
`shared-cpu-1x` / 512 MB machines that run only while serving, two Neon projects, and one Redis. Set
a spending limit on both accounts before the first deploy.

---

## What has actually been verified, and what has not

The distinction matters more here than anywhere else in this repository, because everything on this
page describes something that has never run in the environment it is written for.

**Verified, by running it.** The container image builds from the root `Dockerfile`. Alembic applies
every migration from zero to head against a real PostgreSQL 16 and the fixture corpus loads into a
database named `lecp_demo` — 10 ledger entries, 2 settlement batches, 17 settlement lines. The
built image, run with `LECP_ENVIRONMENT=production` against that database, a real Redis and a real
principal registry, passes all seven post-deploy checks: liveness, correlation-id echo,
correlation-id sanitising, readiness reporting both dependencies healthy, the queue refusing an
anonymous caller with `401` and `WWW-Authenticate: Bearer`, an authenticated queue read, and `/docs`
answering `404`. The smoke checks are falsifiable — ten planted deployment faults, each caught by
the check that exists for it. The secret detector is falsifiable — nine credible credentials
reported and nine legitimate placeholders not. Every workflow file parses, and P1 above was
confirmed by inspecting the image rather than by reading the Dockerfile.

**Not verified, and not verifiable from here.** No Fly app or Neon project exists, so nothing about
Fly's release-command mechanism, its health checks, machine suspend and resume, the GHCR pull path,
or Neon's connection behaviour has been exercised — including whether `8.0` is the right readiness
bound for a resuming Neon compute, which is a considered starting point and not a measurement. The
GitHub Actions jobs have never run: `workflow_run` only fires for workflow files on the default
branch, so the deploy workflow will first execute after this reaches `main`. The queue read was
verified against a seeded database containing **no exceptions** — seeding loads settlement data, and
exceptions are produced by running the ingest, match and classify stages — so the check exercised
authentication, the database query and serialisation, and returned an empty list.

## Verifying deployment changes without deploying

```bash
make deploy-check     # the secret and exposure scans, the smoke battery, and workflow YAML
make smoke-selftest   # ten planted deployment faults, each of which must be caught
make secret-scan      # tracked credentials, unsafe config, frontend exposure
make smoke-local      # the post-deploy checks against the local stack (make up first)
```

None of these needs a credential, a cloud account or Docker, except `smoke-local`, which needs the
local stack running.
