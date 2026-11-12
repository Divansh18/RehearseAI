"""Persistent candidate accounts and revocable authentication records."""

import uuid
from datetime import UTC, datetime

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Candidate(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "candidate"


class SessionToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    __tablename__ = "candidate_session"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), nullable=False, index=True
    )


class ActionToken(Base):
    __tablename__ = "account_action_token"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateBucket(Base):
    __tablename__ = "auth_rate_bucket"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
