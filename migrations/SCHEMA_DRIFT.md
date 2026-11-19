# Known differences between the models and the migrations

`alembic check` (autogenerate comparison of `rehearse_ai.models` against a database migrated to
0007) reports **13 differences**. All are indexes. None is a column, type, nullability, foreign-key
or table difference. No migration has been generated or applied for them, and none is needed for the
product to behave correctly. `tests/test_schema_drift.py` pins this exact list: it fails if a
difference is added, removed or changes kind, so the set cannot grow unnoticed.

## A. Naming drift only (5 pairs, 10 entries) — same table, same columns, different index name

The migrations named these indexes by hand; the models declare `index=True`, which SQLAlchemy names
`ix_<table>_<column>`. The index exists once in the database with the migration's name.

| Table | Migration name (in the database) | Name the model would give |
| --- | --- | --- |
| `candidate_interview_session` | `ix_candidate_interview_session_assignment` | `ix_candidate_interview_session_assignment_id` |
| `interview_assignment` | `ix_assignment_email` | `ix_interview_assignment_candidate_email` |
| `interview_assignment_audit` | `ix_assignment_audit_assignment` | `ix_interview_assignment_audit_assignment_id` |
| `interview_blueprint_version` | `ix_blueprint_version_blueprint` | `ix_interview_blueprint_version_blueprint_id` |
| `interview_job` | `ix_job_assignment` | `ix_interview_job_assignment_id` |

Effect: none at runtime. Consequence of "fixing" it blindly: a generated migration would drop and
recreate five working indexes for no behavioural gain. The safe fix, when wanted, is to put the
migration's name on the model (`Index("ix_assignment_email", ...)` instead of `index=True`), which
changes no database.

## B. Real differences: indexes that exist in the database but not in the models (3 entries)

| Table | Index | Created by | What it does |
| --- | --- | --- | --- |
| `interview_job` | `uq_job_active` — **unique, partial** (`assignment_id, kind` where `status in ('queued','running')`) | 0006 | Guarantees at most one live job per assignment and kind. `scheduling/assignments.py` relies on the resulting `IntegrityError` to answer a duplicate revise/retry request. |
| `interview_job` | `ix_job_claim` (`status, run_after`) | 0006 | Serves the job runner's claim query. |
| `interview_record` | `ix_interview_record_created_at` (`created_at`) | 0003 | Serves record listing order. |

These are *model* omissions, not database defects: the database is right and the model is
incomplete. The practical risk is that a database built from the models alone (`create_all`) would
lack the uniqueness guarantee in the first row. The project never builds a database that way (tests
and setup use `alembic upgrade head`), but it is why this is not purely cosmetic. Remedy, when
wanted: declare the three indexes in `models.py` (`__table_args__`, with `postgresql_where` for the
partial one). That changes no database either.

## C. Not reported by `alembic check`, noted for completeness

`alembic check` does not compare server defaults or CHECK constraints. Comparing server defaults
as well shows ten more differences (e.g. `candidate.is_active`, `interview_job.attempts`,
`interview_record.pdf_status`): the migrations set a `server_default` that the models express as a
Python-side `default`. Behaviour through the application is identical; a row inserted by raw SQL
would get the database default.

## Reproducing

```
createdb … rehearse_drift_scratch                      # a throwaway database, never the real one
DATABASE_URL=postgresql+asyncpg://…/rehearse_drift_scratch uv run alembic upgrade head
DATABASE_URL=… uv run alembic check
```
