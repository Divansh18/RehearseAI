"""Administrator access to conversation records: listing, transcript and the Conversation PDF.

Candidates have no route here: the Conversation PDF and the interview image are admin-only.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from rehearse_ai.records import queries, service
from rehearse_ai.server.dependencies import Ctx, Db, require_admin
from rehearse_ai.server.pages import render_page, sign_in_redirect

router = APIRouter()


async def _admin_record(ctx: Ctx, request: Request, db: Db, record_id: uuid.UUID):
    user = await require_admin(ctx, request, db)
    return await queries.accessible_record(db, record_id, user, admin=True)


@router.get("/admin/interviews")
async def records_page(ctx: Ctx, request: Request, db: Db) -> Response:
    user = await ctx.accounts.current_user(request, db)
    if not user:
        return sign_in_redirect(ctx, request)
    if not user.is_superuser:
        raise HTTPException(403, "Administrator access required")
    return render_page(ctx, "records.html")


@router.get("/admin/interviews/{record_id}")
async def record_page(record_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> Response:
    user = await ctx.accounts.current_user(request, db)
    if not user:
        return sign_in_redirect(ctx, request)
    if not user.is_superuser:
        raise HTTPException(403, "Administrator access required")
    await queries.accessible_record(db, record_id, user, admin=True)
    return render_page(ctx, "record_detail.html")


@router.get("/api/admin/interviews")
async def list_records(
    ctx: Ctx,
    request: Request,
    db: Db,
    page_number: int = Query(1, ge=1),
    candidate_email: str | None = Query(None, max_length=320),
    status: str | None = None,
) -> dict:
    await require_admin(ctx, request, db)
    return await queries.list_records(
        db, candidate_email=candidate_email, status=status, page_number=page_number, admin=True
    )


@router.get("/api/admin/interviews/{record_id}")
async def record_detail(record_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    record = await _admin_record(ctx, request, db, record_id)
    return await queries.record_with_address(db, record)


@router.get("/api/admin/interviews/{record_id}/transcript")
async def record_transcript(record_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> list:
    await _admin_record(ctx, request, db, record_id)
    return await queries.transcript_rows(db, record_id)


async def _pdf_response(ctx: Ctx, record) -> Response:
    path = await service.pdf_for_download(ctx.store, record)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"rehearseai-{record.id}.pdf",
        headers={"Cache-Control": "private, no-store"},
    )


# The first path is the original one, kept for compatibility with older admin pages.
@router.get("/api/interviews/{record_id}/pdf")
@router.get("/api/admin/interviews/{record_id}/pdf")
async def download_pdf(record_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> Response:
    return await _pdf_response(ctx, await _admin_record(ctx, request, db, record_id))


@router.post("/api/interviews/{record_id}/pdf/regenerate", status_code=202)
@router.post("/api/admin/interviews/{record_id}/pdf/regenerate", status_code=202)
async def regenerate_pdf(record_id: uuid.UUID, ctx: Ctx, request: Request, db: Db) -> dict:
    user = await require_admin(ctx, request, db)
    ctx.accounts.check_csrf(request)  # before the lookup, as before: a bad token is 403, not 404
    record = await queries.accessible_record(db, record_id, user, admin=True)
    await service.request_pdf_regeneration(ctx.store, record.id)
    return {"message": "PDF regeneration queued"}
