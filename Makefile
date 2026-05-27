define HELP
make [TARGET]

TARGET
    pyenv                      Create the Python virtual environment (pyenv)
    install                    Sync Python deps from requirements.txt
    upgrade                    Recompile requirements.txt from requirements.in
    web-install                npm install in web/
    infra                      Bring up Postgres, Langfuse, SearXNG (docker compose)
    infra-down                 Stop and remove the docker compose stack
    infra-logs                 Tail logs for the docker compose stack
    migrate                    Run pending SQL migrations against the local Postgres
    db-reset                   Drop and recreate the boardroom schema (destructive)
    psql                       Open a psql shell in the postgres container
    bootstrap                  Seed the company and generate exploration kickoff
    harness                    Run the autonomous loop (foreground; Ctrl-C to stop)
    api                        Run the FastAPI command center on :$$API_PORT (default 8503)
    web                        Run Next.js dev server on :$$WEB_PORT (default 3000)
    dev                        Run api + web together (Ctrl-C stops both)
    research-mcp               Run the boardroom-research MCP server (stdio)
    state-mcp                  Run the boardroom-state MCP server (stdio)
    pre-commit                 Install pre-commit hooks
    check                      Run pre-commit checks
    ruff                       Run ruff lint + format
    test                       Run unit tests
    test-last-fail             Re-run the tests that failed last time
    clean                      Remove caches and __pycache__ dirs

See readme.md for setup.
endef
export HELP

help:
	@echo "$$HELP"


# Auto-load .env so DATABASE_URL, ZEP_API_KEY, etc. are visible to all targets.
ifneq (,$(wildcard .env))
    include .env
    export
endif


# ---------------------- Python ----------------------

PYTHON_VERSION  = 3.11
PYENV_NAME      = boardroom

API_PORT       ?= 8503
WEB_PORT       ?= 8088

# System Node is too old; use the nvm-installed Node 20 for the web app.
NODE_BIN       ?= /home/nicholas/.nvm/versions/node/v20.20.2/bin
WEB_ENV         = PATH=$(NODE_BIN):$$PATH

# Explicit env prefix so uv / pip work even when pyenv shims aren't active
# in the non-interactive Make shell.
VENV_PREFIX = $(shell pyenv prefix $(PYENV_NAME) 2>/dev/null)
VENV_PYTHON = $(VENV_PREFIX)/bin/python
UV          = VIRTUAL_ENV=$(VENV_PREFIX) uv

pyenv: # Create the Python virtualenv (pyenv). Idempotent.
	pyenv install -s $(PYTHON_VERSION)
	@pyenv virtualenvs --bare | grep -qx "$(PYENV_NAME)" \
		|| pyenv virtualenv $(PYTHON_VERSION) $(PYENV_NAME)
	pyenv local $(PYENV_NAME)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install uv

upgrade:
	$(UV) pip compile -U requirements.in -o requirements.txt

install:
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(UV) pip sync requirements.txt

pre-commit:
	pre-commit install

check: | pre-commit
	pre-commit run --all-files

ruff:
	ruff check --select I --fix
	ruff format

test:
	pytest

test-last-fail:
	pytest --lf


# ---------------------- Infrastructure ----------------------

infra:
	docker compose up -d
	@echo ""
	@echo "  Postgres : localhost:5432  (boardroom/boardroom)"
	@echo "  Langfuse : http://localhost:3000"
	@echo "  SearXNG  : http://localhost:8080 (if port free)"

infra-down:
	docker compose down

infra-logs:
	docker compose logs -f --tail=50

PG_CONTAINER ?= boardroom-postgres-1

# Run migrations against the local Postgres via docker exec, so no host psql needed.
migrate:
	@for f in migrations/*.sql; do \
		echo ">> applying $$f"; \
		docker exec -i $(PG_CONTAINER) psql -U boardroom -d boardroom < $$f \
			|| { echo "migration $$f failed"; exit 1; }; \
	done

# Drop and recreate the public schema in the boardroom DB. Destructive — wipes
# all data and types. Avoids "DROP DATABASE inside transaction" psql quirk.
db-reset:
	docker exec -i $(PG_CONTAINER) psql -U boardroom -d boardroom -c \
		"DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public AUTHORIZATION boardroom; GRANT ALL ON SCHEMA public TO boardroom;"
	@$(MAKE) migrate

# Open a psql shell inside the postgres container.
psql:
	docker exec -it $(PG_CONTAINER) psql -U boardroom -d boardroom


# ---------------------- Boardroom commands ----------------------

bootstrap:
	python -m boardroom.bootstrap

harness:
	python -m boardroom.harness.main

api:
	$(VENV_PYTHON) -m uvicorn boardroom.api.main:app --host 0.0.0.0 --port $(API_PORT) --reload

.PHONY: web web-install dev api harness bootstrap

web-install:
	cd web && $(WEB_ENV) npm install

web:
	cd web && $(WEB_ENV) PORT=$(WEB_PORT) npm run dev

dev:
	@trap 'kill 0' INT TERM EXIT; \
	$(VENV_PYTHON) -m uvicorn boardroom.api.main:app --host 0.0.0.0 --port $(API_PORT) --reload & \
	(cd web && $(WEB_ENV) PORT=$(WEB_PORT) npm run dev) & \
	wait

research-mcp:
	python -m boardroom.mcp_servers.boardroom_research.server

state-mcp:
	python -m boardroom.mcp_servers.boardroom_state.server


# ---------------------- Cleanup ----------------------

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	rm -rf .ruff_cache
