# Contributing

Read [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) first: it says where a change belongs and which packages may
depend on which. Product behaviour, setup and the privacy limits are in [README.md](README.md).

## Setup

Everything runs from the **repository root**. `.env` and the private storage directories
(`sessions/`, `private_documents/`, `private_pdfs/`, `private_snapshots/`) are resolved relative to
the working directory, so starting elsewhere would read a different `.env` and write to different
folders. The Python project is in `backend/`; commands select it with `uv --project backend`, which
does not change the working directory. `make` lists the same commands (`make setup`, `make run`, ...).

```sh
docker compose up -d --wait            # PostgreSQL (5433) and Mailpit (1025 / 8025)
uv sync --frozen --project backend --group dev
test -f .env || cp .env.example .env   # then fill it in; never commit it
uv run --project backend alembic -c backend/alembic.ini upgrade head     # make migrate
uv run --project backend python -m rehearse_ai                           # make run
```

Keep development and test databases separate. Tests need `REHEARSE_TEST_DATABASE_URL` ending in
`_test` (Compose creates `rehearse_test`) with the migrations applied to it (`make migrate-test`):

```sh
export REHEARSE_TEST_DATABASE_URL=postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test
DATABASE_URL=$REHEARSE_TEST_DATABASE_URL uv run --project backend alembic -c backend/alembic.ini upgrade head
```

Tests use invented data only. Never point them at a real database or real candidate records, and
never send real documents or recordings to a provider while testing.

## Verify a change

```sh
make lint            # ruff check and format --check for backend/ and evaluation/
make test            # uv run --project backend pytest backend/tests; run serially (shared test database)
make test-frontend   # node tests of the browser scripts, then node --check on each script
make check           # uv lock --check --project backend
make build           # sdist, then a wheel built from it, into dist/
```

Run the backend suite **serially**. Several tests start applications against the one test database,
and an application start reconciles in-progress attempts, so parallel runs can fail each other.

After `make build`, check what a wheel contains (`unzip -l dist/*.whl`): the pages, scripts and
styles under `rehearse_ai/web/`, the prompts, and the PDF font with its licence.

Optional, outside pytest: `make smoke` (`evaluation/frontend_smoke.py`) drives every page in
Chromium against a throwaway server and fails on console errors (needs Playwright, which is not a
project dependency; `SMOKE_PYTHON=<python>` runs it against another environment, such as one with the
built wheel installed); the other scripts in `evaluation/` call real providers and use your keys, e.g.
`uv run --project backend python evaluation/plan_preparation_check.py`.

Synthetic tests do not establish microphone recognition, browser playout, interview naturalness
or external email delivery. Do not claim those from them; use
[MANUAL_VOICE_TEST.md](docs/testing/MANUAL_VOICE_TEST.md).

## Naming

Use the vocabulary in [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) consistently:

* **interview plan** (questions and guidance), **assignment** (a scheduled interview),
  **attempt** (one execution), **candidate session** (invitation access), **conversation record**
  (what an attempt persisted);
* say *candidate session*, *voice session* or *database session*, never just "session";
* name modules for what they own (`conversation_pdf`, `speech_output`), not for a layer
  (`utils`, `helpers`, `manager`). Do not add a `utils` or `common` module.

**Do not rename a contract.** Table and column names, migration contents, route paths, JSON keys,
event names and stored enum-like values (`utterance_kind`, `status`) are compatibility surface. Change
the Python name and keep the stored name explicit, as `database/models.py` does for `plan_version_id`.

## Where code goes

* Rules belong in the capability package; a route handler authenticates, validates the request
  shape, calls one rule and shapes the response.
* `interview/` must stay free of FastAPI, Pipecat, SQLAlchemy and vendor SDKs. Pass what it needs as
  callables or plain data. `backend/tests/test_architecture.py` enforces the allowed imports; to allow a new
  one, change that table and ARCHITECTURE.md in the same commit and say why.
* A task that outlives a call must have a single owner that cancels it. In the interview that is
  `InterviewSession`; invalidate stale work with its generation counter instead of ad hoc flags.
* Transactions that change an assignment or attempt lock the row first (`with_for_update`) and check
  its status in the same transaction.
* Add a table or column only with an additive Alembic migration; never edit a shipped migration and
  never write a destructive one. Then update `database/models.py` to match: `backend/tests/test_schema_drift.py` fails
  if the models and migrations disagree (see [MODELS_AND_MIGRATIONS.md](docs/architecture/MODELS_AND_MIGRATIONS.md)).

## Logging

Log through `rehearse_ai.operational_log.event(LOGGER, "category.what_happened", ...)`, with stable
ids, counts, durations and fixed codes only: never a name, email, token, document or anything said
or generated. Pass `exc_info=True` for failures (the exception class and location are logged, not
its message). Do not add `print`, a second handler or another logging library. Operational logs,
the protected `sessions/` files and the audit table are different records; see
[LOGGING.md](docs/architecture/LOGGING.md) before adding to any of them.

## Where documents go

`README.md` and this file are the entry points. Everything longer lives in `docs/`: `architecture/`
(structure and ownership), `product/` (behaviour and prompts), `testing/` (manual checklists and
dated evaluation reports) and `historical/` (the earlier prototype and its evidence). Update the
document that owns a fact rather than repeating it, and keep dated measurements dated.

## Commits

Small and coherent. Keep mechanical moves separate from behaviour changes, so each is reviewable. A
move should not change a line of logic. Do not push or deploy without being asked.
