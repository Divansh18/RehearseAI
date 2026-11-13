"""Persistent candidate accounts and revocable authentication records."""

import uuid
from datetime import UTC, datetime

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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


class InterviewRecord(Base):
    __tablename__ = "interview_record"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="starting")
    termination_reason: Mapped[str | None] = mapped_column(String(48))
    build_id: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pdf_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_ready")
    pdf_error: Mapped[str | None] = mapped_column(String(120))


class TranscriptEntry(Base):
    __tablename__ = "interview_transcript"
    __table_args__ = (
        UniqueConstraint("interview_id", "source_event_id", name="uq_transcript_event"),
        UniqueConstraint("interview_id", "utterance_id", name="uq_transcript_utterance"),
        UniqueConstraint("interview_id", "sequence", name="uq_transcript_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_record.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_event_id: Mapped[str] = mapped_column(String(96), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    utterance_id: Mapped[int | None] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    output_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
