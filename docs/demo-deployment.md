# The public demonstration: a zero-cost deployment, and what it is not

`deployment.md` describes deploying this control plane to Fly.io and Neon the way you would deploy
something that mattered. This document describes something different and smaller: a **public
portfolio demonstration** on free tiers, whose entire purpose is that a stranger can click a link
and see the system work.

**It is not a production financial deployment and nothing here should be read as one.** It runs a
simulated ledger over a disposable database of synthetic rows. The worst a visitor can do is move
imaginary money between imaginary accounts.

---

## 1. The stack, and why each piece

| Layer | Service | Plan | Region |
|---|---|---|---|
| Console | Vercel | Hobby | `fra1` |
| Control plane | Render Web Service (Docker) | Free | Frankfurt |
| PostgreSQL | Neon | Free | `eu-central-1` |
| Redis | Upstash | Free | `eu-central-1` |

One region, chosen so the database is next to the thing querying it. No multi-region anything.

**Render's own free PostgreSQL is deliberately not used**: it expires after 30 days, which would
take the demonstration down on a schedule. Neon's free tier does not expire.

---

## 2. Compatibility audit

Run before anything was changed. The interesting rows are the ones that were **not** compatible.

| Component | As built | Free target | Compatible? | Change made | Semantic risk |
|---|---|---|---|---|---|
| API process | FastAPI + uvicorn, Docker, port hard-coded 8000 | Render Free (Docker) | after a change | entrypoint binds `$PORT` | none |
| **Background worker** | **does not exist** | — | n/a | **none** | **none — see §3** |
| PostgreSQL driver | asyncpg via SQLAlchemy; DSN passed through verbatim | Neon Free | **no** | DSN normalisation | none — connection-level only |
| Redis | readiness probe only; no queue, no state | Upstash Free | yes | none | none |
| Migrations | Fly `release_command` | Render free has no release hook | **no** | run at container start | see §5 |
| Console | Next.js, server-side proxy, httpOnly cookie | Vercel Hobby | yes | timeout + `maxDuration` for cold start | none |
| CORS | fail-closed, empty by default | — | yes | none — stays empty | none |
| Demo mode | off by default | Render env var | yes | set `true` | publishes the fault injector, intended |
| Ledger adapter | `SimulatedLedger`, in-process | — | yes | none | **none — there is no real adapter to misconfigure** |
| Observability | structlog to stdout; OTel a no-op without an SDK | Render captures stdout | yes | none | external sink not configured |

---

## 3. There is no worker, so nothing was collapsed to fit

The usual free-tier compromise is to cram an API and a background worker into one container and
hope the reliability semantics survive. **That question does not arise here**, and it is worth
being precise about why rather than letting it look like an omission:

- no queue or scheduler library is a dependency — no `arq`, `celery`, `rq`, `dramatiq`, `apscheduler`;
- nothing in `src/` imports one;
- the retry and reconciliation passes are **bounded one-shot functions**, not daemons — each does
  one pass and returns;
- a module or package named `workers` is **forbidden by a guard test** at any depth, and has been
  since 4.3, precisely so that this stays true;
- the only long-running process this system has ever had is the API.

So the free tier runs the same topology the paid one would. No process was merged, faked or
deleted to make the deployment fit, and no financial or reliability semantic changed.

What that *costs* is stated plainly in §6: the bounded passes are not scheduled anywhere in this
deployment, so a dead letter sits in the queue until someone runs the command. That is a real gap,
and it is a gap in the deployment rather than a compromise in the architecture.

---

## 4. The DSN incompatibility, which would have looked healthy

Neon issues a connection string ending `?sslmode=require&channel_binding=require`. Pasted in
verbatim it produces a deployment that **reports itself healthy and cannot serve a request**.

- `/readyz` calls `asyncpg.connect(dsn_string)`. asyncpg parses the URL itself, understands
  `sslmode`, and connects. **The probe goes green.**
- Every actual query goes through SQLAlchemy's asyncpg dialect, which splits the query string into
  *keyword arguments* and calls `asyncpg.connect(**kwargs)`. That signature has no `**kwargs`
  catch-all, so `sslmode=` and `channel_binding=` raise `TypeError`. **Every query fails.**

`db/engine.py` now normalises the DSN: `sslmode` becomes `ssl` (which asyncpg accepts),
`channel_binding` is dropped (asyncpg negotiates SCRAM channel binding itself, it is not a connect
argument), and a pooled endpoint additionally gets `prepared_statement_cache_size=0`, because
pgBouncer in transaction mode does not keep a prepared statement across checkouts.

Unrecognised parameters are **kept**, not dropped — silently discarding a future provider's required
option would be a worse failure than the one this fixes, because there would be no error at all.

`tests/test_engine_dsn.py` pins all of it, including the failing case, and needs no database.

---

## 5. Migrations run at container start, and that contradicts the Dockerfile

The Dockerfile argues at length that migrating on boot is wrong: a process that migrates on start
races every other replica for the same DDL, and on a rolling deploy the old and new schema are live
at once. `deployment/fly.*.toml` therefore uses a `release_command`.

