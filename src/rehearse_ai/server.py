"""Candidate account routes and protected local WebRTC signaling."""

import asyncio
import hashlib
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi_users import exceptions
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rehearse_ai.accounts import AccountService, MailDeliveryError
from rehearse_ai.accounts_db import (
    AssignmentAudit,
    BlueprintVersion,
    Candidate,
    CandidateInterviewSession,
    InterviewAssignment,
    InterviewBlueprint,
    InterviewRecord,
    TranscriptEntry,
)
from rehearse_ai.assigned import (
    SESSION_COOKIE,
    BlueprintContent,
    assignment_public,
    candidate_session,
    invitation_assignment,
    invitation_token,
    now,
    session_expiry,
    set_candidate_cookie,
    token_digest,
)
from rehearse_ai.config import ConfigurationError, Settings
from rehearse_ai.interview_store import TERMINAL, InterviewStore
from rehearse_ai.pipeline import LiveSession
from rehearse_ai.snapshot import MAX_UPLOAD_BYTES, InvalidSnapshot, save_snapshot

WEB_DIR = Path(__file__).resolve().parent / "web"
PACKAGE_DIR = WEB_DIR.parent


def source_build_id() -> str:
    """Freeze a source fingerprint when the server starts, even before a commit."""
    digest = hashlib.sha256()
    for path in sorted(PACKAGE_DIR.rglob("*")):
        if path.suffix in {".py", ".txt", ".html", ".css", ".js"}:
            digest.update(str(path.relative_to(PACKAGE_DIR)).encode())
            digest.update(path.read_bytes())
    return f"{version('rehearse-ai')}+{digest.hexdigest()[:12]}"


