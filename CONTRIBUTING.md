# Contributing

Read [ARCHITECTURE.md](ARCHITECTURE.md) first: it says where a change belongs and which packages may
depend on which. Product behaviour, setup and the privacy limits are in [README.md](README.md).

## Setup

```sh
docker compose up -d --wait            # PostgreSQL (5433) and Mailpit (1025 / 8025)
uv sync --frozen --group dev
test -f .env || cp .env.example .env   # then fill it in; never commit it
uv run alembic upgrade head
```

Keep development and test databases separate. Tests need `REHEARSE_TEST_DATABASE_URL` ending in
`_test` (Compose creates `rehearse_test`) with the migrations applied to it:

```sh
export REHEARSE_TEST_DATABASE_URL=postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test
DATABASE_URL=$REHEARSE_TEST_DATABASE_URL uv run alembic upgrade head
```

Tests use invented data only. Never point them at a real database or real candidate records, and
never send real documents or recordings to a provider while testing.

## Verify a change

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                                   # run serially: some tests share one database
node --test tests/frontend/browser_ui.test.mjs
for f in $(find src/rehearse_ai/web/scripts -name '*.js'); do node --check "$f"; done
uv lock --check && uv build
```

Run the suite **serially**. Several tests start applications against the one test database, and an
application start reconciles in-progress attempts, so parallel runs can fail each other.

Optional, outside pytest: `evaluation/frontend_smoke.py` drives every page in Chromium against a
throwaway server and fails on console errors (needs Playwright, which is not a project dependency);
the other scripts in `evaluation/` call real providers and use your keys.

Synthetic tests do not establish microphone recognition, browser playout, interview naturalness
or external email delivery. Do not claim those from them; use
[MANUAL_VOICE_TEST.md](MANUAL_VOICE_TEST.md).

## Naming

Use the vocabulary in ARCHITECTURE.md consistently:

* **interview plan** (questions and guidance), **assignment** (a scheduled interview),
  **attempt** (one execution), **candidate session** (invitation access), **conversation record**
  (what an attempt persisted);
* say *candidate session*, *voice session* or *database session*, never just "session";
* name modules for what they own (`conversation_pdf`, `speech_output`), not for a layer
  (`utils`, `helpers`, `manager`). Do not add a `utils` or `common` module.

**Do not rename a contract.** Table and column names, migration contents, route paths, JSON keys,
event names and stored enum-like values (`utterance_kind`, `status`) are compatibility surface. Change
the Python name and keep the stored name explicit, as `models.py` does for `plan_version_id`.

## Where code goes

* Rules belong in the capability package; a route handler authenticates, validates the request
  shape, calls one rule and shapes the response.
* `interview/` must stay free of FastAPI, Pipecat, SQLAlchemy and vendor SDKs. Pass what it needs as
  callables or plain data. `tests/test_architecture.py` enforces the allowed imports; to allow a new
  one, change that table and ARCHITECTURE.md in the same commit and say why.
* A task that outlives a call must have a single owner that cancels it. In the interview that is
  `InterviewSession`; invalidate stale work with its generation counter instead of ad hoc flags.
* Transactions that change an assignment or attempt lock the row first (`with_for_update`) and check
  its status in the same transaction.
* Add a table or column only with an additive Alembic migration; never edit a shipped migration and
  never write a destructive one.

## Commits

Small and coherent. Keep mechanical moves separate from behaviour changes, so each is reviewable. A
move should not change a line of logic. Do not push or deploy without being asked.