Render's free instance type has **no release or pre-deploy hook** — that is a paid feature. So the
choice is migrate-on-boot or migrate-by-hand, and migrate-on-boot is taken.

The Dockerfile's reasoning does not apply *to this deployment* for a specific reason: **the free
plan runs exactly one instance**, with no horizontal scaling and no rolling deploy. There is no
second replica to race.

**That is a property of the plan, not of the design.** Scale this service to two instances and the
race returns immediately. Recorded here as a limitation rather than resolved, because resolving it
means paying for a release hook.

A failed migration stops the container (`set -e`), the deploy stays unhealthy, and the app never
serves against a schema it does not expect.

---

## 6. Limitations, in full

None of these is hidden, and none of them is a defect. They are what a free deployment of this
system actually is.

**Availability**

- **The backend sleeps.** Render's free plan scales to zero after ~15 minutes idle. The first
  request afterwards waits for a container start — tens of seconds. The console reports this as a
  cold start rather than an error, and the smoke workflow waits up to 180 seconds for liveness.
- **Neon's free branch also suspends** when idle and pays a resume on the next connection. The
  readiness probe timeout is raised to 8 seconds for this.
- **No SLA, on any of the four services.** The demonstration can be down and nobody is paged.

**Topology**

- **One instance.** No redundancy, and therefore the migrate-on-boot allowance in §5.
- **No scheduled passes.** Retry, reconciliation and dead-letter replay are bounded commands with
  nothing running them. A dead letter waits for a person. On a paid plan this is a cron job;
  `deployment.md` §"Scheduled bounded passes" describes what that would look like.
- **No manual approval gate.** Render and Vercel both deploy when `main` moves. Neither free plan
  offers a promotion gate, so the pipeline is Preflight → Tests → Security → Build → *provider
  deploys* → Smoke, and there is no approval step. An approval job that waited for a click and then
  did nothing would be worse than none.

**What the demonstration is made of**

- **Every row is synthetic.** The database holds a settlement file this repository generated. There
  is no customer data, no real merchant, no real amount.
- **The ledger is simulated.** `SimulatedLedger` is an in-process double. There is no real ledger
  integration in this repository at all, so a deployment cannot accidentally acquire one.
- **The proposal is a stand-in, not a model.** No provider credential is configured and no live
  call is made. The proposal shown in the console declares itself: its `model_id` is `stand-in` and
  its rationale opens by saying it was not produced by a model. **Live model quality, cost and
  latency remain NOT MEASURED.**
- **The demo principals are published.** `demo-controller`, `demo-operator` and `demo-analyst` are
  in the `Makefile` beside their hashes, and a test asserts they appear nowhere else. They are safe
  because of what they reach — a disposable database of invented rows behind a simulated ledger —
  and for no other reason. They grant no access to anything outside the demonstration.
- **The fault injector is reachable.** `LECP_DEMO_MODE=true` publishes `/api/v1/demo/…`, including
  the control that crashes a dispatch mid-send. That is the point of the demonstration. Those
  routes answer **404** when demo mode is off, and no other deployment sets it.

**Observability**

- Structured JSON logs go to stdout and Render captures them. `correlation_id`, `exception_id` and
  `operation_id` all survive.
- No external sink is configured. Langfuse and an OTel collector are optional and absent; the
  conventions degrade to a no-op, which is tested.

---

## 7. Owner-set configuration, by name

No value appears in this repository. Set each in the named place.

**Render** — service environment (`sync: false` in `render.yaml`, so Render prompts and encrypts):

| Name | What it is |
|---|---|
| `LECP_POSTGRES_DSN` | The Neon connection string for the `lecp_demo` database. |
| `LECP_REDIS_DSN` | The Upstash `rediss://` URL. |
| `LECP_PRINCIPALS` | The demo registry — the JSON object printed by `make demo-principals`. |

Everything else Render needs is already in `render.yaml` and is not secret.

**Vercel** — project environment:

| Name | Value type |
|---|---|
| `CONTROL_PLANE_BASE_URL` | The Render service URL, no trailing slash, no `/api/v1`. |
| `CONTROL_PLANE_TIMEOUT_MS` | `55000` — just under Vercel Hobby's 60-second function cap. |
| `CONSOLE_COOKIE_SECURE` | `true` — the console is served over HTTPS. |

**GitHub** — repository *variables*, not secrets, because a public demonstration's URL is public:

| Name | What it is |
|---|---|
| `DEMO_API_URL` | The Render service URL. Enables the smoke workflow. |
| `DEMO_CONSOLE_URL` | The Vercel production URL. |
| `DEMO_SMOKE_TOKEN` | Optional. The published `demo-controller` token, for the authenticated read. |

**The Neon database must be named `lecp_demo`.** That is not a convention: the seeder calls
`assert_target_is_disposable`, which refuses any database whose name is not
`lecp_(test|demo|fixtures)`. Point the demonstration at a differently-named database and it will
refuse to write rather than invent transactions in it.
