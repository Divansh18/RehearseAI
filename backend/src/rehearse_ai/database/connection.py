"""Opening the database: one place that decides how engines and session factories are made."""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def open_database(
    database_url: str, *, pool_pre_ping: bool = False
) -> tuple[AsyncEngine, async_sessionmaker]:
    """An engine and a session factory whose objects stay readable after commit."""
    engine = create_async_engine(database_url, pool_pre_ping=pool_pre_ping)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
