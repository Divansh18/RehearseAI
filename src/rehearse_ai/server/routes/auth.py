"""Administrator authentication: sign-in, sign-out, password reset and the CSRF token."""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi_users import exceptions
from pydantic import BaseModel, EmailStr, Field

from rehearse_ai.accounts.service import MailDeliveryError
from rehearse_ai.server.dependencies import (
    Ctx,
    Db,
    client_address,
    rate_limit_address_and_identity,
)
from rehearse_ai.server.pages import render_page

router = APIRouter()


class EmailPayload(BaseModel):
    email: EmailStr


class PasswordPayload(EmailPayload):
    password: str = Field(min_length=1, max_length=256)


class TokenPayload(BaseModel):
    token: str = Field(min_length=16, max_length=4096)


class ResetPayload(TokenPayload):
    password: str = Field(min_length=1, max_length=256)


def normalise_email(value: str) -> str:
    return value.strip().casefold()


@router.get("/login")
@router.get("/reset-password")
async def account_page(ctx: Ctx) -> HTMLResponse:
    return render_page(ctx, "sign_in.html")


@router.get("/api/csrf")
async def csrf(ctx: Ctx) -> Response:
    return ctx.accounts.csrf_response()


@router.get("/api/auth/me")
async def me(ctx: Ctx, request: Request, db: Db) -> dict[str, str]:
    user = await ctx.accounts.require_user(request, db)
    if not user.is_superuser:
        raise HTTPException(403, "Administrator access required")
    return {"email": user.email, "id": str(user.id), "role": "admin"}


@router.post("/api/auth/signup", status_code=410)
async def sign_up() -> dict[str, str]:
    return {"message": "Candidate accounts are no longer offered"}


@router.post("/api/auth/login")
async def login(payload: PasswordPayload, ctx: Ctx, request: Request, db: Db) -> Response:
    ctx.accounts.check_csrf(request)
    normalized = normalise_email(payload.email)
    await rate_limit_address_and_identity(ctx, db, request, "login", normalized, count=10)
    return await ctx.accounts.login(db, normalized, payload.password)


@router.post("/api/auth/logout")
async def logout(ctx: Ctx, request: Request, db: Db) -> Response:
    ctx.accounts.check_csrf(request)
    account = await ctx.accounts.current_user(request, db)
    response = await ctx.accounts.logout(request, db)
    if account:
        await ctx.live.disconnect_owned_by(str(account.id))
    return response


@router.post("/api/auth/request-reset", status_code=202)
async def request_reset(payload: EmailPayload, ctx: Ctx, request: Request, db: Db) -> dict:
    ctx.accounts.check_csrf(request)
    normalized = normalise_email(payload.email)
    await rate_limit_address_and_identity(
        ctx, db, request, "reset-request", normalized, count=3, seconds=3600
    )
    manager = ctx.accounts.manager(db)
    try:
        user = await manager.get_by_email(normalized)
        await manager.forgot_password(user)
    except exceptions.UserNotExists:
        pass
    except MailDeliveryError as exc:
        raise HTTPException(503, "Reset email could not be sent. Try again later") from exc
    return {
        "message": "If this account exists, a reset link was sent to the configured email "
        "service. Local development captures mail in Mailpit."
    }


@router.post("/api/auth/reset-password")
async def reset(payload: ResetPayload, ctx: Ctx, request: Request, db: Db) -> dict[str, str]:
    ctx.accounts.check_csrf(request)
    await ctx.accounts.rate_limit(db, "reset:ip", client_address(request), limit=20, seconds=600)
    account_id = await ctx.accounts.reset_password(db, payload.token, payload.password)
    await ctx.live.disconnect_owned_by(str(account_id))
    return {"message": "Password changed. Sign in again on all devices."}
