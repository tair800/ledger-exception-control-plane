# Developer commands. `make help` lists them.
.DEFAULT_GOAL := help
.PHONY: help install fmt fmt-check lint types test gate coverage-gate up down down-volumes logs \
        ps smoke build db-up test-db-init migrate migrate-down schema-verify fixtures \
        fixtures-check fixtures-load fixtures-verify ingest-verify match-verify classify-verify \
        money-verify m2-demo m2-demo-check cassettes cassettes-check cassette-verify \
        operations-verify dispatch-verify ledger-verify retry-verify approval-verify \
        reconcile-verify audit-verify chaos-verify chaos-table chaos-check \
        golden golden-check eval-verify observability-verify \
        eval-gate eval-gate-update eval-gate-verify label-packet label-packet-verify \
        eval-compare eval-compare-verify \
        smoke-local smoke-selftest secret-scan deploy-check demo demo-api demo-reset \
        demo-principals demo-smoke

# Every Docker command goes through this seam so the whole file can be pointed at a throwaway
# Compose project — which is how the clean-environment bootstrap is proved without destroying
# anyone's volume. It is never anything but this project's own stack in normal use.
COMPOSE ?= docker compose

# The disposable database every integration test targets, and where to reach it. Test-only by
# name: `test-db-init` refuses to create anything the fixture loader would refuse to load into
# (ADR-036).
#
# Both halves are here because a reviewer split them and broke something real. When only the name
# was a variable, `make LECP_TEST_DB=lecp_demo schema-verify` created `lecp_demo`, said so, exited
# 0 — and then the suite failed on `lecp_test`, because the test modules read their own default.
# When only the name followed `COMPOSE`, `fixtures-load` created a database in a throwaway project
# and then reset the corpus in the developer's real one. A target that creates one database and
# then talks to another is worse than no target at all, so every recipe below derives its DSN from
# exactly these three values.
LECP_TEST_DB ?= lecp_test
LECP_DB_PORT ?= 15432
LECP_TEST_DSN = postgresql://lecp:lecp_local_dev@localhost:$(LECP_DB_PORT)/$(LECP_TEST_DB)

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies exactly as pinned by uv.lock
	uv sync --frozen

fmt: ## Format the codebase
	uv run ruff format .

fmt-check: ## Verify formatting (CI gate)
	uv run ruff format --check .

lint: ## Lint
	uv run ruff check .

types: ## Strict type check
	uv run mypy

test: ## Unit tests with coverage (no Docker required)
	uv run pytest

gate: install fmt-check lint types test ## Run the full local quality gate, in CI order

coverage-gate: test-db-init ## The authoritative coverage gate: whole suite, real database
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest -m "" --ignore=tests/test_integration_stack.py --cov-fail-under=90

build: ## Build the application container
	$(COMPOSE) build app

up: ## Start the local stack (postgres, redis, app) and wait for health
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

down-volumes: ## DESTRUCTIVE. Stop the stack and delete its data volumes, lecp_test included
	$(COMPOSE) down -v

ps: ## Show stack status
	$(COMPOSE) ps

logs: ## Tail application logs
	$(COMPOSE) logs -f app

smoke: test-db-init ## Run ALL integration tests (needs the full stack: make up)
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest -m integration -p no:cacheprovider

# --- schema / migrations (need PostgreSQL only, not the whole stack) ---

db-up: ## Start only PostgreSQL, for schema and migration work
	$(COMPOSE) up -d --wait postgres

# The name is captured into a single-quoted shell variable before anything looks at it. Make
# interpolates its variables into the recipe verbatim, so spelling `$(LECP_TEST_DB)` inside the
# double-quoted messages below let a backtick in the value run a command while the guard was busy
# composing its refusal — and the refusal then named a permitted database, because the substitution
# had already eaten the payload. Captured once, quoted once, read as "$$db" everywhere after.
test-db-init: db-up ## Create the disposable integration database if absent (idempotent)
	@db='$(LECP_TEST_DB)'; \
	case "$$db" in \
	  lecp_test|lecp_demo|lecp_fixtures) ;; \
	  *) echo "refusing to create '$$db': an integration database is named lecp_test, lecp_demo or lecp_fixtures" >&2; exit 1 ;; \
	esac; \
	if [ "$$($(COMPOSE) exec -T postgres psql -tAqX -U lecp -d postgres -c \
	        "SELECT 1 FROM pg_database WHERE datname = '$$db'")" = "1" ]; then \
	  echo "$$db already exists"; \
	else \
	  $(COMPOSE) exec -T postgres createdb -U lecp -O lecp "$$db" \
	    && echo "created $$db"; \
	fi

