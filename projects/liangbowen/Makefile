SHELL := /bin/sh
.SHELLFLAGS := -eu -c

MAKEFILE_PATH := $(shell printf '%s\n' '$(MAKEFILE_LIST)' | sed 's/^[[:space:]]*//')
ROOT_DIR := $(shell cd "$$(dirname '$(MAKEFILE_PATH)')" && pwd -P)
BACKEND_DIR := $(ROOT_DIR)/backend
FRONTEND_DIR := $(ROOT_DIR)/frontend
E2E_DIR := $(ROOT_DIR)/e2e
VENV_DIR := $(ROOT_DIR)/.venv
PYTHON := $(VENV_DIR)/bin/python
VERIFY_PYTHON := $(shell if test -x "$(PYTHON)"; then printf '%s\n' "$(PYTHON)"; else command -v python3; fi)
PIP := $(VENV_DIR)/bin/pip
ALEMBIC := $(VENV_DIR)/bin/alembic
PYTEST := $(VENV_DIR)/bin/pytest
RUFF := $(VENV_DIR)/bin/ruff
NPM := npm
ENV_FILE := $(ROOT_DIR)/.env
COMPOSE := docker compose --project-directory "$(ROOT_DIR)" --env-file "$(ENV_FILE)"
TEST_DATABASE_URL ?= postgresql+asyncpg://grader:grader@localhost:5433/grader_test

.DEFAULT_GOAL := help
.PHONY: help install env db-up db-down migrate seed api-up api-down health \
	mattermost-up mattermost-configure demo demo-full \
	test-backend lint-backend format-backend build-backend verify-backend \
	install-frontend test-frontend lint-frontend build-frontend verify-frontend \
	e2e-install e2e test lint format build verify verify-local foundation

help: ## Show available developer commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' "$(MAKEFILE_PATH)"

install: ## Create Python 3.12 virtualenv and install backend development dependencies
	@test -x "$(PYTHON)" || python3.12 -m venv "$(VENV_DIR)"
	@"$(PIP)" install -e '$(BACKEND_DIR)[dev]'

env: ## Create .env from the documented local defaults if absent
	@test -f "$(ENV_FILE)" || cp "$(ROOT_DIR)/.env.example" "$(ENV_FILE)"

db-up: env ## Start development PostgreSQL, test PostgreSQL, and Redis
	@$(COMPOSE) up -d --wait postgres test-postgres redis

db-down: env ## Stop local Compose services while retaining development data
	@$(COMPOSE) down

migrate: env ## Apply migrations to the host DATABASE_URL from .env
	@PYTHONPATH="$(BACKEND_DIR)" "$(PYTHON)" -m scripts.env_exec \
		--env-file "$(ENV_FILE)" --cwd "$(BACKEND_DIR)" -- \
		"$(ALEMBIC)" upgrade head

seed: env ## Idempotently load demo data (development/test only)
	@PYTHONPATH="$(BACKEND_DIR)" "$(PYTHON)" -m scripts.env_exec \
		--env-file "$(ENV_FILE)" --cwd "$(ROOT_DIR)" -- \
		"$(PYTHON)" -m scripts.seed_demo

api-up: env ## Build and start the API, evaluation worker, and redrive beat
	@$(COMPOSE) up -d --build --wait api worker beat

api-down: env ## Stop and remove the API, evaluation worker, and redrive beat containers
	@$(COMPOSE) rm --stop --force api worker beat

health: ## Require the API readiness endpoint to return HTTP 200
	@PYTHONPATH="$(BACKEND_DIR)" "$(PYTHON)" -m scripts.healthcheck --env-file "$(ENV_FILE)"

mattermost-up: env ## Start the local Mattermost profile and wait for readiness
	@$(COMPOSE) --profile mattermost up -d --wait mattermost

mattermost-configure: env ## Idempotently configure the local Mattermost demo
	@PYTHONPATH="$(BACKEND_DIR)" "$(PYTHON)" -m scripts.env_exec \
		--env-file "$(ENV_FILE)" --cwd "$(ROOT_DIR)" -- \
		"$(PYTHON)" scripts/configure_mattermost.py \
		--api-origin http://api:8000

