# Developer commands. `make help` lists them.
.DEFAULT_GOAL := help
.PHONY: help install fmt fmt-check lint types test gate coverage-gate up down down-volumes logs ps smoke build \n        db-up migrate migrate-down schema-verify \n        fixtures fixtures-check fixtures-load fixtures-verify ingest-verify match-verify classify-verify money-verify m2-demo m2-demo-check

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

coverage-gate: ## The authoritative coverage gate: whole suite, real database (needs db-up)
	uv run pytest -m "" --ignore=tests/test_integration_stack.py --cov-fail-under=90

build: ## Build the application container
	docker compose build app

up: ## Start the local stack (postgres, redis, app) and wait for health
	docker compose up -d --build --wait

down: ## Stop the stack, keeping volumes
	docker compose down

down-volumes: ## Stop the stack and delete its data volumes
	docker compose down -v

ps: ## Show stack status
	docker compose ps

logs: ## Tail application logs
	docker compose logs -f app

smoke: ## Run ALL integration tests (needs the full stack: make up)
	uv run pytest -m integration -p no:cacheprovider

# --- schema / migrations (need PostgreSQL only, not the whole stack) ---

db-up: ## Start only PostgreSQL, for schema and migration work
	docker compose up -d --wait postgres

migrate: ## Apply migrations up to head
	uv run alembic upgrade head

migrate-down: ## Roll back one revision
	uv run alembic downgrade -1

schema-verify: ## Verify migrations and schema integrity against real PostgreSQL
	uv run pytest tests/test_schema_postgres.py -m integration -p no:cacheprovider --no-cov

# --- deterministic fixture corpus (M1.3) ---

fixtures: ## Regenerate the committed canonical corpus
	uv run python -m ledger_exception_control_plane.fixtures generate

fixtures-check: ## Fail if the committed corpus has drifted from the generator
	uv run python -m ledger_exception_control_plane.fixtures verify

fixtures-load: ## Load the canonical corpus into the disposable test database (needs db-up)
	LECP_POSTGRES_DSN=postgresql://lecp:lecp_local_dev@localhost:15432/lecp_test 		uv run python -m ledger_exception_control_plane.fixtures load --reset

fixtures-verify: ## Prove the corpus loads against real PostgreSQL with constraints on
	uv run pytest tests/test_fixtures_postgres.py -m integration -p no:cacheprovider --no-cov

# --- settlement ingestion (M2.1) ---

ingest-verify: ## Prove ingestion and quarantine against real PostgreSQL (needs db-up)
	uv run pytest tests/test_ingest_postgres.py -m integration -p no:cacheprovider --no-cov

# --- deterministic matching (M2.2) ---

match-verify: ## Prove matching, tolerance and concurrency against real PostgreSQL (needs db-up)
	uv run pytest tests/test_matching_postgres.py -m integration -p no:cacheprovider --no-cov

# --- residual classification (M2.3) ---

classify-verify: ## Prove the taxonomy, provenance, integrity and races against real PostgreSQL (needs db-up)
	uv run pytest tests/test_classification_postgres.py -m integration -p no:cacheprovider --no-cov

# --- deterministic money path (M2.4) ---

money-verify: ## Prove the calculator, its AI/money firewall and the corpus evaluation (no Docker needed)
	uv run pytest tests/test_money.py tests/test_money_evaluation.py -p no:cacheprovider --no-cov

# --- M2 visual snapshot (demo artifact, outside the milestone ladder) ---

m2-demo: ## Render artifacts/m2-demo.html from real M2 pipeline output (no Docker needed)
	uv run python -m ledger_exception_control_plane.demo render

m2-demo-check: ## Fail if the committed snapshot has drifted from the pipeline
	uv run python -m ledger_exception_control_plane.demo verify