migrate: ## Apply migrations up to head (against LECP_POSTGRES_DSN, or the app's configured database)
	uv run alembic upgrade head

migrate-down: ## Roll back one revision
	uv run alembic downgrade -1

schema-verify: test-db-init ## Verify migrations and schema integrity against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_schema_postgres.py -m integration -p no:cacheprovider --no-cov

# --- deterministic fixture corpus (M1.3) ---

fixtures: ## Regenerate the committed canonical corpus
	uv run python -m ledger_exception_control_plane.fixtures generate

fixtures-check: ## Fail if the committed corpus has drifted from the generator
	uv run python -m ledger_exception_control_plane.fixtures verify

fixtures-load: test-db-init ## Load the canonical corpus into the disposable test database
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run python -m ledger_exception_control_plane.fixtures load --reset

fixtures-verify: test-db-init ## Prove the corpus loads against real PostgreSQL with constraints on
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_fixtures_postgres.py -m integration -p no:cacheprovider --no-cov

# --- settlement ingestion (M2.1) ---

ingest-verify: test-db-init ## Prove ingestion and quarantine against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_ingest_postgres.py -m integration -p no:cacheprovider --no-cov

# --- deterministic matching (M2.2) ---

match-verify: test-db-init ## Prove matching, tolerance and concurrency against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_matching_postgres.py -m integration -p no:cacheprovider --no-cov

# --- residual classification (M2.3) ---

classify-verify: test-db-init ## Prove the taxonomy, provenance, integrity and races against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_classification_postgres.py -m integration -p no:cacheprovider --no-cov

# --- deterministic money path (M2.4) ---

money-verify: ## Prove the calculator, its AI/money firewall and the corpus evaluation (no Docker needed)
	uv run pytest tests/test_money.py tests/test_money_evaluation.py -p no:cacheprovider --no-cov

# --- M2 visual snapshot (demo artifact, outside the milestone ladder) ---

m2-demo: ## Render artifacts/m2-demo.html from real M2 pipeline output (no Docker needed)
	uv run python -m ledger_exception_control_plane.demo render

m2-demo-check: ## Fail if the committed snapshot has drifted from the pipeline
	uv run python -m ledger_exception_control_plane.demo verify

# --- claim locking and operation identity (M4.1) ---

operations-verify: test-db-init ## Prove the claim lock and the persisted identifier against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_operations_postgres.py -m integration -p no:cacheprovider --no-cov

# --- transactional outbox and ledger adapter (M4.2) ---

ledger-verify: ## Prove the adapter capability contract and the conformance gate (no Docker needed)
	uv run pytest tests/test_ledger_adapter.py -p no:cacheprovider --no-cov

dispatch-verify: test-db-init ## Prove the outbox and one dispatch end to end against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_dispatch_postgres.py -m integration -p no:cacheprovider --no-cov

# --- human approval gate (M5.1) ---
#
# The exit criterion is that the gate *blocks the write*, and the thing doing the blocking is a
# composite foreign key — so it can only be demonstrated against a real database.

approval-verify: test-db-init ## Prove the approval gate, roles and single use against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_approval_postgres.py tests/test_approval_api_postgres.py -m integration -p no:cacheprovider --no-cov

# --- bounded retry, dead-letter queue and replay (M4.3) ---
#
# The classifier and the backoff bounds are pure functions and run in the default suite; only the
# scheduling, dead-lettering and replay behaviour needs a server, because every property there is
# about persisted state and transaction boundaries.

retry-verify: test-db-init ## Prove bounded retry, the DLQ and replay against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_retry_postgres.py tests/test_replay_cli_postgres.py -m integration -p no:cacheprovider --no-cov

# --- ambiguous outcomes, reconciliation and manual recovery (M4.4) ---
#
# The bounds and the windows are pure functions and run in the default suite. Everything else here
# is database behaviour: the transitions are rows, the monotonicity is triggers, the count of
# consecutive negative answers is derived from appended evidence, and the supersession interlock
# spans four tables.

reconcile-verify: test-db-init ## Prove the UNKNOWN branch, reconciliation and recovery against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_reconcile.py tests/test_reconcile_postgres.py -m "integration or not integration" -p no:cacheprovider --no-cov

