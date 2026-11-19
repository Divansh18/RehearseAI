# Models and migrations

The migrations in `migrations/versions/` are authoritative: they define what exists in a real
database, and the application never creates tables itself. `rehearse_ai/models.py` describes the
same schema so that queries are typed and Alembic can detect change.

The two **agree exactly**. For the migrated schema (0001–0007), the models declare every index with
its migration name, uniqueness and partial predicate (e.g. `uq_job_active`, unique on
`(assignment_id, kind)` where `status in ('queued','running')`), every server default, every CHECK
constraint with its name, and the named unique constraint on `interview_record.assignment_id`
(`uq_interview_assignment_attempt`). `alembic check` reports no operations.

## Intentional differences

Only one: Alembic's own `alembic_version` table is not in the models.

Python-side `default=` values remain beside the server defaults. They are not schema, and the
application relies on them when it builds rows in memory before flushing.

## How agreement is kept

`tests/test_schema_drift.py` fails if they diverge:

1. Alembic's comparison of the models against the migrated test database (types and server
   defaults included) must be empty.
2. A throwaway database built from the models alone must have the same indexes (definitions
   included), column defaults and constraints as the migrated one. This covers CHECK constraints
   and unique-constraint names, which Alembic's comparison does not examine.

## Changing the schema

Write a migration, then change the model to match; both checks above tell you if they disagree.
Never edit an applied migration. Never fix a difference by autogenerating a migration blindly:
the migrations here are hand-written and additive, and downgrades are deliberately refused.

## History

Before this alignment the models differed from the migrations in 13 indexes (five index-name pairs
and three indexes the models did not declare), ten server defaults, one unique-constraint name and
seven CHECK constraints. All were omissions or naming differences in the models; no database was
changed and no migration was added.
