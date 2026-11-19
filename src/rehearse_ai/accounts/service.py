"""Candidate account lifecycle; voice-domain state never imports this module."""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiosmtplib
from fastapi import HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import BaseUserManager, UUIDIDMixin, exceptions, schemas
from fastapi_users.authentication import CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.jwt import generate_jwt
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyAccessTokenDatabase
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.config import AuthConfig
from rehearse_ai.models import ActionToken, Candidate, RateBucket, SessionToken


def utcnow() -> datetime:
    return datetime.now(UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class CandidateCreate(schemas.BaseUserCreate):
    pass


class MailDeliveryError(RuntimeError):
    """The configured SMTP service did not accept an account email."""


class Mailer:
    def __init__(self, config: AuthConfig):
        self.config = config

    async def send_link(self, to: str, purpose: str, token: str) -> None:
        link_path = "/verify" if purpose == "verify" else "/reset-password"
        link = f"{self.config.public_base_url}{link_path}?token={quote(token, safe='')}"
        message = EmailMessage()
        message["From"] = self.config.smtp_from
        message["To"] = to
        message["Subject"] = (
            "Verify your RehearseAI email"
            if purpose == "verify"
            else "Reset your RehearseAI password"
        )
        action = "verify your email" if purpose == "verify" else "reset your password"
        message.set_content(
            f"Open this link to {action}:\n{link}\n\n"
            "This link expires in one hour. If you did not request it, ignore this email."
        )
        try:
            await aiosmtplib.send(
                message,
                hostname=self.config.smtp_host,
                port=self.config.smtp_port,
                username=self.config.smtp_username,
                password=self.config.smtp_password,
                start_tls=self.config.smtp_starttls,
                timeout=10,
            )
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise MailDeliveryError("Account email delivery failed") from exc

    async def send_invitation(
        self,
        to: str,
        token: str,
        *,
        title: str,
        scheduled_at: datetime,
        latest_at: datetime,
        timezone: str,
        target_minutes: int,
    ) -> None:
        local_start = scheduled_at.astimezone(ZoneInfo(timezone))
        local_latest = latest_at.astimezone(ZoneInfo(timezone))
        link = f"{self.config.public_base_url}/join/{quote(token, safe='')}"
        message = EmailMessage()
        message["From"] = self.config.smtp_from
        message["To"] = to
        message["Subject"] = f"RehearseAI interview invitation: {title}"
        message.set_content(
            f"{self.config.interviewer_name} invites you to a RehearseAI interview.\n\n"
            f"Interview: {title}\n"
            f"Scheduled: {local_start:%d %B %Y, %H:%M} {timezone}\n"
            f"Begin between {local_start:%H:%M} and {local_latest:%H:%M} {timezone}.\n"
            f"Expected duration: about {target_minutes} minutes.\n"
            "You will need a camera, microphone, speakers, and screen sharing.\n\n"
            f"Join interview: {link}\n\n"
            "This private link can be used by anyone who possesses it. Do not forward it."
        )
        try:
            await aiosmtplib.send(
                message,
                hostname=self.config.smtp_host,
                port=self.config.smtp_port,
                username=self.config.smtp_username,
                password=self.config.smtp_password,
                start_tls=self.config.smtp_starttls,
                timeout=10,
            )
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise MailDeliveryError("Interview invitation delivery failed") from exc


class CandidateManager(UUIDIDMixin, BaseUserManager[Candidate, uuid.UUID]):
    def __init__(self, db: AsyncSession, mailer: Mailer, secret: str):
        super().__init__(SQLAlchemyUserDatabase(db, Candidate))
        self.db = db
        self.mailer = mailer
        self.verification_token_secret = secret
        self.reset_password_token_secret = secret

    async def validate_password(self, password: str, user: object) -> None:
        if len(password) < 12 or len(password) > 256:
            raise exceptions.InvalidPasswordException("Use a password of 12–256 characters")

    async def request_verify(self, user: Candidate, request: Request | None = None) -> None:
        if not user.is_active:
            raise exceptions.UserInactive()
        if user.is_verified:
            raise exceptions.UserAlreadyVerified()
        token = generate_jwt(
            {
                "sub": str(user.id),
                "email": user.email,
                "aud": self.verification_token_audience,
                "jti": secrets.token_urlsafe(16),
            },
            self.verification_token_secret,
            self.verification_token_lifetime_seconds,
        )
        await self.on_after_request_verify(user, token, request)

    async def _send_action(self, user: Candidate, token: str, purpose: str) -> None:
        if purpose == "verify":
            await self.db.execute(
                update(ActionToken)
                .where(ActionToken.candidate_id == user.id, ActionToken.purpose == purpose)
                .values(used_at=utcnow())
            )
        self.db.add(
            ActionToken(
                digest=digest(token),
                candidate_id=user.id,
                purpose=purpose,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        await self.db.commit()
        await self.mailer.send_link(user.email, purpose, token)

    async def on_after_request_verify(
        self, user: Candidate, token: str, request: Request | None = None
    ) -> None:
        await self._send_action(user, token, "verify")

    async def on_after_forgot_password(
        self, user: Candidate, token: str, request: Request | None = None
    ) -> None:
        await self._send_action(user, token, "reset")


class AccountService:
    def __init__(self, config: AuthConfig, sessions: async_sessionmaker[AsyncSession]):
        self.config = config
        self.sessions = sessions
        self.mailer = Mailer(config)
        self.csrf_signer = URLSafeTimedSerializer(config.secret, salt="rehearse-csrf-v1")
        self.cookies = CookieTransport(
            cookie_name="rehearse_session",
            cookie_max_age=config.session_seconds,
            cookie_secure=config.secure_cookie,
            cookie_httponly=True,
            cookie_samesite="lax",
        )

    def csrf_response(self) -> Response:
        token = self.csrf_signer.dumps(secrets.token_urlsafe(24))
        response = Response(content=f'{{"token":"{token}"}}', media_type="application/json")
        response.set_cookie(
            "rehearse_csrf",
            token,
            max_age=3600,
            httponly=True,
            secure=self.config.secure_cookie,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def check_csrf(self, request: Request) -> None:
        origin = request.headers.get("origin")
        cookie = request.cookies.get("rehearse_csrf")
        header = request.headers.get("x-csrf-token")
        if origin and origin != self.config.public_base_url:
            raise HTTPException(403, "Request origin was rejected")
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise HTTPException(403, "Refresh the page and try again")
        try:
            self.csrf_signer.loads(header, max_age=3600)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(403, "Refresh the page and try again") from exc

    def manager(self, db: AsyncSession) -> CandidateManager:
        return CandidateManager(db, self.mailer, self.config.secret)

    def strategy(self, db: AsyncSession) -> DatabaseStrategy:
        return DatabaseStrategy(
            SQLAlchemyAccessTokenDatabase(db, SessionToken),
            lifetime_seconds=self.config.session_seconds,
        )

    async def current_user(self, request: Request, db: AsyncSession) -> Candidate | None:
        token = request.cookies.get(self.cookies.cookie_name)
        user = await self.strategy(db).read_token(token, self.manager(db))
        return user if user and user.is_active else None

    async def require_user(self, request: Request, db: AsyncSession) -> Candidate:
        user = await self.current_user(request, db)
        if not user:
            raise HTTPException(401, "Sign in to continue")
        return user

    async def rate_limit(
        self, db: AsyncSession, operation: str, identity: str, *, limit: int, seconds: int
    ) -> None:
        key = digest(f"{operation}:{identity}")
        now = utcnow()
        bucket = await db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
        if bucket is None:
            db.add(RateBucket(key=key, window_started_at=now, count=1))
        elif bucket.window_started_at + timedelta(seconds=seconds) <= now:
            bucket.window_started_at = now
            bucket.count = 1
        else:
            bucket.count += 1
            if bucket.count > limit:
                await db.rollback()
                raise HTTPException(429, "Too many requests. Please try again later")
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(429, "Too many requests. Please try again later") from exc

    async def consume_action(self, db: AsyncSession, token: str, purpose: str) -> None:
        result = await db.execute(
            update(ActionToken)
            .where(
                ActionToken.digest == digest(token),
                ActionToken.purpose == purpose,
                ActionToken.used_at.is_(None),
                ActionToken.expires_at > utcnow(),
            )
            .values(used_at=utcnow())
            .returning(ActionToken.digest)
        )
        if result.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(400, "This link is invalid or has expired")
        await db.commit()

    async def login(self, db: AsyncSession, email: str, password: str) -> Response:
        manager = self.manager(db)
        credentials = OAuth2PasswordRequestForm(username=email, password=password, scope="")
        user = await manager.authenticate(credentials)
        if not user or not user.is_active or not user.is_superuser:
            raise HTTPException(401, "Invalid credentials")
        token = await self.strategy(db).write_token(user)
        return await self.cookies.get_login_response(token)

    async def logout(self, request: Request, db: AsyncSession) -> Response:
        token = request.cookies.get(self.cookies.cookie_name)
        if token:
            user = await self.strategy(db).read_token(token, self.manager(db))
            if user:
                await self.strategy(db).destroy_token(token, user)
        return await self.cookies.get_logout_response()

    async def reset_password(self, db: AsyncSession, token: str, password: str) -> uuid.UUID:
        try:
            await self.manager(db).validate_password(password, None)
        except exceptions.InvalidPasswordException as exc:
            raise HTTPException(400, str(exc.reason)) from exc
        await self.consume_action(db, token, "reset")
        try:
            user = await self.manager(db).reset_password(token, password)
        except (exceptions.InvalidResetPasswordToken, exceptions.UserNotExists) as exc:
            raise HTTPException(400, "This link is invalid or has expired") from exc
        except exceptions.InvalidPasswordException as exc:
            raise HTTPException(400, str(exc.reason)) from exc
        await db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
        await db.commit()
        return user.id