def create_app(settings: Settings) -> FastAPI:
    build_id = source_build_id()
    engine = create_async_engine(settings.auth.database_url, pool_pre_ping=True)
    db_sessions = async_sessionmaker(engine, expire_on_commit=False)
    accounts = AccountService(settings.auth, db_sessions)
    prompt_digest = hashlib.sha256()
    for prompt in sorted((PACKAGE_DIR / "prompts").glob("*.txt")):
        prompt_digest.update(prompt.name.encode())
        prompt_digest.update(prompt.read_bytes())
    prompt_version = prompt_digest.hexdigest()[:16]
    handler = SmallWebRTCRequestHandler()
    sessions: set[LiveSession] = set()
    tasks: set[asyncio.Task[None]] = set()

    async def storage_failure(session_id: str) -> None:
        for live in tuple(sessions):
            if live.log.session_id == session_id:
                await live.connection.send_app_message(
                    {"type": "rehearseai", "event": {"event": "persistence_error"}}
                )

    store = InterviewStore(
        db_sessions, settings.session_dir, settings.pdf_dir, storage_failure, settings.snapshot_dir
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with db_sessions() as db:
                await db.execute(select(Candidate.id).limit(1))
                await db.execute(select(InterviewRecord.id).limit(1))
                await db.execute(select(InterviewBlueprint.id).limit(1))
        except SQLAlchemyError:
            raise RuntimeError(
                "Interview database is unavailable or unmigrated. "
                "Start PostgreSQL and run `uv run alembic upgrade head`."
            ) from None
        async with db_sessions.begin() as db:
            await db.execute(
                update(InterviewAssignment)
                .where(
                    InterviewAssignment.status == "assigned",
                    InterviewAssignment.latest_start_at < now(),
                )
                .values(status="expired", updated_at=now())
            )
        await store.start()
        yield
        for session in tuple(sessions):
            await session.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await store.stop()
        await handler.close()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.build_id = build_id
    app.state.accounts = accounts
    app.state.interview_store = store
    app.state.active_sessions = sessions

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return JSONResponse({"detail": fields}, status_code=422)

    async def get_db():
        async with db_sessions() as db:
            yield db

    Db = Annotated[AsyncSession, Depends(get_db)]

    def page(name: str) -> HTMLResponse:
        html = (WEB_DIR / name).read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__BUILD_ID__", build_id),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    def sign_in_redirect(request: Request) -> RedirectResponse:
        reason = "expired" if accounts.cookies.cookie_name in request.cookies else "required"
        return RedirectResponse(f"/login?reason={reason}", status_code=303)

    class EmailPayload(BaseModel):
        email: EmailStr

    class PasswordPayload(EmailPayload):
        password: str = Field(min_length=1, max_length=256)

    class TokenPayload(BaseModel):
        token: str = Field(min_length=16, max_length=4096)

    class ResetPayload(TokenPayload):
        password: str = Field(min_length=1, max_length=256)

    def email(value: str) -> str:
        return value.strip().casefold()

    async def limit(
        db: AsyncSession,
        request: Request,
        action: str,
        identity: str,
        count: int = 5,
        seconds: int = 600,
    ) -> None:
        address = request.client.host if request.client else "unknown"
        await accounts.rate_limit(db, action + ":ip", address, limit=count * 4, seconds=seconds)
        await accounts.rate_limit(db, action + ":email", identity, limit=count, seconds=seconds)

    @app.get("/assets/{name}")
    async def asset(name: str) -> Response:
        if name not in {
            "app.js",
            "media_setup.js",
            "styles.css",
            "account.js",
            "account.css",
            "history.js",
            "theme.css",
            "assigned.js",
            "assignments.js",
        }:
            raise HTTPException(404)
        path = WEB_DIR / name
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return Response(path.read_text(encoding="utf-8"), media_type=media)

    @app.get("/")
    async def home(request: Request, db: Db) -> RedirectResponse:
        user = await accounts.current_user(request, db)
        if user and user.is_superuser:
            return RedirectResponse("/admin/interviews", status_code=303)
        return sign_in_redirect(request)

    @app.get("/login")
    @app.get("/reset-password")
    async def account_page() -> HTMLResponse:
        return page("account.html")

    @app.get("/admin/interviews")
    async def admin_page(request: Request, db: Db) -> Response:
        user = await accounts.current_user(request, db)
        if not user:
            return sign_in_redirect(request)
        if not user.is_superuser:
            raise HTTPException(403, "Administrator access required")
        return page("admin.html")

    @app.get("/admin/interviews/{record_id}")
    async def admin_detail(record_id: uuid.UUID, request: Request, db: Db) -> Response:
        user = await accounts.current_user(request, db)
        if not user:
            return sign_in_redirect(request)
        if not user.is_superuser:
            raise HTTPException(403, "Administrator access required")
        await accessible_record(db, record_id, user, admin=True)
        return page("admin_interview_record.html")

    @app.get("/interview")
    async def interview(request: Request, db: Db) -> Response:
        assignment = await candidate_session(db, request)
        if assignment.status != "assigned" or not assignment.self_reported_name:
            return RedirectResponse("/candidate/welcome", status_code=303)
        return page("index.html")

    @app.get("/join/{token}")
    async def join_page(token: str) -> Response:
        # Rendering the shell is read-only. Crawlers and email scanners never redeem.
        if len(token) < 32 or len(token) > 128:
            raise HTTPException(404)
        return page("join.html")

    @app.get("/api/invitations/{token}")
    async def invitation_preview(token: str, db: Db) -> dict:
        assignment = await invitation_assignment(db, token)
        version = await db.get(BlueprintVersion, assignment.blueprint_version_id)
        return assignment_public(assignment, BlueprintContent.model_validate(version.content))

    @app.post("/api/invitations/{token}/redeem")
    async def redeem_invitation(token: str, request: Request, db: Db) -> Response:
        accounts.check_csrf(request)
        await accounts.rate_limit(
            db,
            "invite-redeem",
            request.client.host if request.client else "unknown",
            limit=12,
            seconds=600,
        )
        async with db_sessions.begin() as transaction:
            assignment = await transaction.scalar(
                select(InterviewAssignment)
                .where(InterviewAssignment.invitation_digest == token_digest(token))
                .with_for_update()
            )
            if (
                assignment is None
                or len(token) < 32
                or len(token) > 128
                or assignment.status != "assigned"
                or assignment.invitation_expires_at <= now()
            ):
                raise HTTPException(404, "Invitation unavailable")
            existing = await transaction.scalars(
                select(CandidateInterviewSession).where(
                    CandidateInterviewSession.assignment_id == assignment.id,
                    CandidateInterviewSession.revoked_at.is_(None),
                )
            )
            for prior in existing:
                prior.revoked_at = now()
            session_token = secrets.token_urlsafe(32)
            expires = session_expiry(assignment)
            transaction.add(
                CandidateInterviewSession(
                    digest=token_digest(session_token),
                    assignment_id=assignment.id,
                    expires_at=expires,
                )
            )
        response = JSONResponse({"next": "/candidate/welcome"})
        set_candidate_cookie(
            response, session_token, secure=settings.auth.secure_cookie, expires=expires
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/candidate/welcome")
    async def candidate_welcome(request: Request, db: Db) -> Response:
        await candidate_session(db, request)
        return page("welcome.html")

    @app.get("/api/candidate/interview")
    async def candidate_interview_info(request: Request, db: Db) -> dict:
        assignment = await candidate_session(db, request)
        version = await db.get(BlueprintVersion, assignment.blueprint_version_id)
        data = assignment_public(assignment, BlueprintContent.model_validate(version.content))
        instant = now()
        data["can_begin"] = (
            assignment.status == "assigned"
            and assignment.scheduled_start_at <= instant <= assignment.latest_start_at
        )
        data["window_state"] = (
            "too_early"
            if instant < assignment.scheduled_start_at
            else "closed"
            if instant > assignment.latest_start_at
            else "open"
        )
        return data

    class NamePayload(BaseModel):
        full_name: str = Field(min_length=2, max_length=120)

    @app.post("/api/candidate/name")
    async def confirm_candidate_name(payload: NamePayload, request: Request, db: Db) -> dict:
        accounts.check_csrf(request)
        assignment = await candidate_session(db, request)
        name = " ".join(payload.full_name.split())
        if len(name.split()) < 2 or any(char in name for char in "<>\r\n"):
            raise HTTPException(400, "Enter your full name")
        async with db_sessions.begin() as transaction:
            current = await transaction.get(
                InterviewAssignment, assignment.id, with_for_update=True
            )
            if current.status != "assigned":
                raise HTTPException(409, "Name cannot change after the interview starts")
            current.self_reported_name = name
            current.updated_at = now()
        return {"name": name, "identity_verified": False}

    @app.post("/api/candidate/logout")
    async def leave_invitation(request: Request, db: Db) -> Response:
        accounts.check_csrf(request)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            async with db_sessions.begin() as transaction:
                row = await transaction.get(CandidateInterviewSession, token_digest(token))
                if row:
                    row.revoked_at = now()
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/csrf")
    async def csrf() -> Response:
        return accounts.csrf_response()

    @app.get("/api/auth/me")
    async def me(request: Request, db: Db) -> dict[str, str]:
        user = await accounts.require_user(request, db)
        if not user.is_superuser:
            raise HTTPException(403, "Administrator access required")
        return {
            "email": user.email,
            "id": str(user.id),
            "role": "admin",
        }

    async def require_admin(request: Request, db: AsyncSession) -> Candidate:
        user = await accounts.require_user(request, db)
        if not user.is_superuser:
            raise HTTPException(403, "Administrator access required")
        return user

    @app.get("/admin/assignments")
    async def assignment_page(request: Request, db: Db) -> Response:
        await require_admin(request, db)
        return page("assignments.html")

    @app.get("/api/admin/blueprint-versions/{version_id}")
    async def blueprint_version_detail(version_id: uuid.UUID, request: Request, db: Db) -> dict:
        await require_admin(request, db)
        item = await db.get(BlueprintVersion, version_id)
        if item is None:
            raise HTTPException(404, "Plan version not found")
        return {"id": str(item.id), "version": item.version, "content": item.content}

    async def mail_assignment(assignment_id: uuid.UUID, *, actor_id: uuid.UUID, action: str) -> str:
        # Rotate the one-way digest on each send. A failed send is visible and retryable.
        token = invitation_token()
        async with db_sessions.begin() as transaction:
            assignment = await transaction.get(
                InterviewAssignment, assignment_id, with_for_update=True
            )
            if (
                assignment is None
                or assignment.status != "assigned"
                or assignment.latest_start_at <= now()
            ):
                raise HTTPException(409, "Only an unstarted assignment can be invited")
            assignment.invitation_digest = token_digest(token)
            assignment.invitation_expires_at = assignment.latest_start_at
            assignment.mail_status = "sending"
            assignment.updated_at = now()
            transaction.add(
                AssignmentAudit(
                    assignment_id=assignment.id,
                    actor_id=actor_id,
                    action=action,
                    occurred_at=now(),
                    detail={"mail_status": "sending"},
                )
            )
        async with db_sessions() as db:
            assignment = await db.get(InterviewAssignment, assignment_id)
            version = await db.get(BlueprintVersion, assignment.blueprint_version_id)
            content = BlueprintContent.model_validate(version.content)
        try:
            await accounts.mailer.send_invitation(
                assignment.candidate_email,
                token,
                title=content.title,
                scheduled_at=assignment.scheduled_start_at,
                latest_at=assignment.latest_start_at,
                timezone=assignment.timezone,
                target_minutes=content.target_minutes,
            )
            status = "sent"
        except MailDeliveryError:
            status = "failed"
        async with db_sessions.begin() as transaction:
            current = await transaction.get(
                InterviewAssignment, assignment_id, with_for_update=True
            )
            if current.invitation_digest == token_digest(token):
                current.mail_status = status
                transaction.add(
                    AssignmentAudit(
                        assignment_id=assignment_id,
                        actor_id=actor_id,
                        action="mail_result",
                        occurred_at=now(),
                        detail={"mail_status": status},
                    )
                )
        return status

    @app.get("/api/admin/assignments")
    async def list_assignments(request: Request, db: Db) -> list[dict]:
        await require_admin(request, db)
        rows = (
            await db.scalars(
                select(InterviewAssignment)
                .order_by(InterviewAssignment.created_at.desc())
                .limit(100)
            )
        ).all()
        return [
            {
                "id": str(item.id),
                "candidate_email": item.candidate_email,
                "candidate_name": item.candidate_name,
                "status": item.status,
                "mail_status": item.mail_status,
                "scheduled_start_at": item.scheduled_start_at.astimezone(
                    ZoneInfo(item.timezone)
                ).isoformat(),
                "latest_start_at": item.latest_start_at.astimezone(
                    ZoneInfo(item.timezone)
                ).isoformat(),
                "timezone": item.timezone,
            }
            for item in rows
        ]

    @app.get("/api/admin/assignments/{assignment_id}")
    async def assignment_detail(assignment_id: uuid.UUID, request: Request, db: Db) -> dict:
        await require_admin(request, db)
        item = await db.get(InterviewAssignment, assignment_id)
        if item is None:
            raise HTTPException(404, "Assignment not found")
        return {
            "id": str(item.id),
            "blueprint_version_id": str(item.blueprint_version_id),
            "candidate_email": item.candidate_email,
            "candidate_name": item.candidate_name,
            "focus_areas": item.adjustments.get("focus_areas", []),
            "candidate_instructions": item.candidate_instructions,
            "scheduled_start_at": item.scheduled_start_at.astimezone(
                ZoneInfo(item.timezone)
            ).isoformat(),
            "latest_start_at": item.latest_start_at.astimezone(ZoneInfo(item.timezone)).isoformat(),
            "timezone": item.timezone,
            "status": item.status,
            "mail_status": item.mail_status,
        }

    @app.post("/api/admin/assignments/{assignment_id}/cancel")
    async def cancel_assignment(assignment_id: uuid.UUID, request: Request, db: Db) -> dict:
        admin = await require_admin(request, db)
        accounts.check_csrf(request)
        async with db_sessions.begin() as transaction:
            item = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
            if item is None or item.status != "assigned":
                raise HTTPException(409, "Only an unstarted assignment can be cancelled")
            item.status = "cancelled"
            item.updated_at = now()
            sessions_for_assignment = await transaction.scalars(
                select(CandidateInterviewSession).where(
                    CandidateInterviewSession.assignment_id == assignment_id,
                    CandidateInterviewSession.revoked_at.is_(None),
                )
            )
            for session in sessions_for_assignment:
                session.revoked_at = now()
            transaction.add(
                AssignmentAudit(
                    assignment_id=assignment_id,
                    actor_id=admin.id,
                    action="cancelled",
                    occurred_at=now(),
                    detail={},
                )
            )
        return {"status": "cancelled"}

    @app.post("/api/admin/assignments/{assignment_id}/resend")
    async def resend_assignment(assignment_id: uuid.UUID, request: Request, db: Db) -> dict:
        admin = await require_admin(request, db)
        accounts.check_csrf(request)
        await accounts.rate_limit(db, "invite-resend", str(assignment_id), limit=3, seconds=3600)
        return {
            "mail_status": await mail_assignment(
                assignment_id, actor_id=admin.id, action="invite_resent"
            )
        }

    async def accessible_record(
        db: AsyncSession, record_id: uuid.UUID, user: Candidate, *, admin: bool
    ) -> InterviewRecord:
        record = await db.get(InterviewRecord, record_id)
        if record is None or (not admin and record.candidate_id != user.id):
            raise HTTPException(404, "Interview not found")
        return record

    def record_data(
        record: InterviewRecord, candidate_email: str | None = None, *, admin: bool = False
    ) -> dict:
        data = {
            "id": str(record.id),
            "candidate_email": candidate_email,
            "created_at": record.created_at.isoformat(),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "ended_at": record.ended_at.isoformat() if record.ended_at else None,
            "status": record.status,
            "termination_reason": record.termination_reason,
            "build_id": record.build_id,
            "prompt_version": record.prompt_version,
        }
        if admin:
            data["pdf_status"] = record.pdf_status
            data["pdf_error"] = record.pdf_error
        return data

    async def list_records(
        db: AsyncSession,
        *,
        candidate_id: uuid.UUID | None = None,
        candidate_email: str | None = None,
        status: str | None = None,
        page_number: int = 1,
        admin: bool = False,
    ) -> dict:
        if status and status not in {"starting", "in_progress", *TERMINAL}:
            raise HTTPException(400, "Invalid interview status")
        query = (
            select(InterviewRecord, Candidate.email, InterviewAssignment.candidate_email)
            .outerjoin(Candidate, Candidate.id == InterviewRecord.candidate_id)
            .outerjoin(InterviewAssignment, InterviewAssignment.id == InterviewRecord.assignment_id)
        )
        if candidate_id is not None:
            query = query.where(InterviewRecord.candidate_id == candidate_id)
        if candidate_email:
            pattern = candidate_email.strip().casefold()
            query = query.where(
                or_(
                    Candidate.email.contains(pattern, autoescape=True),
                    InterviewAssignment.candidate_email.contains(pattern, autoescape=True),
                )
            )
        if status:
            query = query.where(InterviewRecord.status == status)
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await db.execute(
                query.order_by(InterviewRecord.created_at.desc(), InterviewRecord.id.desc())
                .offset((page_number - 1) * 20)
                .limit(20)
            )
        ).all()
        return {
            "items": [
                record_data(row, account_email or invite_email, admin=admin)
                for row, account_email, invite_email in rows
            ],
            "total": total,
            "page": page_number,
            "page_size": 20,
        }

    async def transcript_rows(db: AsyncSession, record_id: uuid.UUID) -> list[dict]:
        entries = (
            await db.scalars(
                select(TranscriptEntry)
                .where(TranscriptEntry.interview_id == record_id)
                .order_by(TranscriptEntry.sequence)
            )
        ).all()
        return [
            {
                "id": str(entry.id),
                "sequence": entry.sequence,
                "speaker": entry.speaker,
                "text": entry.text,
                "timestamp_utc": entry.timestamp_utc.isoformat(),
                "status": entry.status,
                "interrupted": entry.interrupted,
                "utterance_id": entry.utterance_id,
            }
            for entry in entries
        ]

    @app.get("/api/admin/interviews")
    async def admin_records(
        request: Request,
        db: Db,
        page_number: int = Query(1, ge=1),
        candidate_email: str | None = Query(None, max_length=320),
        status: str | None = None,
    ) -> dict:
        await require_admin(request, db)
        return await list_records(
            db,
            candidate_email=candidate_email,
            status=status,
            page_number=page_number,
            admin=True,
        )

    @app.get("/api/admin/interviews/{record_id}")
    async def admin_record(record_id: uuid.UUID, request: Request, db: Db) -> dict:
        user = await require_admin(request, db)
        record = await accessible_record(db, record_id, user, admin=True)
        if record.assignment_id:
            assignment = await db.get(InterviewAssignment, record.assignment_id)
            address = assignment.candidate_email
        else:
            candidate = await db.get(Candidate, record.candidate_id)
            address = candidate.email
        return record_data(record, address, admin=True)

    @app.get("/api/admin/interviews/{record_id}/transcript")
    async def admin_transcript(record_id: uuid.UUID, request: Request, db: Db) -> list[dict]:
        user = await require_admin(request, db)
        await accessible_record(db, record_id, user, admin=True)
        return await transcript_rows(db, record_id)

    @app.post("/api/interviews/{record_id}/snapshot", status_code=201)
    async def candidate_snapshot(record_id: uuid.UUID, request: Request, db: Db) -> dict[str, str]:
        assignment = await candidate_session(db, request)
        accounts.check_csrf(request)
        record = await db.get(InterviewRecord, record_id)
        if record is None or record.assignment_id != assignment.id:
            raise HTTPException(404, "Interview not found")
        if record.status != "in_progress" or record.snapshot_status != "pending":
            raise HTTPException(409, "Camera snapshot is not accepted for this interview")
        if not any(
            live.log.session_id == str(record_id) and live.candidate_id == str(assignment.id)
            for live in sessions
        ):
            raise HTTPException(409, "Interview connection is no longer active")
        if request.headers.get("content-type", "").split(";", 1)[0] != "image/jpeg":
            raise HTTPException(415, "Expected a JPEG camera image")
        size_header = request.headers.get("content-length", "0")
        if not size_header.isdecimal():
            raise HTTPException(400, "Invalid camera image length")
        if int(size_header) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Camera image is too large")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Camera image is too large")
        async with db_sessions.begin() as transaction:
            current = await transaction.get(InterviewRecord, record_id, with_for_update=True)
            if (
                current is None
                or current.assignment_id != assignment.id
                or current.status != "in_progress"
                or current.snapshot_status != "pending"
            ):
                raise HTTPException(409, "Camera snapshot is no longer accepted")
            try:
                await asyncio.to_thread(save_snapshot, bytes(raw), store.snapshot_path(record_id))
            except InvalidSnapshot as exc:
                raise HTTPException(422, str(exc)) from exc
            current.snapshot_status = "available"
            current.snapshot_captured_at = datetime.now(UTC)
        store.snapshot_ready(record_id)
        return {"status": "saved"}

    async def pdf_response(record: InterviewRecord) -> Response:
        if record.status not in TERMINAL:
            raise HTTPException(409, "Available after the interview ends")
        path = store.pdf_path(record.id)
        if record.pdf_status == "available" and not path.is_file():
            async with db_sessions.begin() as transaction:
                await transaction.execute(
                    update(InterviewRecord)
                    .where(
                        InterviewRecord.id == record.id,
                        InterviewRecord.pdf_status == "available",
                    )
                    .values(pdf_status="failed", pdf_error="MissingFile")
                )
            raise HTTPException(409, "PDF file is missing; refresh to regenerate it")
        if record.pdf_status != "available":
            raise HTTPException(409, "PDF is not available yet")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"rehearseai-{record.id}.pdf",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/interviews/{record_id}/pdf")
    async def legacy_admin_pdf(record_id: uuid.UUID, request: Request, db: Db) -> Response:
        user = await require_admin(request, db)
        return await pdf_response(await accessible_record(db, record_id, user, admin=True))

    @app.get("/api/admin/interviews/{record_id}/pdf")
    async def admin_pdf(record_id: uuid.UUID, request: Request, db: Db) -> Response:
        user = await require_admin(request, db)
        return await pdf_response(await accessible_record(db, record_id, user, admin=True))

    async def regenerate(record: InterviewRecord) -> dict[str, str]:
        async with db_sessions.begin() as transaction:
            result = await transaction.execute(
                update(InterviewRecord)
                .where(
                    InterviewRecord.id == record.id,
                    InterviewRecord.status.in_(TERMINAL),
                    InterviewRecord.pdf_status == "failed",
                )
                .values(pdf_status="pending", pdf_error=None)
            )
        if result.rowcount != 1:
            raise HTTPException(409, "PDF regeneration is only available after failure")
        if not store.schedule_pdf(record.id):
            raise HTTPException(503, "PDF queue is full; restart to retry pending generation")
        return {"message": "PDF regeneration queued"}

    @app.post("/api/interviews/{record_id}/pdf/regenerate", status_code=202)
    async def legacy_admin_regenerate(record_id: uuid.UUID, request: Request, db: Db) -> dict:
        user = await require_admin(request, db)
        accounts.check_csrf(request)
        return await regenerate(await accessible_record(db, record_id, user, admin=True))

    @app.post("/api/admin/interviews/{record_id}/pdf/regenerate", status_code=202)
    async def admin_regenerate(record_id: uuid.UUID, request: Request, db: Db) -> dict:
        user = await require_admin(request, db)
        accounts.check_csrf(request)
        return await regenerate(await accessible_record(db, record_id, user, admin=True))

    @app.post("/api/auth/signup", status_code=410)
    async def sign_up() -> dict[str, str]:
        return {"message": "Candidate accounts are no longer offered"}

    @app.post("/api/auth/login")
    async def login(payload: PasswordPayload, request: Request, db: Db) -> Response:
        accounts.check_csrf(request)
        normalized = email(payload.email)
        await limit(db, request, "login", normalized, count=10)
        return await accounts.login(db, normalized, payload.password)

    @app.post("/api/auth/logout")
    async def logout(request: Request, db: Db) -> Response:
        accounts.check_csrf(request)
        candidate = await accounts.current_user(request, db)
        response = await accounts.logout(request, db)
        if candidate:
            for live in tuple(sessions):
                if live.candidate_id == str(candidate.id):
                    await live.stop()
                    await live.connection.disconnect()
        return response

    @app.post("/api/auth/request-reset", status_code=202)
    async def request_reset(payload: EmailPayload, request: Request, db: Db) -> dict[str, str]:
        accounts.check_csrf(request)
        normalized = email(payload.email)
        await limit(db, request, "reset-request", normalized, count=3, seconds=3600)
        manager = accounts.manager(db)
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

    @app.post("/api/auth/reset-password")
    async def reset(payload: ResetPayload, request: Request, db: Db) -> dict[str, str]:
        accounts.check_csrf(request)
        await accounts.rate_limit(
            db,
            "reset:ip",
            request.client.host if request.client else "unknown",
            limit=20,
            seconds=600,
        )
        candidate_id = await accounts.reset_password(db, payload.token, payload.password)
        for live in tuple(sessions):
            if live.candidate_id == str(candidate_id):
                await live.stop()
                await live.connection.disconnect()
        return {"message": "Password changed. Sign in again on all devices."}

    @app.get("/healthz")
    async def health(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"status": "ready", "build_id": build_id}

    @app.post("/api/offer")
    async def offer(request: Request, db: Db) -> dict[str, str]:
        assignment = await candidate_session(db, request)
        accounts.check_csrf(request)
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or body.get("type") != "offer"
                or body.get("snapshot_acknowledged") is not True
            ):
                raise ValueError("Expected a WebRTC offer")
            offer_request = SmallWebRTCRequest.from_dict(
                {key: value for key, value in body.items() if key != "snapshot_acknowledged"}
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid WebRTC offer") from exc

        version = await db.get(BlueprintVersion, assignment.blueprint_version_id)
        if version is None:
            raise HTTPException(409, "The assigned interview plan is unavailable")
        try:
            record_id = await store.create_assignment_attempt(
                assignment.id, build_id, prompt_version
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        async def connected(connection: object) -> None:
            live = LiveSession(
                connection,
                settings,
                build_id=build_id,
                candidate_id=str(assignment.id),
                session_id=str(record_id),
                event_sink=store.enqueue,
                interview_plan={
                    **version.content,
                    "candidate_focus_areas": assignment.adjustments.get("focus_areas", []),
                    "blueprint_version_id": str(version.id),
                },
                self_reported_name=assignment.self_reported_name,
            )
            sessions.add(live)
            task = asyncio.create_task(live.run())
            tasks.add(task)

            def finished(done: asyncio.Task[None]) -> None:
                tasks.discard(done)
                sessions.discard(live)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)

        try:
            result = await handler.handle_web_request(offer_request, connected)
        except Exception:
            async with db_sessions.begin() as transaction:
                record = await transaction.get(InterviewRecord, record_id)
                record.status = "failed"
                record.termination_reason = "signalling_failed"
                record.ended_at = now()
                record.pdf_status = "pending"
                assigned = await transaction.get(InterviewAssignment, assignment.id)
                assigned.status = "failed"
            store.schedule_pdf(record_id)
            raise
        if result is None:
            async with db_sessions.begin() as transaction:
                record = await transaction.get(InterviewRecord, record_id)
                record.status = "failed"
                record.termination_reason = "signalling_rejected"
                record.ended_at = now()
                record.pdf_status = "pending"
                assigned = await transaction.get(InterviewAssignment, assignment.id)
                assigned.status = "failed"
            store.schedule_pdf(record_id)
            raise HTTPException(status_code=503, detail="WebRTC offer was not accepted")
        return result

    return app


def main() -> None:
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    app = create_app(settings)
    print(f"RehearseAI build {app.state.build_id}", flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)
