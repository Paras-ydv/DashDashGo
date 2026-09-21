# DashDashGo developer commands. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash

REPORT ?= weekly_sales
COMPOSE := docker compose
# Host-side commands talk to the Compose services through their published ports.
HOST_ENV := CLICKHOUSE_HOST=localhost METABASE_URL=http://localhost:3000

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: env
env: ## Create .env from .env.example with random secrets (never overwrites)
	@if [ -f .env ]; then echo ".env already exists - leaving it alone"; else \
		python3 scripts/make_env.py && echo "Created .env with generated secrets"; fi

.PHONY: setup
setup: env ## Local dev setup: .env, Python deps (uv), Playwright Chromium
	uv sync
	uv run playwright install chromium

.PHONY: up
up: env ## Build and start the full stack (ClickHouse, Metabase, app)
	$(COMPOSE) up --build -d
	@echo "Waiting for the app to become healthy..."
	@for i in $$(seq 1 60); do \
		status=$$(docker inspect -f '{{.State.Health.Status}}' $$($(COMPOSE) ps -q app) 2>/dev/null); \
		[ "$$status" = healthy ] && break; sleep 3; done; \
		port=$$(grep -E '^APP_PORT=' .env | cut -d= -f2); \
		echo "DashDashGo UI:  http://localhost:$${port:-8000}"; \
		echo "API docs:       http://localhost:$${port:-8000}/docs"; \
		echo "Metabase demo:  http://localhost:3000"

.PHONY: down
down: ## Stop the stack (keeps data volumes)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Follow the app logs
	$(COMPOSE) logs -f app

.PHONY: run
run: ## Run one pipeline now in the app container: make run REPORT=customer_usage
	$(COMPOSE) exec app dashdashgo run $(REPORT)

.PHONY: validate
validate: ## Validate every report config
	$(COMPOSE) exec app dashdashgo validate

.PHONY: test
test: ## Unit tests (no infrastructure needed)
	uv run pytest

.PHONY: test-integration
test-integration: ## Python <-> ClickHouse tests against the running stack
	$(HOST_ENV) uv run pytest -m integration

.PHONY: test-e2e
test-e2e: ## Full Metabase -> Playwright -> ClickHouse tests inside the app container
	$(COMPOSE) exec app pytest -m e2e

.PHONY: test-all
test-all: ## Every test suite, inside the app container
	$(COMPOSE) exec app pytest -m ""

.PHONY: lint
lint: ## ruff (lint + format check) and mypy --strict
	uv run ruff check src tests demo scripts
	uv run ruff format --check src tests demo scripts
	uv run mypy

.PHONY: format
format: ## Auto-format the code
	uv run ruff format src tests demo scripts
	uv run ruff check --fix src tests demo scripts
