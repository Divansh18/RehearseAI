"""Candidate access: private invitation, restricted candidate session and setup pages."""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from rehearse_ai.invitations.access import (
    SESSION_COOKIE,
    assignment_public,
    candidate_interview_view,
    candidate_session,
    confirm_self_reported_name,
    invitation_assignment,
    public_plan,
    redeem_invitation,
    revoke_candidate_session,
    set_candidate_cookie,
)
from rehearse_ai.server.dependencies import Ctx, Db, client_address
from rehearse_ai.server.pages import render_page

router = APIRouter()


class NamePayload(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)


@router.get("/join/{token}")
async def join_page(ctx: Ctx, token: str) -> Response:
    # Rendering the shell is read-only. Crawlers and email scanners never redeem.
    if len(token) < 32 or len(token) > 128:
        raise HTTPException(404)
    return render_page(ctx, "join.html")


@router.get("/api/invitations/{token}")
async def invitation_preview(token: str, db: Db) -> dict:
    assignment = await invitation_assignment(db, token)
    return assignment_public(assignment, await public_plan(db, assignment))


@router.post("/api/invitations/{token}/redeem")
async def redeem(token: str, ctx: Ctx, request: Request, db: Db) -> Response:
    ctx.accounts.check_csrf(request)
    await ctx.accounts.rate_limit(
        db, "invite-redeem", client_address(request), limit=12, seconds=600
    )
    redeemed = await redeem_invitation(ctx.db_sessions, token)
    response = JSONResponse({"next": "/candidate/welcome"})
    set_candidate_cookie(
        response,
        redeemed.token,
        secure=ctx.settings.auth.secure_cookie,
        expires=redeemed.expires,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/candidate/welcome")
async def welcome_page(ctx: Ctx, request: Request, db: Db) -> Response:
    await candidate_session(db, request)
    return render_page(ctx, "welcome.html")


@router.get("/interview")
async def interview_page(ctx: Ctx, request: Request, db: Db) -> Response:
    assignment = await candidate_session(db, request)
    if assignment.status != "assigned" or not assignment.self_reported_name:
        return RedirectResponse("/candidate/welcome", status_code=303)
    return render_page(ctx, "index.html")


@router.get("/api/candidate/interview")
async def candidate_interview(request: Request, db: Db) -> dict:
    assignment = await candidate_session(db, request)
    return candidate_interview_view(assignment, await public_plan(db, assignment))


@router.post("/api/candidate/name")
async def confirm_name(payload: NamePayload, ctx: Ctx, request: Request, db: Db) -> dict:
    ctx.accounts.check_csrf(request)
    assignment = await candidate_session(db, request)
    name = await confirm_self_reported_name(ctx.db_sessions, assignment.id, payload.full_name)
    return {"name": name, "identity_verified": False}


@router.post("/api/candidate/logout")
async def leave_invitation(ctx: Ctx, request: Request) -> Response:
    ctx.accounts.check_csrf(request)
    await revoke_candidate_session(ctx.db_sessions, request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
