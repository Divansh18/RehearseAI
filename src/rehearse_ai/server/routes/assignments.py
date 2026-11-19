"""Administrator scheduling and the assignments board."""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from rehearse_ai.plans.preparation import MAX_REVISION_REQUEST
from rehearse_ai.scheduling import assignments
from rehearse_ai.scheduling.documents import MAX_UPLOAD_BYTES as MAX_DOCUMENT_BYTES
from rehearse_ai.scheduling.service import (
    IdempotencyConflict,
    InvitationUnavailable,
    ScheduleRejected,
    Upload,
    parse_fields,
    schedule_interview,
    send_invitation,
)
from rehearse_ai.server.dependencies import Ctx, Db, require_admin
from rehearse_ai.server.pages import render_page, sign_in_redirect

router = APIRouter()

MAX_FORM_BYTES = 2 * MAX_DOCUMENT_BYTES + 1_500_000
SCHEDULE_FIELDS = (
    "candidate_name",
    "candidate_email",
    "interviewer_instructions",
    "job_description_text",
    "scheduled_local",
    "timezone",
    "target_minutes",
    "join_window_minutes",
    "idempotency_key",
)


class RevisionPayload(BaseModel):
    request: str = Field(min_length=3, max_length=MAX_REVISION_REQUEST)


async def read_schedule_form(request: Request):
    """Read the multipart body with a hard byte cap, then parse it."""
    if request.headers.get("content-type", "").split(";", 1)[0] != "multipart/form-data":
        raise HTTPException(415, "Submit the schedule form as multipart form data")
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > MAX_FORM_BYTES:
        raise HTTPException(413, "The upload is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(413, "The upload is too large")
    sent = False

    async def replay() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    return await Request(request.scope, replay).form(max_files=2, max_fields=20)


async def upload_of(form, name: str) -> Upload | None:
    item = form.get(name)
    if item is None or isinstance(item, str) or not getattr(item, "filename", ""):
        return None
    data = await item.read(MAX_DOCUMENT_BYTES + 1)
    return Upload(item.filename, data)


@router.get("/admin/assignments")
async def assignments_page(ctx: Ctx, request: Request, db: Db) -> Response:
    await require_admin(ctx, request, db)
    return render_page(ctx, "assignments.html")


@router.get("/admin/schedule")
async def schedule_page(ctx: Ctx, request: Request, db: Db) -> Response:
    user = await ctx.accounts.current_user(request, db)
    if not user:
        return sign_in_redirect(ctx, request)
    if not user.is_superuser:
        raise HTTPException(403, "Administrator access required")
    return render_page(ctx, "schedule.html")


# Compatibility: the route and the JSON key keep their original "blueprint" names.
@router.get("/api/admin/blueprint-versions/{version_id}")
async def plan_version_detail(version_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    await require_admin(ctx, request, db)
    return await assignments.plan_version_detail(db, version_id)


@router.get("/api/admin/assignments")
async def list_assignments(ctx: Ctx, request: Request, db: Db) -> list[dict]:
    await require_admin(ctx, request, db)
    return await assignments.list_assignments(db)


@router.get("/api/admin/assignments/{assignment_id}")
async def assignment_detail(assignment_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    await require_admin(ctx, request, db)
    return await assignments.assignment_detail(db, assignment_id)


@router.post("/api/admin/schedule")
async def schedule(ctx: Ctx, request: Request, db: Db) -> Response:
    admin = await require_admin(ctx, request, db)
    ctx.accounts.check_csrf(request)
    await ctx.accounts.rate_limit(db, "schedule", str(admin.id), limit=60, seconds=3600)
    form = await read_schedule_form(request)
    raw = {name: form.get(name) for name in SCHEDULE_FIELDS if isinstance(form.get(name), str)}
    try:
        fields = parse_fields(raw)
        result = await schedule_interview(
            ctx.db_sessions,
            ctx.settings.document_dir,
            admin_id=admin.id,
            fields=fields,
            resume=await upload_of(form, "resume"),
            job_file=await upload_of(form, "job_description_file"),
        )
    except ScheduleRejected as exc:
        return JSONResponse({"detail": exc.errors}, status_code=422)
    except IdempotencyConflict:
        return JSONResponse(
            {
                "detail": [
                    {
                        "field": "form",
                        "message": "This form was already submitted with different content. "
                        "Reload the page to schedule another interview.",
                    }
                ]
            },
            status_code=409,
        )
    if result.created:
        ctx.jobs.wake()
    return JSONResponse(
        {"assignment_id": str(result.assignment_id), "created": result.created},
        status_code=201 if result.created else 200,
    )


@router.post("/api/admin/assignments/{assignment_id}/revise", status_code=202)
async def revise_plan(
    assignment_id: uuid.UUID, payload: RevisionPayload, ctx: Ctx, request: Request, db: Db
) -> dict:
    admin = await require_admin(ctx, request, db)
    ctx.accounts.check_csrf(request)
    await ctx.accounts.rate_limit(db, "plan-revise", str(assignment_id), limit=10, seconds=3600)
    await assignments.request_plan_revision(
        ctx.db_sessions, assignment_id, admin.id, payload.request
    )
    ctx.jobs.wake()
    return {"status": "queued"}


@router.post("/api/admin/assignments/{assignment_id}/retry-preparation", status_code=202)
async def retry_preparation(assignment_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    admin = await require_admin(ctx, request, db)
    ctx.accounts.check_csrf(request)
    await assignments.retry_preparation(ctx.db_sessions, assignment_id, admin.id)
    ctx.jobs.wake()
    return {"status": "queued"}


@router.post("/api/admin/assignments/{assignment_id}/cancel")
async def cancel_assignment(assignment_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    admin = await require_admin(ctx, request, db)
    ctx.accounts.check_csrf(request)
    await assignments.cancel_assignment(ctx.db_sessions, assignment_id, admin.id)
    return {"status": "cancelled"}


@router.post("/api/admin/assignments/{assignment_id}/resend")
async def resend_assignment(assignment_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    admin = await require_admin(ctx, request, db)
    ctx.accounts.check_csrf(request)
    await ctx.accounts.rate_limit(db, "invite-resend", str(assignment_id), limit=3, seconds=3600)
    try:
        status = await send_invitation(
            ctx.db_sessions,
            ctx.accounts.mailer,
            assignment_id,
            actor_id=admin.id,
            action="invite_resent",
        )
    except InvitationUnavailable:
        raise HTTPException(409, "Only an unstarted assignment can be invited") from None
    return {"mail_status": status}
