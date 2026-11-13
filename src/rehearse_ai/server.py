"""Candidate account routes and protected local WebRTC signaling."""

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

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
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rehearse_ai.accounts import AccountService, MailDeliveryError
from rehearse_ai.accounts_db import Candidate, InterviewRecord, TranscriptEntry
from rehearse_ai.config import ConfigurationError, Settings
from rehearse_ai.interview_store import TERMINAL, InterviewStore
from rehearse_ai.pipeline import LiveSession

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

    store = InterviewStore(db_sessions, settings.session_dir, settings.pdf_dir, storage_failure)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with db_sessions() as db:
                await db.execute(select(Candidate.id).limit(1))
                await db.execute(select(InterviewRecord.id).limit(1))
        except SQLAlchemyError:
            raise RuntimeError(
                "Candidate database is unavailable or unmigrated. "
                "Start PostgreSQL and run `uv run alembic upgrade head`."
            ) from None
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
            "styles.css",
            "account.js",
            "account.css",
            "history.js",
            "theme.css",
        }:
            raise HTTPException(404)
        path = WEB_DIR / name
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return Response(path.read_text(encoding="utf-8"), media_type=media)

    @app.get("/")
    async def home(request: Request, db: Db) -> RedirectResponse:
        user = await accounts.current_user(request, db)
        if user:
            return RedirectResponse("/dashboard", status_code=303)
        return sign_in_redirect(request)

    @app.get("/login")
    @app.get("/signup")
    @app.get("/verify")
    @app.get("/reset-password")
    async def account_page() -> HTMLResponse:
        return page("account.html")

    @app.get("/dashboard")
    async def dashboard(request: Request, db: Db) -> Response:
        user = await accounts.current_user(request, db)
        if not user:
            return sign_in_redirect(request)
        return page("dashboard.html")

    @app.get("/interviews/{record_id}")
    async def candidate_detail(record_id: uuid.UUID, request: Request, db: Db) -> Response:
        user = await accounts.current_user(request, db)
        if not user:
            return sign_in_redirect(request)
        await accessible_record(db, record_id, user, admin=False)
        return page("interview_record.html")

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
        user = await accounts.current_user(request, db)
        if not user:
            return sign_in_redirect(request)
        return page("index.html")

    @app.get("/api/csrf")
    async def csrf() -> Response:
        return accounts.csrf_response()

    @app.get("/api/auth/me")
    async def me(request: Request, db: Db) -> dict[str, str]:
        user = await accounts.require_user(request, db)
        return {
            "email": user.email,
            "id": str(user.id),
            "role": "admin" if user.is_superuser else "candidate",
        }

    async def require_admin(request: Request, db: AsyncSession) -> Candidate:
        user = await accounts.require_user(request, db)
        if not user.is_superuser:
            raise HTTPException(403, "Administrator access required")
        return user

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
        query = select(InterviewRecord, Candidate.email).join(
            Candidate, Candidate.id == InterviewRecord.candidate_id
        )
        if candidate_id is not None:
            query = query.where(InterviewRecord.candidate_id == candidate_id)
        if candidate_email:
            query = query.where(
                Candidate.email.contains(candidate_email.strip().casefold(), autoescape=True)
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
            "items": [record_data(row, address, admin=admin) for row, address in rows],
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

    @app.get("/api/interviews")
    async def candidate_records(
        request: Request, db: Db, page_number: int = Query(1, ge=1)
    ) -> dict:
        user = await accounts.require_user(request, db)
        return await list_records(db, candidate_id=user.id, page_number=page_number)

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

    @app.get("/api/interviews/{record_id}")
    async def candidate_record(record_id: uuid.UUID, request: Request, db: Db) -> dict:
        user = await accounts.require_user(request, db)
        return record_data(await accessible_record(db, record_id, user, admin=False))

    @app.get("/api/admin/interviews/{record_id}")
    async def admin_record(record_id: uuid.UUID, request: Request, db: Db) -> dict:
        user = await require_admin(request, db)
        record = await accessible_record(db, record_id, user, admin=True)
        candidate = await db.get(Candidate, record.candidate_id)
        return record_data(record, candidate.email, admin=True)

    @app.get("/api/interviews/{record_id}/transcript")
    async def candidate_transcript(record_id: uuid.UUID, request: Request, db: Db) -> list[dict]:
        user = await accounts.require_user(request, db)
        await accessible_record(db, record_id, user, admin=False)
        return await transcript_rows(db, record_id)

    @app.get("/api/admin/interviews/{record_id}/transcript")
    async def admin_transcript(record_id: uuid.UUID, request: Request, db: Db) -> list[dict]:
        user = await require_admin(request, db)
        await accessible_record(db, record_id, user, admin=True)
        return await transcript_rows(db, record_id)

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

    @app.post("/api/auth/signup", status_code=201)
    async def sign_up(payload: PasswordPayload, request: Request, db: Db) -> dict[str, str]:
        accounts.check_csrf(request)
        normalized = email(payload.email)
        await limit(db, request, "signup", normalized)
        await accounts.sign_up(db, normalized, payload.password)
        return {"message": "Account created. Sign in to continue."}

    @app.post("/api/auth/resend-verification", status_code=202)
    async def resend(payload: EmailPayload, request: Request, db: Db) -> dict[str, str]:
        accounts.check_csrf(request)
        normalized = email(payload.email)
        await limit(db, request, "resend", normalized, count=3, seconds=3600)
        manager = accounts.manager(db)
        try:
            user = await manager.get_by_email(normalized)
            if not user.is_verified:
                await manager.request_verify(user)
        except exceptions.UserNotExists:
            pass
        except MailDeliveryError as exc:
            raise HTTPException(
                503, "Verification email could not be sent. Try again later"
            ) from exc
        return {
            "message": "If needed, a verification link was sent to the configured email "
            "service. Local development captures mail in Mailpit."
        }

    @app.post("/api/auth/verify")
    async def verify(payload: TokenPayload, request: Request, db: Db) -> dict[str, str]:
        accounts.check_csrf(request)
        await accounts.rate_limit(
            db,
            "verify:ip",
            request.client.host if request.client else "unknown",
            limit=20,
            seconds=600,
        )
        await accounts.consume_action(db, payload.token, "verify")
        try:
            await accounts.manager(db).verify(payload.token)
        except (exceptions.InvalidVerifyToken, exceptions.UserAlreadyVerified) as exc:
            raise HTTPException(400, "This link is invalid or has expired") from exc
        return {"message": "Email verified. You can sign in."}

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
        candidate = await accounts.require_user(request, db)
        accounts.check_csrf(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("type") != "offer":
                raise ValueError("Expected a WebRTC offer")
            offer_request = SmallWebRTCRequest.from_dict(body)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid WebRTC offer") from exc

        async def connected(connection: object) -> None:
            record_id = await store.create_attempt(candidate.id, build_id, prompt_version)
            live = LiveSession(
                connection,
                settings,
                build_id=build_id,
                candidate_id=str(candidate.id),
                session_id=str(record_id),
                event_sink=store.enqueue,
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

        result = await handler.handle_web_request(offer_request, connected)
        if result is None:
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
