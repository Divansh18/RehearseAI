# A thin command list, not a build system. Run everything from the repository root: .env and the
# private storage directories (sessions/, private_*/) are resolved relative to the working directory.
# The Python project lives in backend/ and is selected with `uv --project backend`; there is no
# second project or lockfile.

UV      := uv run --project backend
# The isolated test database created by `docker compose` (never the development database).
TEST_DB ?= postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test
RUFF_EVAL := --config backend/pyproject.toml --config "src=['backend/src']"

.PHONY: setup migrate migrate-test run test test-frontend lint check build smoke

setup:  ## services, dependencies from the frozen lockfile, and .env if missing
	docker compose up -d --wait
	uv sync --frozen --project backend --group dev
	test -f .env || cp .env.example .env

migrate:  ## apply migrations to the development database (DATABASE_URL from .env)
	$(UV) alembic -c backend/alembic.ini upgrade head

migrate-test:  ## apply migrations to the isolated test database
	DATABASE_URL=$(TEST_DB) $(UV) alembic -c backend/alembic.ini upgrade head

run:  ## start the server (http://127.0.0.1:7860)
	$(UV) python -m rehearse_ai

test:  ## backend tests; serial, because they share one test database
	REHEARSE_TEST_DATABASE_URL=$(TEST_DB) $(UV) pytest backend/tests -q

test-frontend:  ## browser-script tests and JavaScript syntax
	node --test frontend/tests/browser_ui.test.mjs
	find frontend/scripts -name '*.js' -exec node --check {} \;

lint:  ## ruff lint and format check
	$(UV) ruff check backend
	$(UV) ruff format --check backend
	$(UV) ruff check $(RUFF_EVAL) evaluation
	$(UV) ruff format --check --config backend/pyproject.toml evaluation

check:  ## lockfile is current
	uv lock --check --project backend

build:  ## sdist and wheel (the wheel is built from the sdist) into dist/
	uv build backend --out-dir dist

smoke:  ## Chromium smoke test of every page (needs Playwright in the python3 used; see script)
	REHEARSE_TEST_DATABASE_URL=$(TEST_DB) python3 evaluation/frontend_smoke.py
