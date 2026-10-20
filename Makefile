.PHONY: up down logs demo demo-dry-run demo-activate demo-resolve test lint typecheck

COMPOSE ?= docker compose

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

demo: up
	$(COMPOSE) run --rm --no-deps api ouros-audience-engine run-once --publish

demo-dry-run: up
	$(COMPOSE) run --rm --no-deps api ouros-audience-engine run-once --dry-run

demo-activate:
	$(COMPOSE) exec -T postgres psql -U audience_owner -d audience -v ON_ERROR_STOP=1 < docker/postgres/demo_activate.sql
	$(COMPOSE) run --rm --no-deps api ouros-audience-engine run-once --publish

demo-resolve:
	$(COMPOSE) exec -T postgres psql -U audience_owner -d audience -v ON_ERROR_STOP=1 < docker/postgres/demo_resolve.sql
	$(COMPOSE) run --rm --no-deps api ouros-audience-engine run-once --publish --as-of 2026-11-01T12:00:00Z

test:
	python -m pytest

lint:
	ruff check src tests

typecheck:
	mypy src
