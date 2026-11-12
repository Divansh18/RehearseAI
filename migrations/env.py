"""Run explicit account migrations against the selected PostgreSQL database."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from rehearse_ai.accounts_db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def database_url() -> str:
    values = dotenv_values(".env")
    url = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("Set DATABASE_URL to the PostgreSQL asyncpg URL before migrating")
    return url


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: context.configure(
                connection=sync_connection, target_metadata=target_metadata
            )
        )
        await connection.run_sync(lambda _: context.run_migrations())
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
