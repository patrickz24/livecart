# Common tasks, as thin wrappers around the project's own commands. Run them
# from the repository root, in Git Bash on Windows or in any POSIX shell.
# CONTRIBUTING.md gives the raw commands of every target for PowerShell.

.DEFAULT_GOAL := help
.PHONY: help dev lint test seed load-test gen-api reset

help: ## List the targets
	@awk 'BEGIN { FS = ":.*## " } /^[a-z-]+:.*## / { printf "  %-10s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

dev: ## Build and start the whole stack in the foreground (Ctrl+C stops it)
	docker compose up --build

lint: ## Check the Node version, lint, check formatting and types, then the version pins
	pnpm run check:node
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy
	pnpm run lint
	pnpm exec prettier --check .
	pnpm -r typecheck
	pnpm run check:pins

test: ## Check the Node version, start PostgreSQL and Redis, then run every suite and the coverage gate
	pnpm run check:node
	docker compose up --detach --wait postgres redis
	uv run --locked pytest --cov
	pnpm -r test
	pnpm run test:scripts

seed: ## Load the demo data again, creating only what is missing (the stack seeds itself on up)
	docker compose run --rm seed

load-test: ## Run the k6 comment spike (arrives in milestone 9)
	@echo "No load test yet: the k6 comment spike arrives in milestone 9 (compose loadtest profile)."

gen-api: ## Regenerate openapi.json and the generated TypeScript client
	pnpm run gen-api

reset: ## Stop the stack and delete its volumes (PostgreSQL and Redis data)
	docker compose down --volumes
