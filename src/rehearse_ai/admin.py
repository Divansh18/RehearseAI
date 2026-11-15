"""Explicit administrator provisioning or promotion; no default credentials."""

import argparse
import asyncio
import getpass
import os

from dotenv import dotenv_values
from fastapi_users import exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehearse_ai.accounts import AccountService, CandidateCreate
from rehearse_ai.accounts_db import Candidate
from rehearse_ai.config import AuthConfig


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


async def create_admin(email: str, password: str, database_url: str, secret: str) -> str:
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg URL")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        service = AccountService(AuthConfig(database_url=database_url, secret=secret), sessions)
        async with sessions() as db:
            address = email.strip().casefold()
            if await db.scalar(select(Candidate.id).where(Candidate.email == address)):
                raise ValueError("Account already exists; use promotion instead")
            try:
                user = await service.manager(db).create(
                    CandidateCreate(email=address, password=password), safe=True
                )
            except exceptions.InvalidPasswordException as exc:
                raise ValueError(str(exc.reason)) from exc
            user.is_superuser = True
            await db.commit()
            return user.email
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision or promote a RehearseAI admin")
    parser.add_argument(
        "--create", action="store_true", help="Create a new admin; prompt for password"
    )
    parser.add_argument("email")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL") or ""
    try:
        if args.create:
            password = getpass.getpass("New admin password: ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords did not match")
            secret = (
                os.environ.get("REHEARSE_AUTH_SECRET")
                or dotenv_values(".env").get("REHEARSE_AUTH_SECRET")
                or ""
            )
            promoted = asyncio.run(create_admin(args.email, password, url, secret))
        else:
            promoted = asyncio.run(promote(args.email, url))
    except ValueError as exc:
        parser.exit(1, f"Promotion failed: {exc}\n")
    print(f"Admin role granted to account {promoted}")


if __name__ == "__main__":
    main()
