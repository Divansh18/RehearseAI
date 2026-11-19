"""Schedule an interview: validate the form, store documents privately, persist atomically.

``schedule_interview`` writes the assignment, the private preparation inputs and both
background jobs (plan preparation and invitation delivery) in one short transaction, before
any model or SMTP call. A repeated submission with the same idempotency key returns the
original interview instead of creating another.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rehearse_ai.accounts.service import MailDeliveryError, Mailer
from rehearse_ai.invitations.access import BlueprintContent, invitation_token, now, token_digest
from rehearse_ai.jobs import ClaimedJob, JobCancelled, JobError, enqueue, verify_claim
from rehearse_ai.models import (
    AssignmentAudit,
    BlueprintVersion,
    CandidateInterviewSession,
    InterviewAssignment,
    InterviewPreparation,
)
from rehearse_ai.scheduling.documents import (
    DocumentError,
    detect_kind,
    extract_document,
    store_document,
    validate_text,
)

MAX_INSTRUCTIONS = 2000
MAX_FUTURE_DAYS = 366
_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")
_KEY = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ScheduleRejected(Exception):
    """One or more form fields are invalid. ``errors`` is a list of field/message pairs."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("invalid schedule form")
        self.errors = errors


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different submission."""


class InvitationUnavailable(Exception):
    """The assignment can no longer be invited (started, cancelled or window passed)."""


@dataclass(frozen=True)
class Upload:
    filename: str
    data: bytes


@dataclass(frozen=True)
class ScheduleResult:
    assignment_id: uuid.UUID
    created: bool


class ScheduleFields(BaseModel):
    candidate_name: str = Field(min_length=2, max_length=120)
    candidate_email: EmailStr
    interviewer_instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS)
    job_description_text: str = Field(default="", max_length=60_000)
    scheduled_local: str = Field(min_length=16, max_length=19)
    timezone: str = Field(min_length=1, max_length=64)
    target_minutes: int = Field(ge=5, le=120)
    join_window_minutes: int = Field(default=30, ge=5, le=1440)
    idempotency_key: str

    @field_validator("candidate_name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2 or any(char in value for char in "<>"):
            raise ValueError("Enter the candidate's full name")
        return value

    @field_validator("interviewer_instructions", "job_description_text")
    @classmethod
    def no_control(cls, value: str) -> str:
        return _CONTROL.sub(" ", value).strip()

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if not _KEY.match(value):
            raise ValueError("Reload the page and submit again")
        return value


def resolve_window(
    local_text: str, timezone: str, window_minutes: int, *, instant: datetime
) -> tuple[datetime, datetime]:
    """Convert a wall-clock time in a named zone to UTC, rejecting DST gaps and overlaps.

    A local time that does not exist (spring forward) or occurs twice (fall back) is
    ambiguous about the real moment, so it is rejected instead of guessed.
    """
    if not _TIMEZONE.match(timezone):
        raise ScheduleRejected(
            [{"field": "timezone", "message": "Use a timezone such as Asia/Kolkata"}]
        )
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleRejected(
            [{"field": "timezone", "message": "Use an IANA timezone such as Asia/Kolkata"}]
        ) from None
    try:
        naive = datetime.fromisoformat(local_text)
    except ValueError:
        raise ScheduleRejected(
            [{"field": "scheduled_local", "message": "Enter a valid date and time"}]
        ) from None
    if naive.tzinfo is not None:
        raise ScheduleRejected(
            [{"field": "scheduled_local", "message": "Enter the local time without an offset"}]
        )
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        round_trip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        problem = (
            "does not exist on that date because clocks move forward"
            if round_trip != naive
            else "occurs twice on that date because clocks move back"
        )
        raise ScheduleRejected(
            [
                {
                    "field": "scheduled_local",
                    "message": f"That local time {problem} in {timezone}. Choose a different time.",
                }
            ]
        )
    start = first.astimezone(UTC)
    latest = start + timedelta(minutes=window_minutes)
    if latest <= instant or start < instant - timedelta(minutes=5):
        raise ScheduleRejected(
            [{"field": "scheduled_local", "message": "Choose a start time that has not passed"}]
        )
    if start > instant + timedelta(days=MAX_FUTURE_DAYS):
        raise ScheduleRejected(
            [{"field": "scheduled_local", "message": "Choose a start within the next year"}]
        )
    return start, latest


def fingerprint(fields: ScheduleFields, resume_sha: str, jd_sha: str | None) -> str:
    body = fields.model_dump(mode="json", exclude={"idempotency_key"})
    body["resume_sha256"] = resume_sha
    body["jd_sha256"] = jd_sha
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def parse_fields(raw: dict[str, str]) -> ScheduleFields:
    try:
        return ScheduleFields.model_validate(raw)
    except ValidationError as exc:
        errors = []
        for error in exc.errors():
            location = str(error["loc"][0]) if error["loc"] else "form"
            message = error["msg"].removeprefix("Value error, ")
            errors.append({"field": location, "message": message})
        raise ScheduleRejected(errors) from None


async def _existing(
    db_sessions: async_sessionmaker, admin_id: uuid.UUID, key: str
) -> InterviewPreparation | None:
    async with db_sessions() as db:
        return await db.scalar(
            select(InterviewPreparation).where(
                InterviewPreparation.created_by == admin_id,
                InterviewPreparation.idempotency_key == key,
            )
        )


def _remove(directory: Path, *names: str | None) -> None:
    for name in names:
        if name:
            (directory / name).unlink(missing_ok=True)


async def schedule_interview(
    db_sessions: async_sessionmaker,
    document_dir: Path,
    *,
    admin_id: uuid.UUID,
    fields: ScheduleFields,
    resume: Upload | None,
    job_file: Upload | None,
) -> ScheduleResult:
    errors: list[dict[str, str]] = []
    if resume is None or not resume.data:
        errors.append(
            {"field": "resume", "message": "Upload the candidate's résumé (PDF or DOCX)."}
        )
    if fields.job_description_text and job_file is not None and job_file.data:
        errors.append(
            {
                "field": "job_description",
                "message": "Provide the job description as pasted text or a file, not both.",
            }
        )
    if not fields.job_description_text and (job_file is None or not job_file.data):
        errors.append(
            {"field": "job_description", "message": "Paste the job description or upload a file."}
        )
    start = latest = None
    try:
        start, latest = resolve_window(
            fields.scheduled_local, fields.timezone, fields.join_window_minutes, instant=now()
        )
    except ScheduleRejected as exc:
        errors.extend(exc.errors)
    resume_kind = jd_kind = None
    if resume is not None and resume.data:
        try:
            resume_kind = detect_kind(resume.filename, resume.data)
        except DocumentError as exc:
            errors.append({"field": "resume", "message": exc.message})
    if job_file is not None and job_file.data and not fields.job_description_text:
        try:
            jd_kind = detect_kind(job_file.filename, job_file.data)
        except DocumentError as exc:
            errors.append({"field": "job_description", "message": exc.message})
    if errors:
        raise ScheduleRejected(errors)
    assert resume is not None and start is not None and latest is not None  # noqa: S101

    resume_sha = hashlib.sha256(resume.data).hexdigest()
    jd_sha = hashlib.sha256(job_file.data).hexdigest() if jd_kind and job_file else None
    digest = fingerprint(fields, resume_sha, jd_sha)
    previous = await _existing(db_sessions, admin_id, fields.idempotency_key)
    if previous is not None:
        if previous.request_fingerprint != digest:
            raise IdempotencyConflict
        return ScheduleResult(previous.assignment_id, False)

    stored: list[str] = []
    committed = False
    try:
        resume_name, _ = store_document(document_dir, resume.data, resume_kind or "pdf")
        stored.append(resume_name)
        try:
            resume_doc = await extract_document(document_dir / resume_name, resume_kind, "resume")
        except DocumentError as exc:
            errors.append({"field": "resume", "message": exc.message})
            resume_doc = None
        jd_name = None
        if jd_kind and job_file:
            jd_name, _ = store_document(document_dir, job_file.data, jd_kind)
            stored.append(jd_name)
            try:
                jd_doc = await extract_document(document_dir / jd_name, jd_kind, "job")
                jd_text = jd_doc.text
            except DocumentError as exc:
                errors.append({"field": "job_description", "message": exc.message})
                jd_text = ""
        else:
            try:
                jd_text = validate_text(fields.job_description_text, "job")
            except DocumentError as exc:
                errors.append({"field": "job_description", "message": exc.message})
                jd_text = ""
        if errors or resume_doc is None:
            raise ScheduleRejected(errors)

        instant = now()
        assignment_id = uuid.uuid4()
        try:
            async with db_sessions.begin() as db:
                db.add(
                    InterviewAssignment(
                        id=assignment_id,
                        blueprint_version_id=None,
                        candidate_email=str(fields.candidate_email).strip().casefold(),
                        candidate_name=fields.candidate_name,
                        adjustments={},
                        candidate_instructions="",
                        scheduled_start_at=start,
                        latest_start_at=latest,
                        timezone=fields.timezone,
                        status="assigned",
                        # A throwaway digest nobody holds; the real token is minted at send time.
                        invitation_digest=token_digest(invitation_token()),
                        invitation_expires_at=latest,
                        mail_status="pending",
                        preparation_status="pending",
                        target_minutes=fields.target_minutes,
                        created_at=instant,
                        updated_at=instant,
                    )
                )
                await db.flush()
                db.add(
                    InterviewPreparation(
                        id=uuid.uuid4(),
                        assignment_id=assignment_id,
                        created_by=admin_id,
                        idempotency_key=fields.idempotency_key,
                        request_fingerprint=digest,
                        resume_filename=Path(resume.filename).name[:200],
                        resume_path=resume_name,
                        resume_sha256=resume_sha,
                        resume_pages=resume_doc.pages,
                        resume_text=resume_doc.text,
                        jd_source="upload" if jd_name else "pasted",
                        jd_filename=Path(job_file.filename).name[:200]
                        if jd_name and job_file
                        else None,
                        jd_path=jd_name,
                        jd_sha256=jd_sha,
                        job_description_text=jd_text,
                        interviewer_instructions=fields.interviewer_instructions,
                        target_minutes=fields.target_minutes,
                        created_at=instant,
                    )
                )
                await enqueue(db, assignment_id=assignment_id, kind="prepare", created_by=admin_id)
                await enqueue(db, assignment_id=assignment_id, kind="invite", created_by=admin_id)
                db.add(
                    AssignmentAudit(
                        assignment_id=assignment_id,
                        actor_id=admin_id,
                        action="scheduled",
                        occurred_at=instant,
                        detail={"target_minutes": fields.target_minutes},
                    )
                )
        except IntegrityError:
            # A concurrent submission with the same key won; return it (our files are removed
            # below because this transaction did not commit).
            winner = await _existing(db_sessions, admin_id, fields.idempotency_key)
            if winner is None:
                raise
            if winner.request_fingerprint != digest:
                raise IdempotencyConflict from None
            return ScheduleResult(winner.assignment_id, False)
        committed = True
        return ScheduleResult(assignment_id, True)
    finally:
        if not committed:
            _remove(document_dir, *stored)


# --- invitations --------------------------------------------------------------------


async def send_invitation(
    db_sessions: async_sessionmaker,
    mailer: Mailer,
    assignment_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    action: str,
) -> str:
    """Rotate the invitation token and email it. Returns ``sent`` or ``failed``.

    Rotation and the revocation of every earlier candidate session happen in one transaction,
    so an old link or cookie stops working at the instant a new invitation exists. Only an
    unstarted assignment can be invited, which also prevents resend during an interview.
    """
    token = invitation_token()
    async with db_sessions.begin() as transaction:
        assignment = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
        if (
            assignment is None
            or assignment.status != "assigned"
            or assignment.latest_start_at <= now()
        ):
            raise InvitationUnavailable
        instant = now()
        assignment.invitation_digest = token_digest(token)
        assignment.invitation_expires_at = assignment.latest_start_at
        assignment.mail_status = "sending"
        assignment.updated_at = instant
        await transaction.execute(
            update(CandidateInterviewSession)
            .where(
                CandidateInterviewSession.assignment_id == assignment_id,
                CandidateInterviewSession.revoked_at.is_(None),
            )
            .values(revoked_at=instant)
        )
        transaction.add(
            AssignmentAudit(
                assignment_id=assignment.id,
                actor_id=actor_id,
                action=action,
                occurred_at=instant,
                detail={"mail_status": "sending"},
            )
        )
    async with db_sessions() as db:
        assignment = await db.get(InterviewAssignment, assignment_id)
        version = (
            await db.get(BlueprintVersion, assignment.blueprint_version_id)
            if assignment.blueprint_version_id
            else None
        )
    # Scheduled interviews use a stable generic title so the email never waits for the plan.
    title = "Candidate interview"
    minutes = assignment.target_minutes
    if version is not None and assignment.target_minutes is None:
        content = BlueprintContent.model_validate(version.content)
        title, minutes = content.title, content.target_minutes
    try:
        await mailer.send_invitation(
            assignment.candidate_email,
            token,
            title=title,
            scheduled_at=assignment.scheduled_start_at,
            latest_at=assignment.latest_start_at,
            timezone=assignment.timezone,
            target_minutes=minutes or 30,
        )
        status = "sent"
    except MailDeliveryError:
        status = "failed"
    async with db_sessions.begin() as transaction:
        current = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
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


class InviteHandler:
    """Deliver the invitation without waiting for plan preparation."""

    timeout_secs = 90.0

    def __init__(self, db_sessions: async_sessionmaker, mailer_provider) -> None:
        self.db_sessions = db_sessions
        self.mailer = mailer_provider

    async def run(self, job: ClaimedJob) -> None:
        async with self.db_sessions.begin() as db:
            await verify_claim(db, job)
            assignment = await db.get(InterviewAssignment, job.assignment_id)
            if assignment is None or assignment.status != "assigned":
                raise JobCancelled("assignment_unavailable")
            if assignment.mail_status == "sent":
                # An earlier run already delivered it; do not rotate the link again.
                return
        try:
            status = await send_invitation(
                self.db_sessions,
                self.mailer(),
                job.assignment_id,
                actor_id=job.created_by,
                action="invite_sent",
            )
        except InvitationUnavailable:
            raise JobCancelled("assignment_unavailable") from None
        if status != "sent":
            raise JobError("smtp_failed")

    async def exhausted(self, job: ClaimedJob, code: str) -> None:
        # mail_status already says "failed"; the administrator can use Resend.
        return None
