"""Administrator actions and read models for assignments (scheduled interviews).

Every command here is one short transaction that locks the assignment row first, checks that it
is still unstarted, and writes its audit row in the same transaction. Callers wake the job runner
after a command that enqueued work. These functions raise ``HTTPException`` for refusals so the
HTTP layer stays a thin mapping.
"""

import uuid
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.jobs import enqueue
from rehearse_ai.models import (
    AssignmentAudit,
    CandidateSession,
    InterviewAssignment,
    InterviewJob,
    InterviewPlanVersion,
    InterviewPreparation,
    utcnow,
)


def _local(instant, zone: str) -> str:
    return instant.astimezone(ZoneInfo(zone)).isoformat()


async def plan_version_detail(db: AsyncSession, version_id: uuid.UUID) -> dict:
    item = await db.get(InterviewPlanVersion, version_id)
    if item is None:
        raise HTTPException(404, "Plan version not found")
    return {"id": str(item.id), "version": item.version, "content": item.content}


async def list_assignments(db: AsyncSession) -> list[dict]:
    rows = (
        await db.scalars(
            select(InterviewAssignment).order_by(InterviewAssignment.created_at.desc()).limit(100)
        )
    ).all()
    # The most recent change request per assignment, so the board can show its progress.
    latest_revision: dict[uuid.UUID, InterviewJob] = {}
    if rows:
        for job in await db.scalars(
            select(InterviewJob)
            .where(
                InterviewJob.kind == "revise",
                InterviewJob.assignment_id.in_([item.id for item in rows]),
            )
            .order_by(InterviewJob.created_at.desc())
        ):
            latest_revision.setdefault(job.assignment_id, job)
    return [
        {
            "id": str(item.id),
            # Compatibility: the JSON key keeps its original name for the admin pages.
            "blueprint_version_id": str(item.plan_version_id) if item.plan_version_id else None,
            "revision": (
                {
                    "status": latest_revision[item.id].status,
                    "error": latest_revision[item.id].last_error,
                }
                if item.id in latest_revision
                else None
            ),
            "candidate_email": item.candidate_email,
            "candidate_name": item.candidate_name,
            "status": item.status,
            "mail_status": item.mail_status,
            "preparation_status": item.preparation_status,
            "preparation_error": item.preparation_error,
            "scheduled_start_at": _local(item.scheduled_start_at, item.timezone),
            "latest_start_at": _local(item.latest_start_at, item.timezone),
            "timezone": item.timezone,
        }
        for item in rows
    ]


async def assignment_detail(db: AsyncSession, assignment_id: uuid.UUID) -> dict:
    item = await db.get(InterviewAssignment, assignment_id)
    if item is None:
        raise HTTPException(404, "Assignment not found")
    prep = await db.scalar(
        select(InterviewPreparation).where(InterviewPreparation.assignment_id == item.id)
    )
    jobs = (
        await db.scalars(
            select(InterviewJob)
            .where(InterviewJob.assignment_id == item.id)
            .order_by(InterviewJob.created_at.desc())
            .limit(20)
        )
    ).all()
    versions: list[InterviewPlanVersion] = []
    if prep is not None and prep.plan_id is not None:
        versions = list(
            (
                await db.scalars(
                    select(InterviewPlanVersion)
                    .where(InterviewPlanVersion.plan_id == prep.plan_id)
                    .order_by(InterviewPlanVersion.version)
                )
            ).all()
        )
    return {
        "id": str(item.id),
        "blueprint_version_id": str(item.plan_version_id) if item.plan_version_id else None,
        "candidate_email": item.candidate_email,
        "candidate_name": item.candidate_name,
        "focus_areas": item.adjustments.get("focus_areas", []),
        "candidate_instructions": item.candidate_instructions,
        "scheduled_start_at": _local(item.scheduled_start_at, item.timezone),
        "latest_start_at": _local(item.latest_start_at, item.timezone),
        "timezone": item.timezone,
        "status": item.status,
        "mail_status": item.mail_status,
        "preparation_status": item.preparation_status,
        "preparation_error": item.preparation_error,
        "target_minutes": item.target_minutes,
        # Inputs are described, never returned: the résumé and instructions stay private.
        "preparation": None
        if prep is None
        else {
            "resume_filename": prep.resume_filename,
            "job_description_source": prep.jd_source,
            "job_description_filename": prep.jd_filename,
            "has_private_instructions": bool(prep.interviewer_instructions),
        },
        "plan_versions": [
            {"id": str(v.id), "version": v.version, "published_at": v.published_at.isoformat()}
            for v in versions
        ],
        "jobs": [
            {
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "error": job.last_error,
                "updated_at": job.updated_at.isoformat(),
            }
            for job in jobs
        ],
    }


def _audit(assignment_id: uuid.UUID, admin_id: uuid.UUID, action: str) -> AssignmentAudit:
    return AssignmentAudit(
        assignment_id=assignment_id,
        actor_id=admin_id,
        action=action,
        occurred_at=utcnow(),
        detail={},
    )


async def request_plan_revision(
    db_sessions: async_sessionmaker[AsyncSession],
    assignment_id: uuid.UUID,
    admin_id: uuid.UUID,
    request_text: str,
) -> None:
    """Queue a natural-language change to a ready plan. The plan in use is untouched."""
    text = " ".join(request_text.split())
    try:
        async with db_sessions.begin() as transaction:
            item = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
            if item is None or item.status != "assigned":
                raise HTTPException(409, "Only an unstarted interview can be changed")
            if item.preparation_status != "ready" or item.plan_version_id is None:
                raise HTTPException(409, "The plan must be ready before requesting changes")
            await enqueue(
                transaction,
                assignment_id=item.id,
                kind="revise",
                created_by=admin_id,
                payload={"request": text, "base_version_id": str(item.plan_version_id)},
            )
            transaction.add(_audit(item.id, admin_id, "revision_requested"))
            await transaction.flush()
    except IntegrityError:
        raise HTTPException(409, "A change is already being applied") from None


async def retry_preparation(
    db_sessions: async_sessionmaker[AsyncSession], assignment_id: uuid.UUID, admin_id: uuid.UUID
) -> None:
    try:
        async with db_sessions.begin() as transaction:
            item = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
            if (
                item is None
                or item.status != "assigned"
                or item.preparation_status != "failed"
                or item.plan_version_id is not None
            ):
                raise HTTPException(409, "Preparation can only be retried after it fails")
            item.preparation_status = "pending"
            item.preparation_error = None
            item.updated_at = utcnow()
            await enqueue(transaction, assignment_id=item.id, kind="prepare", created_by=admin_id)
            transaction.add(_audit(item.id, admin_id, "preparation_retried"))
            await transaction.flush()
    except IntegrityError:
        raise HTTPException(409, "Preparation is already queued") from None


async def cancel_assignment(
    db_sessions: async_sessionmaker[AsyncSession], assignment_id: uuid.UUID, admin_id: uuid.UUID
) -> None:
    """Cancel an unstarted assignment and revoke every candidate session for it."""
    async with db_sessions.begin() as transaction:
        item = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
        if item is None or item.status != "assigned":
            raise HTTPException(409, "Only an unstarted assignment can be cancelled")
        item.status = "cancelled"
        item.updated_at = utcnow()
        open_sessions = await transaction.scalars(
            select(CandidateSession).where(
                CandidateSession.assignment_id == assignment_id,
                CandidateSession.revoked_at.is_(None),
            )
        )
        for session in open_sessions:
            session.revoked_at = utcnow()
        transaction.add(_audit(assignment_id, admin_id, "cancelled"))