# --- audit-event contract v1 (M5.2) ---
#
# The vocabularies and the mappings are pure and run in the default suite. The three tests §5.2
# names are all database claims: what the services actually emitted, whether the correlation id
# recomputes from the ingested artefact, and whether the least-privilege role is denied a write it
# must not have.

audit-verify: test-db-init ## Prove audit-event contract v1 and the provenance read against real PostgreSQL
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/test_audit_contract.py tests/test_audit_contract_postgres.py -m "integration or not integration" -p no:cacheprovider --no-cov

# --- the chaos suite and the RED baseline (M4.5) ---
#
# The flagship gate. Both branches, every §19 scenario, all three adapter capability
# configurations, against real PostgreSQL — because most of what `main` does about these failures
# *is* database behaviour: a transaction-scoped claim, a unique constraint, an append-only trail.
#
# `chaos-verify` runs the gate and leaves one observation file per cell behind. `chaos-table`
# re-runs it and renders those observations into README.md. `chaos-check` re-runs it and *fails*
# if the committed table has drifted — which is what stops the flagship numbers becoming a
# paragraph nobody re-derives.
#
# The mutation battery that proves this instrumentation can itself go red needs no database and
# runs in the default suite, so it is included here rather than left to a separate command: a gate
# whose falsifiability is checked by a command nobody runs is a gate on trust.

chaos-verify: test-db-init ## Run the kill test: §19 on both branches, three capabilities, real PostgreSQL
	uv run pytest tests/test_kill_test_falsifiability.py -p no:cacheprovider --no-cov
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run pytest tests/chaos -m integration -p no:cacheprovider --no-cov

chaos-table: chaos-verify ## Render the §19 results table into README.md from the run
	uv run python -m tests.chaos.results render

chaos-check: chaos-verify ## Fail if the results table in README.md has drifted from the run
	uv run python -m tests.chaos.results verify

# --- the golden set and the scorer (M6.1) ---
#
# §20's golden set is generated from a seeded corpus by running the shipped deterministic stages —
# ingest, match, classify — and labelling what the classifier left. So it needs no database and no
# model, and `golden-check` is cheap enough to run on every build.
#
# `eval-verify` runs the scorer's own suite. It is separate from `golden-check` because they fail
# for different reasons: the first means the committed artefact is stale, the second means the
# arithmetic or the reporting is wrong. A red build should not make a reader guess which.
#
# **None of these can reach a provider.** No module they import has an HTTP client in its
# dependency graph, and no command takes a credential.

golden: ## Regenerate the committed §20 golden set from the seeded corpus
	uv run python -m tests.evaluation generate

golden-check: ## Fail if the committed golden set has drifted from its generator
	uv run python -m tests.evaluation verify

eval-verify: ## Prove the golden set's schema and the scorer's arithmetic and reporting
	uv run pytest tests/test_golden_set.py tests/test_scorer.py -p no:cacheprovider --no-cov

# --- recorded cassettes (M3.4) ---
#
# The builder lives under tests/ rather than in the package: it runs the fixture generator to
# produce the requests, and no module in the package may import the corpus (the M3.3 fixture-truth
# firewall). A test artifact is made on the test side of that fence.

cassettes: ## Regenerate the committed cassette from the canonical corpus
	uv run python -m tests.cassette_builder generate

cassettes-check: ## Fail if the committed cassette has drifted from its builder
	uv run python -m tests.cassette_builder verify

cassette-verify: ## Prove the harness replays the whole corpus offline (no key, no network)
	uv run pytest tests/test_cassette_harness.py -p no:cacheprovider --no-cov

# --- evaluation (M6.2/6.3) ---
#
# 6.2's gate and 6.3's comparison harness. Both replay the committed cassette offline, so neither
# needs a database, a credential or a network — and neither can reach a provider: no module they
# import has an HTTP client in its dependency graph.
#
# **`eval-gate` is a reproduction gate, not a model-quality gate.** The committed cassettes are
# synthesised and their treatments are assigned round-robin by position, so agreement with the
# golden labels is arithmetic. What the gate protects is evidence assembly, prompt construction,
# request fingerprinting, response parsing, the golden labels and the scorer's arithmetic. The
# accuracy and abstention thresholds a build should fail on remain OPEN-6, and cannot be chosen
# before a real capture exists.
#
# `live-eval` is deliberately absent from this file. It is the one command that would reach a paid
# API, it is gated on an explicit environment opt-in, and putting it behind a `make` target would
# make it one tab-completion away from a run nobody meant to pay for.
#
# A second `.PHONY` rather than an edit to the one at the top of the file. Make accumulates them,
# so a block that declares its own targets can be added or removed in one piece.
.PHONY: eval-gate eval-gate-update eval-gate-verify label-packet label-packet-verify \
        eval-compare eval-compare-verify

