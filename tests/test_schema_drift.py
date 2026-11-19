"""The models and the migrations differ only in the documented ways (migrations/SCHEMA_DRIFT.md).

This pins the exact set rather than asserting "no drift", so that new drift fails loudly while the
known index differences stay documented instead of being fixed by a blind migration. It compares the
models against the test database, which is migrated to head (see CONTRIBUTING.md).
"""

import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine
from support import TEST_URL

from rehearse_ai.models import Base

# Same columns, different name: the migrations named these by hand.
RENAMED = {
    ("candidate_interview_session", "ix_candidate_interview_session_assignment"),
    ("interview_assignment", "ix_assignment_email"),
    ("interview_assignment_audit", "ix_assignment_audit_assignment"),
    ("interview_blueprint_version", "ix_blueprint_version_blueprint"),
    ("interview_job", "ix_job_assignment"),
}
# Present in the database (migrations 0003, 0006), not declared on the models.
UNDECLARED = {
    ("interview_job", "uq_job_active"),
    ("interview_job", "ix_job_claim"),
    ("interview_record", "ix_interview_record_created_at"),
}


def differences() -> list[tuple]:
    async def collect():
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.connect() as connection:

                def compare(sync_connection):
                    context = MigrationContext.configure(
                        sync_connection, opts={"compare_type": True}
                    )
                    return compare_metadata(context, Base.metadata)

                return await connection.run_sync(compare)
        finally:
            await engine.dispose()

    return asyncio.run(collect())


def test_only_the_documented_index_differences_exist():
    found = differences()
    assert all(entry[0] in {"add_index", "remove_index"} for entry in found), found
    removed = {
        (entry[1].table.name, entry[1].name) for entry in found if entry[0] == "remove_index"
    }
    added = {(entry[1].table.name, entry[1].name) for entry in found if entry[0] == "add_index"}
    assert removed == RENAMED | UNDECLARED
    # Each rename's replacement is the model-generated name on the same table; nothing else is new.
    assert {table for table, _ in added} == {table for table, _ in RENAMED}
    assert len(added) == len(RENAMED)
    assert len(found) == 13
