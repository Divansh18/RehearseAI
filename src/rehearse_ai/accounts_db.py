"""Persistent candidate accounts and revocable authentication records."""

import uuid
from datetime import UTC, datetime

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


class InterviewBlueprint(Base):
    __tablename__ = "interview_blueprint"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidate.id", ondelete="RESTRICT"))
    draft: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BlueprintVersion(Base):
    __tablename__ = "interview_blueprint_version"
    __table_args__ = (UniqueConstraint("blueprint_id", "version", name="uq_blueprint_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    blueprint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_blueprint.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InterviewAssignment(Base):
    __tablename__ = "interview_assignment"
    __table_args__ = (
        CheckConstraint("latest_start_at >= scheduled_start_at", name="ck_assignment_window"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Null until automatic preparation has produced a valid plan version.
    blueprint_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("interview_blueprint_version.id", ondelete="RESTRICT"), nullable=True
    )
    candidate_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    candidate_name: Mapped[str | None] = mapped_column(String(120))
    self_reported_name: Mapped[str | None] = mapped_column(String(120))
    adjustments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    candidate_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduled_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="assigned")
    invitation_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invitation_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mail_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending -> preparing -> ready | failed. Existing assignments are 'ready'.
    preparation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", server_default="ready"
    )
    preparation_error: Mapped[str | None] = mapped_column(String(64))
    target_minutes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandidateInterviewSession(Base):
    __tablename__ = "candidate_interview_session"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_assignment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssignmentAudit(Base):
    __tablename__ = "interview_assignment_audit"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_assignment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidate.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class InterviewRecord(Base):
    __tablename__ = "interview_record"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("candidate.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("interview_assignment.id", ondelete="RESTRICT"), unique=True
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
    snapshot_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_started")
    snapshot_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The plan version this attempt ran, frozen atomically when the attempt starts.
    blueprint_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("interview_blueprint_version.id", ondelete="RESTRICT")
    )
    # Number-only account of questions asked, covered, skipped for time or not reached.
    coverage_summary: Mapped[dict | None] = mapped_column(JSON)


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


class InterviewPreparation(Base):
    """Private inputs for automatic plan preparation. Never exposed to candidates."""

    __tablename__ = "interview_preparation"
    __table_args__ = (
        UniqueConstraint("created_by", "idempotency_key", name="uq_preparation_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_assignment.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate.id", ondelete="RESTRICT"), nullable=False
    )
    blueprint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("interview_blueprint.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_filename: Mapped[str] = mapped_column(String(200), nullable=False)
    resume_path: Mapped[str] = mapped_column(String(96), nullable=False)
    resume_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_text: Mapped[str] = mapped_column(Text, nullable=False)
    jd_source: Mapped[str] = mapped_column(String(8), nullable=False)
    jd_filename: Mapped[str | None] = mapped_column(String(200))
    jd_path: Mapped[str | None] = mapped_column(String(96))
    jd_sha256: Mapped[str | None] = mapped_column(String(64))
    job_description_text: Mapped[str] = mapped_column(Text, nullable=False)
    interviewer_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InterviewJob(Base):
    """A durable unit of background work: prepare, revise or invite."""

    __tablename__ = "interview_job"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_assignment.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