demo: ## Start the seeded web demo in dependency-safe order
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "db-up"
	+@MIGRATION_LEGACY_PROCESSES_STOPPED=true "$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "migrate"
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "seed"
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "api-up"
	@$(COMPOSE) up -d --build --wait web
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "health"

demo-full: ## Start and configure the complete local demo in dependency-safe order
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "mattermost-up"
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "demo"
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" "mattermost-configure"

test-backend: ## Run the complete backend suite against the isolated test database
	@cd "$(ROOT_DIR)"; PYTHONPATH="$(BACKEND_DIR):$(ROOT_DIR)/scripts" TEST_DATABASE_URL="$(TEST_DATABASE_URL)" \
		MIGRATION_LEGACY_PROCESSES_STOPPED=true \
		"$(PYTEST)" backend/tests scripts/tests -q

lint-backend: ## Run Ruff lint checks
	@cd "$(ROOT_DIR)"; "$(RUFF)" check backend/app backend/migrations backend/scripts backend/tests \
		scripts/configure_mattermost.py scripts/verify_acceptance.py scripts/tests

format-backend: ## Format backend source and tests with Ruff
	@cd "$(ROOT_DIR)"; "$(RUFF)" format backend/app backend/migrations backend/scripts backend/tests \
		scripts/configure_mattermost.py scripts/verify_acceptance.py scripts/tests

build-backend: ## Build an installable backend wheel
	@cd "$(ROOT_DIR)"; "$(PYTHON)" -m pip wheel --no-deps --wheel-dir backend/dist "$(BACKEND_DIR)"
	@cd "$(ROOT_DIR)"; PYTHONPATH="$(BACKEND_DIR)" "$(PYTHON)" \
		-m scripts.verify_wheel_routes --wheel-dir backend/dist

verify-backend: test-backend lint-backend ## Run tests, lint, and formatting verification
	@cd "$(ROOT_DIR)"; "$(RUFF)" format --check backend/app backend/migrations backend/scripts backend/tests \
		scripts/configure_mattermost.py scripts/verify_acceptance.py scripts/tests

install-frontend: ## Install the locked frontend dependency graph
	@cd "$(FRONTEND_DIR)"; "$(NPM)" ci

test-frontend: ## Run the frontend component and design-contract tests
	@cd "$(FRONTEND_DIR)"; "$(NPM)" test

lint-frontend: ## Run frontend ESLint with zero warnings
	@cd "$(FRONTEND_DIR)"; "$(NPM)" run lint

build-frontend: ## Type-check and build the frontend production bundle
	@cd "$(FRONTEND_DIR)"; "$(NPM)" run build

verify-frontend: test-frontend lint-frontend build-frontend ## Verify the complete frontend scaffold

e2e-install: ## Install the locked E2E dependencies and Playwright Chromium
	@cd "$(E2E_DIR)"; "$(NPM)" ci
	@cd "$(E2E_DIR)"; "$(NPM)" exec -- playwright install chromium

e2e: ## Run browser and Mattermost acceptance tests on a disposable local stack
	@cd "$(E2E_DIR)"; "$(NPM)" test

test: test-backend ## Alias for test-backend

lint: lint-backend ## Alias for lint-backend

format: format-backend ## Alias for format-backend

build: build-backend ## Alias for build-backend

verify: ## Run the complete strict acceptance verifier and write its evidence manifest
	@cd "$(ROOT_DIR)"; "$(VERIFY_PYTHON)" scripts/verify_acceptance.py

verify-local: ## Run every non-Docker acceptance gate on a disposable local stack
	@cd "$(ROOT_DIR)"; "$(VERIFY_PYTHON)" scripts/verify_acceptance.py --local-only

foundation: ## Bootstrap and verify the local backend runtime in strict order
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" install
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" env
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" db-up
	+@MIGRATION_LEGACY_PROCESSES_STOPPED=true "$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" migrate
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" seed
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" verify
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" build
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" api-up
	+@"$(MAKE)" --no-print-directory -f "$(MAKEFILE_PATH)" health
