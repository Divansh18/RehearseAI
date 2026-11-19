"""The ORM metadata and the migrations describe the same schema.

The migrations are authoritative. Two checks keep the models honest:

* Alembic's comparison of the models against the migrated test database (types and server
  defaults included) finds nothing.
* A database built from the models alone has the same indexes (names, uniqueness, partial
  predicates), column defaults and constraints as the migrated one. This covers what Alembic's
  comparison does not look at: CHECK constraints and unique-constraint names.

The only intentional difference is Alembic's own ``alembic_version`` table, which the models do
not describe. The test database is migrated to head (see CONTRIBUTING.md).
"""

import asyncio
import uuid

import asyncpg
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from support import TEST_URL

from rehearse_ai.database.models import Base

CATALOGUE = {
    "indexes": (
        "select tablename, indexname, indexdef from pg_indexes "
        "where schemaname = 'public' and tablename <> 'alembic_version' order by 1, 2"
    ),
    "columns": (
        "select table_name, column_name, column_default, is_nullable, data_type "
        "from information_schema.columns "
        "where table_schema = 'public' and table_name <> 'alembic_version' order by 1, 2"
    ),
    "constraints": (
        "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) from pg_constraint "
        "where connamespace = 'public'::regnamespace "
        "and conrelid::regclass::text <> 'alembic_version' order by 1, 2"
    ),
}


def test_alembic_finds_no_difference_between_models_and_migrations():
    async def collect():
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.connect() as connection:

                def compare(sync_connection):
                    context = MigrationContext.configure(
                        sync_connection,
                        opts={"compare_type": True, "compare_server_default": True},
                    )
                    return compare_metadata(context, Base.metadata)

                return await connection.run_sync(compare)
        finally:
            await engine.dispose()

    assert asyncio.run(collect()) == []


def test_a_database_built_from_the_models_matches_the_migrated_one():
    url = make_url(TEST_URL)
    scratch = f"rehearse_models_only_{uuid.uuid4().hex[:8]}_test"
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )

    async def snapshot(database: str) -> dict[str, list[tuple]]:
        engine = create_async_engine(url.set(database=database))
        try:
            async with engine.connect() as connection:
                return {
                    name: [tuple(row) for row in await connection.execute(text(query))]
                    for name, query in CATALOGUE.items()
                }
        finally:
            await engine.dispose()

    async def build_from_models() -> dict[str, list[tuple]]:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'CREATE DATABASE "{scratch}"')
            engine = create_async_engine(url.set(database=scratch))
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            finally:
                await engine.dispose()
            return await snapshot(scratch)
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
            await admin.close()

    async def both():
        return await snapshot(url.database), await build_from_models()

    migrated, from_models = asyncio.run(both())
    for name in CATALOGUE:
        assert from_models[name] == migrated[name], name
