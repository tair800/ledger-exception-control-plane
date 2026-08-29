# Developer commands. `make help` lists them.
.DEFAULT_GOAL := help
.PHONY: help install fmt fmt-check lint types test gate up down down-volumes logs ps smoke build

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

smoke: ## Run integration tests against the running stack
	uv run pytest -m integration -p no:cacheprovider
