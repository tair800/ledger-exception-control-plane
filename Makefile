# Developer commands. `make help` lists them.
.DEFAULT_GOAL := help
.PHONY: help install fmt fmt-check lint types test gate coverage-gate up down down-volumes logs \
        ps smoke build db-up test-db-init migrate migrate-down schema-verify fixtures \
        fixtures-check fixtures-load fixtures-verify ingest-verify match-verify classify-verify \
        money-verify m2-demo m2-demo-check cassettes cassettes-check cassette-verify \
        operations-verify dispatch-verify ledger-verify

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