eval-gate: ## Fail if the offline evaluation replay has drifted from its committed baseline
	uv run python -m tests.evaluation gate

eval-gate-update: ## Deliberately rewrite tests/golden/replay-baseline.json from the current run
	uv run python -m tests.evaluation gate --update

eval-gate-verify: ## Prove the gate passes on the baseline and fails on an injected regression
	uv run pytest tests/test_evaluation_gate.py -p no:cacheprovider --no-cov

# The hold-out label packet (§20's human-labelled slice, OPEN-15). `label-packet` writes the
# question; nothing in this repository writes the answer. `import-labels` is not a target on
# purpose — it takes a path to a file the owner filled in, so it belongs on a command line rather
# than behind a `make` verb that would need a variable to be useful.

label-packet: ## Write the human-label packet for the frozen hold-out slice (writes no label)
	uv run python -m tests.evaluation packet

label-packet-verify: ## Prove the packet leaks no ground truth and the import validator refuses bad input
	uv run pytest tests/test_human_labels.py -p no:cacheprovider --no-cov

# §20's three-arm comparison (6.3). The deterministic arm is measured; the two model-dependent arms
# print NOT MEASURED, because the committed cassettes are synthesised and carry no token usage and
# cost is computed from provider usage fields or not at all.
#
# It prints rather than writing a file, and that is deliberate: one column is wall clock on the
# machine that ran it, so a committed copy could not be drift-checked the way the §19 results
# table is. Whoever publishes it records this command beside the table.

eval-compare: ## Render §20's three-arm comparison (NOT MEASURED where a live capture is required)
	uv run python -m tests.evaluation compare

eval-compare-verify: ## Prove the comparison harness fabricates no number and gates live capture
	uv run pytest tests/test_three_arm_comparison.py -p no:cacheprovider --no-cov

# --- observability (M8.1) ---
#
# `PROJECT_SPEC.md` §18. Conventions, the redaction gate and the correlation contract, none of
# which needs a database, a network, a provider or an OpenTelemetry SDK — the sink is injected and
# the default one emits nothing. So this runs in the default suite too, and is only broken out here
# because the leak test and its kill test are the pair a reviewer will want to run on their own.
#
# The exit criterion §18 states — a single exception traceable end to end in Langfuse — is **not**
# discharged by this command and cannot be from this branch: it needs the OTel dependency, a
# provider bootstrap and a running collector. `docs/observability.md` §9 says what remains.
#
# The target is declared on the file's single `.PHONY` line above rather than in a second one: the
# guard in `tests/test_tooling_bootstrap.py` reads the first declaration only, so a second block
# would be invisible to it and the target would fail the check it exists to satisfy.
observability-verify: ## Prove the telemetry conventions, the redaction gate and the correlation contract
	uv run pytest tests/test_observability.py -p no:cacheprovider --no-cov

# --- deployment (M10.1) ---
#
# Nothing here deploys anything. These targets run the checks the pipeline runs, so a
# deployment-affecting change can be verified before it is pushed rather than after.
#
# `smoke` (above) is a different thing and the names are worth keeping straight: that target runs
# the *integration test suite* against the local stack. These run the *post-deploy* checks against
# a URL, which is a much smaller question — is the thing at this address alive, ready, refusing
# anonymous callers, and not leaking a connection string.

smoke-local: ## Run the post-deploy smoke checks against the local stack (make up first)
	uv run python scripts/smoke/smoke.py --base-url http://localhost:8000 --environment local

smoke-selftest: ## Prove the smoke checks can still fail: 10 planted deployment faults
	uv run python scripts/smoke/selftest.py

secret-scan: ## Scan tracked files for credentials, unsafe config and frontend exposure
	uv run python deployment/checks/scan.py all

deploy-check: secret-scan smoke-selftest ## Everything the deployment lane gates on, no Docker needed
	uv run --no-project --with pyyaml python -c "import pathlib, yaml; [yaml.safe_load(p.read_text(encoding='utf-8')) for p in pathlib.Path('.github/workflows').glob('*.yml')]; print('workflows parse')"

