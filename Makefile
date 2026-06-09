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
    db-reset                   Drop and recreate the labfoundry schema (destructive)
    psql                       Open a psql shell in the postgres container
    bootstrap                  Seed the company and generate exploration kickoff
    harness                    Run the autonomous loop (foreground; Ctrl-C to stop)
    api                        Run the FastAPI command center on :$$API_PORT (default 8503)
    web                        Run Next.js dev server on :$$WEB_PORT (default 3000)
    dev                        Run api + web together (Ctrl-C stops both)
    research-mcp               Run the labfoundry-research MCP server (stdio)
    state-mcp                  Run the labfoundry-state MCP server (stdio)
    pre-commit                 Install pre-commit hooks
    check                      Lint + format check (ruff; no fixes — CI parity)
    ruff                       Autofix imports + format (ruff)
    test / tests               Run the test suite
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
PYENV_NAME      = labfoundry

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

.PHONY: check ruff test tests test-last-fail

# Non-mutating lint + format check — mirrors the CI `lint` job. Use `make ruff` to autofix.
check:
	ruff check .
	ruff format --check .

ruff:
	ruff check --select I --fix
	ruff format

# ---------------------- Tests ----------------------
#
# The DB-backed tests must RUN, not skip — but they TRUNCATE core tables, and
# pointing them at the live :5432 corpus would wipe ~57k documents. So `make
# test` spins up a DISPOSABLE pgvector container, applies the migrations, points
# DATABASE_URL at it, runs the suite, and always tears it down. The live DB is
# never touched.
#
# It also strips every .env-exported var for the pytest run so local matches CI
# (which has no .env). Without this, LIBRARY_SCOUTS would fire real network
# scouts in the collector tests (the "22 != 2" failures), and other loop/pace
# flags would skew agent defaults.
TEST_DB_CONTAINER ?= labfoundry-test-db
TEST_DB_PORT      ?= 5434
TEST_DATABASE_URL  = postgresql://labfoundry:labfoundry@localhost:$(TEST_DB_PORT)/labfoundry
# Every KEY defined in .env, rendered as `-u KEY` so `env` drops them for pytest.
ENV_KEYS := $(shell test -f .env && awk -F= '/^[A-Za-z_]/{print "-u "$$1}' .env | tr '\n' ' ')

# Spin a disposable migrated DB, run pytest ($(1) = extra args) against it in a
# CI-clean env, and ALWAYS tear it down (trap on EXIT). Never touches :5432.
define RUN_TESTS
	set -e; \
	echo ">> disposable test DB '$(TEST_DB_CONTAINER)' on :$(TEST_DB_PORT) (never the live :5432)"; \
	docker rm -f $(TEST_DB_CONTAINER) >/dev/null 2>&1 || true; \
	docker run -d --name $(TEST_DB_CONTAINER) \
		-e POSTGRES_USER=labfoundry -e POSTGRES_PASSWORD=labfoundry -e POSTGRES_DB=labfoundry \
		-p $(TEST_DB_PORT):5432 pgvector/pgvector:pg16 >/dev/null; \
	trap 'docker rm -f $(TEST_DB_CONTAINER) >/dev/null 2>&1 || true' EXIT; \
	for i in $$(seq 1 60); do \
		if docker exec $(TEST_DB_CONTAINER) pg_isready -U labfoundry -d labfoundry >/dev/null 2>&1; then break; fi; \
		sleep 1; \
	done; \
	for f in migrations/*.sql; do \
		docker exec -i $(TEST_DB_CONTAINER) psql -q -U labfoundry -d labfoundry < $$f >/dev/null \
			|| { echo "migration $$f failed"; exit 1; }; \
	done; \
	echo ">> pytest (clean env: .env vars stripped; disposable DB)"; \
	env $(ENV_KEYS) DATABASE_URL=$(TEST_DATABASE_URL) LABFOUNDRY_ALLOW_DB_WIPE=1 $(VENV_PYTHON) -m pytest $(1)
endef

test tests:
	@$(call RUN_TESTS,)

test-last-fail:
	@$(call RUN_TESTS,--lf)


# ---------------------- Infrastructure ----------------------

infra:
	docker compose up -d
	@echo ""
	@echo "  Postgres : localhost:5432  (labfoundry/boardroom)"
	@echo "  Langfuse : http://localhost:3000"
	@echo "  SearXNG  : http://localhost:8080 (if port free)"

infra-down:
	docker compose down

infra-logs:
	docker compose logs -f --tail=50

PG_CONTAINER ?= labfoundry-postgres-1

# Run migrations against the local Postgres via docker exec, so no host psql needed.
migrate:
	@for f in migrations/*.sql; do \
		echo ">> applying $$f"; \
		docker exec -i $(PG_CONTAINER) psql -U labfoundry -d labfoundry < $$f \
			|| { echo "migration $$f failed"; exit 1; }; \
	done

# Drop and recreate the public schema in the labfoundry DB. Destructive — wipes
# all data and types. Avoids "DROP DATABASE inside transaction" psql quirk.
db-reset:
	docker exec -i $(PG_CONTAINER) psql -U labfoundry -d labfoundry -c \
		"DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public AUTHORIZATION labfoundry; GRANT ALL ON SCHEMA public TO labfoundry;"
	@$(MAKE) migrate

# Open a psql shell inside the postgres container.
psql:
	docker exec -it $(PG_CONTAINER) psql -U labfoundry -d labfoundry


# ---------------------- LabFoundry commands ----------------------

bootstrap:
	python -m ops.bootstrap

harness:
	python -m harness.main

# Drive ONE Mimir cycle on demand against the live stack (preflight -> ingest ->
# report). The fast way to fully test the Library without booting the full loop.
mimir-firstlight:
	python -m ops.mimir_firstlight $(ARGS)

# Bulk-seed the Library from the rag-bench arXiv corpus (~21.8k papers, ~2h).
# Resumable: safe to Ctrl-C and re-run. ARGS="--limit 100" for a pilot.
seed-corpus:
	python -m ops.seed_corpus $(ARGS)

api:
	$(VENV_PYTHON) -m uvicorn api.main:app --host 0.0.0.0 --port $(API_PORT) --reload

.PHONY: web web-install dev api harness bootstrap mimir-firstlight seed-corpus

web-install:
	cd web && $(WEB_ENV) npm install

web:
	cd web && $(WEB_ENV) PORT=$(WEB_PORT) npm run dev

dev:
	@trap 'kill 0' INT TERM EXIT; \
	$(VENV_PYTHON) -m uvicorn api.main:app --host 0.0.0.0 --port $(API_PORT) --reload & \
	(cd web && $(WEB_ENV) PORT=$(WEB_PORT) npm run dev) & \
	wait

research-mcp:
	python -m agents.researcher.server

state-mcp:
	python -m state.server


# ---------------------- Cleanup ----------------------

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	rm -rf .ruff_cache
