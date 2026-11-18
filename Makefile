COMPOSE := docker compose
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

.PHONY: up down status migrate dbt registry extract chunk embed sync sync-full sync-deletes fixtures test lint fmt notebooks psql logs

.env:
	cp .env.example .env

up: .env
	mkdir -p logs
	$(COMPOSE) up -d --build  # app services start only after db + phoenix are healthy (depends_on)

down:
	$(COMPOSE) down

status:
	$(COMPOSE) exec -T app knesset status

migrate:
	uv run knesset migrate

# Sync runs natively: knesset.gov.il answers Israeli IPs only, so run from the Mac / home server.
sync:
	uv run knesset sync

sync-full:
	uv run knesset sync --full

sync-deletes:
	uv run knesset sync --deletes

fixtures:
	uv run knesset record-fixtures

dbt:
	uv run knesset dbt build

registry:
	uv run knesset registry

# Extraction runs in the app container (LibreOffice inside); needs an Israeli IP.
# Pass options with ARGS, e.g. make extract ARGS="--priority 1 --limit 50"
extract:
	$(COMPOSE) exec -T app knesset extract $(ARGS)

# Chunking and embedding run natively (embedding needs the GPU: uv sync --extra embed).
chunk:
	uv run knesset chunk $(ARGS)

embed:
	uv run --extra embed knesset embed $(ARGS)

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
