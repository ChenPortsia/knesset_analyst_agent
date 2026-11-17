COMPOSE := docker compose
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

.PHONY: up down status test lint fmt notebooks psql logs

.env:
	cp .env.example .env

up: .env
	mkdir -p logs
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

status:
	$(COMPOSE) exec -T app knesset status

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

notebooks:
	uv run jupytext --to ipynb --execute notebooks/[0-9]*.py

psql:
	$(COMPOSE) exec db psql -U postgres -d knesset

logs:
	$(COMPOSE) logs -f --tail 100
