"""Administrator read models over conversation records (attempts) and their transcripts."""

import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from rehearse_ai.models import Account, InterviewAssignment, InterviewRecord, TranscriptEntry
from rehearse_ai.records.store import TERMINAL

PAGE_SIZE = 20


async def accessible_record(
    db: AsyncSession, record_id: uuid.UUID, user: Account, *, admin: bool
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
        data["coverage"] = record.coverage_summary
        data["plan_version_id"] = str(record.plan_version_id) if record.plan_version_id else None
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
        select(InterviewRecord, Account.email, InterviewAssignment.candidate_email)
        .outerjoin(Account, Account.id == InterviewRecord.candidate_id)
        .outerjoin(InterviewAssignment, InterviewAssignment.id == InterviewRecord.assignment_id)
    )
    if candidate_id is not None:
        query = query.where(InterviewRecord.candidate_id == candidate_id)
    if candidate_email:
        pattern = candidate_email.strip().casefold()
        query = query.where(
            or_(
                Account.email.contains(pattern, autoescape=True),
                InterviewAssignment.candidate_email.contains(pattern, autoescape=True),
            )
        )
    if status:
        query = query.where(InterviewRecord.status == status)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(InterviewRecord.created_at.desc(), InterviewRecord.id.desc())
            .offset((page_number - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    return {
        "items": [
            record_data(row, account_email or invite_email, admin=admin)
            for row, account_email, invite_email in rows
        ],
        "total": total,
        "page": page_number,
        "page_size": PAGE_SIZE,
    }


async def record_with_address(db: AsyncSession, record: InterviewRecord) -> dict:
    """One record for the admin detail view, with the candidate's email address."""
    if record.assignment_id:
        assignment = await db.get(InterviewAssignment, record.assignment_id)
        address = assignment.candidate_email
    else:
        candidate = await db.get(Account, record.candidate_id)
        address = candidate.email
    return record_data(record, address, admin=True)


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
