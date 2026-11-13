"""Explicit local promotion of an existing active account."""

import argparse
import asyncio
import os

from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehearse_ai.accounts_db import Candidate


async def promote(email: str, database_url: str) -> str:
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg URL")
    engine = create_async_engine(database_url)
    try:
        sessions = async_sessionmaker(engine)
        async with sessions.begin() as db:
            user = await db.scalar(
                select(Candidate).where(Candidate.email == email.strip().casefold())
            )
            if user is None:
                raise ValueError("Account not found")
            if not user.is_active:
                raise ValueError("Activate the account before promotion")
            user.is_superuser = True
            promoted_email = user.email
        return promoted_email
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote an active RehearseAI account")
    parser.add_argument("email")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL") or ""
    try:
        promoted = asyncio.run(promote(args.email, url))
    except ValueError as exc:
        parser.exit(1, f"Promotion failed: {exc}\n")
    print(f"Admin role granted to account {promoted}")


if __name__ == "__main__":
    main()
