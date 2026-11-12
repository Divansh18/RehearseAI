"""Candidate account routes and protected local WebRTC signaling."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi_users import exceptions
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rehearse_ai.accounts import AccountService, MailDeliveryError
from rehearse_ai.accounts_db import Candidate
from rehearse_ai.config import ConfigurationError, Settings
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
    handler = SmallWebRTCRequestHandler()
    sessions: set[LiveSession] = set()
    tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with db_sessions() as db:
                await db.execute(select(Candidate.id).limit(1))
        except SQLAlchemyError:
            raise RuntimeError(
                "Candidate database is unavailable or unmigrated. "
                "Start PostgreSQL and run `uv run alembic upgrade head`."
            ) from None
        yield
        for session in tuple(sessions):
            await session.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await handler.close()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.build_id = build_id
    app.state.accounts = accounts

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
        if name not in {"app.js", "styles.css", "account.js", "account.css"}:
            raise HTTPException(404)
        path = WEB_DIR / name
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return Response(path.read_text(encoding="utf-8"), media_type=media)

    @app.get("/")
    async def home(request: Request, db: Db) -> RedirectResponse:
        user = await accounts.current_user(request, db)
        return RedirectResponse("/dashboard" if user else "/login", status_code=303)

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
            return RedirectResponse("/login", status_code=303)
        return page("dashboard.html")

    @app.get("/interview")
    async def interview(request: Request, db: Db) -> Response:
        user = await accounts.current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        return page("index.html")

    @app.get("/api/csrf")
    async def csrf() -> Response:
        return accounts.csrf_response()

    @app.get("/api/auth/me")
    async def me(request: Request, db: Db) -> dict[str, str]:
        user = await accounts.require_user(request, db)
        return {"email": user.email, "id": str(user.id)}

    @app.post("/api/auth/signup", status_code=202)
    async def sign_up(payload: PasswordPayload, request: Request, db: Db) -> dict[str, str]:
        accounts.check_csrf(request)
        normalized = email(payload.email)
        await limit(db, request, "signup", normalized)
        try:
            await accounts.sign_up(db, normalized, payload.password)
        except MailDeliveryError as exc:
            raise HTTPException(
                503, "Verification email could not be sent. Try again later"
            ) from exc
        return {"message": "Check your email for a verification link"}

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
        return {"message": "If verification is needed, an email is on its way"}

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
        return {"message": "If this account exists, a reset email is on its way"}

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
            live = LiveSession(
                connection, settings, build_id=build_id, candidate_id=str(candidate.id)
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
