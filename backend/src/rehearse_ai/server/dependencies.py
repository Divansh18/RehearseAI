"""Request dependencies shared by every router: context, database session, admin guard."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rehearse_ai.database.models import Account
from rehearse_ai.server.context import ServerContext


def get_context(request: Request) -> ServerContext:
    return request.app.state.context


Ctx = Annotated[ServerContext, Depends(get_context)]


async def get_db(ctx: Ctx):
    async with ctx.db_sessions() as db:
        yield db


Db = Annotated[AsyncSession, Depends(get_db)]


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def require_admin(ctx: ServerContext, request: Request, db: AsyncSession) -> Account:
    user = await ctx.accounts.require_user(request, db)
    if not user.is_superuser:
        raise HTTPException(403, "Administrator access required")
    return user


async def rate_limit_address_and_identity(
    ctx: ServerContext,
    db: AsyncSession,
    request: Request,
    action: str,
    identity: str,
    count: int = 5,
    seconds: int = 600,
) -> None:
    address = client_address(request)
    await ctx.accounts.rate_limit(db, action + ":ip", address, limit=count * 4, seconds=seconds)
    await ctx.accounts.rate_limit(db, action + ":email", identity, limit=count, seconds=seconds)
