# Kazdy punkt Definition of Done ma tu swoja jedna komende.
.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup lint format typecheck test check clean

help: ## Lista dostepnych komend
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Instaluje interpreter 3.12, tworzy .venv i synchronizuje workspace
	uv python install 3.12
	uv sync --all-packages

lint: ## ruff check
	uv run ruff check .

format: ## ruff format + autofix importow
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy strict
	uv run mypy

test: ## pytest z pokryciem, bez testow wydajnosciowych
	uv run pytest -m "not slow" --cov --cov-report=term-missing

check: lint typecheck test ## Pelna bramka jakosci, to samo co CI

clean: ## Usuwa cache narzedzi i artefakty lokalne
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