# Smoke a *deployed* demonstration. The backend's free plan scales to zero, so the wait is long on
# purpose: liveness still has to arrive, it is just allowed to take a container start to do it.
demo-smoke: ## Smoke a deployed demonstration. Pass URL=https://... (and optionally TOKEN=...)
	@test -n "$(URL)" || { echo "usage: make demo-smoke URL=https://your-service.onrender.com"; exit 2; }
	SMOKE_BASE_URL="$(URL)" SMOKE_TOKEN="$(TOKEN)" uv run python scripts/smoke/smoke.py 	  --environment staging --wait-seconds 180 --timeout 30

# --- the local demonstration (M7 support) ---
#
# `demo` leaves a disposable database holding an exception in every state the console renders,
# including the two that matter: one posting recorded UNKNOWN that the system refuses to retry, and
# one dead-lettered with the envelope an operator replays it from. Repeatable — it resets before it
# seeds, because `alembic downgrade base` cannot serve here: the 4.4 migration refuses to downgrade
# while the `recover` events this seeder writes exist, and that refusal protects a real deployment.
#
# No credential and no model: a stand-in proposer supplies the proposal and says so in its own
# rationale, which the console renders verbatim.

# The demonstration's three principals. **Demo-only, and their tokens are published on purpose.**
#
# The registry holds SHA-256 hashes and never a token, so a deployment's registry reveals nothing.
# But that property made the demonstration unusable: a reader following the README reached a
# sign-in page with no way to compute a token that any hash matched, and `.env.example` carries
# placeholder hashes that match nothing at all. A demonstration nobody can sign into is not one.
#
# So these three are recorded here beside their hashes, on the same footing as the development
# PostgreSQL password in `docker-compose.yml`: they authenticate against a *disposable* database
# on localhost with demo mode on, and a deployment supplies its own registry through
# LECP_PRINCIPALS and reuses none of this. `docs/deployment.md` says how, and says why.
DEMO_ANALYST_TOKEN = demo-analyst
DEMO_CONTROLLER_TOKEN = demo-controller
DEMO_OPERATOR_TOKEN = demo-operator
DEMO_PRINCIPALS = {"analyst-a":{"role":"analyst","token_sha256":"cce657f16c436d5357567f215df4e4d5c56f8a19c32916e43562db7905035a04"},"controller-a":{"role":"controller","token_sha256":"5e443ce41f14ce2c5cf6062cad6f583092f065e4901791ae8b63f8667f4bf78d"},"operator-a":{"role":"operator","token_sha256":"9437e87fae95c03d3778f2575bb18ce30e647e51d86a8c37963364e1fc4f374e"}}

# **The migration runs inside the recipe, not as a prerequisite, and that was a real defect.**
# `demo: test-db-init migrate` reads correctly and is wrong: a prerequisite is a separate recipe
# and does not inherit this one's environment, so `migrate` resolved the DSN from `Settings` and
# applied every migration to `lecp` while the line below seeded `lecp_test`. On a clean checkout
# the seeder's first statement then failed against tables that were never created.
demo: test-db-init ## Seed the disposable database so the console has real rows to show
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run alembic upgrade head
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run python -m ledger_exception_control_plane.demo seed

# Deliberately NOT `make up`. That target starts the Compose stack against the `lecp` database,
# which the seeder is forbidden to touch — `assert_target_is_disposable` refuses any name outside
# `lecp_(test|demo|fixtures)`. Pointing the documented demonstration at `make up` left a reader
# with a correctly-running API serving an empty queue, which is the worst of the three outcomes:
# nothing looks broken.
demo-principals: ## Print the demonstration's principal registry, for a deployment's env var
	@echo '$(DEMO_PRINCIPALS)'

demo-api: ## Serve the seeded demonstration on 127.0.0.1:8000 — demo mode on, demo principals loaded
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) \
	LECP_DEMO_MODE=true \
	LECP_PRINCIPALS='$(DEMO_PRINCIPALS)' \
	uv run uvicorn ledger_exception_control_plane.api:create_app \
	  --factory --host 127.0.0.1 --port 8000

demo-reset: test-db-init ## Empty every table the demonstration writes, leaving the schema in place
	LECP_POSTGRES_DSN=$(LECP_TEST_DSN) uv run python -c "import asyncio; from ledger_exception_control_plane.config import Settings; from ledger_exception_control_plane.db.engine import create_engine; from ledger_exception_control_plane.demo.seed import reset_demo; from ledger_exception_control_plane.fixtures.loader import assert_target_is_disposable; s=Settings(); assert_target_is_disposable(s); e=create_engine(s); asyncio.run(reset_demo(e))"
