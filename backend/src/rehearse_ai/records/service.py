"""Rules for the one interview image, Conversation PDF access and failed attempt starts.

These are the state changes on a conversation record that are not event persistence (that is
``records.store``). Refusals raise ``HTTPException`` so the HTTP layer stays a thin mapping.
"""

import asyncio
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.database.models import InterviewAssignment, InterviewRecord, utcnow
from rehearse_ai.records.images import InvalidSnapshot, save_snapshot
from rehearse_ai.records.store import TERMINAL, InterviewStore


async def accept_interview_image(
    store: InterviewStore,
    *,
    record_id: uuid.UUID,
    assignment_id: uuid.UUID,
    kind: str,
    raw: bytes,
) -> str:
    """Store the one image of an in-progress attempt; returns "saved" or "already_saved".

    The row is locked so concurrent uploads cannot both write. Once an image exists a second
    request is answered "already_saved" and never replaces it. The file is decoded, validated and
    re-encoded off the event loop before the row is marked available.
    """
    async with store.db_sessions.begin() as transaction:
        current = await transaction.get(InterviewRecord, record_id, with_for_update=True)
        if current is None or current.assignment_id != assignment_id:
            raise HTTPException(404, "Interview not found")
        if current.snapshot_status == "available":
            return "already_saved"
        if current.status != "in_progress" or current.snapshot_status != "pending":
            raise HTTPException(409, "An image is no longer accepted")
        try:
            await asyncio.to_thread(save_snapshot, raw, store.snapshot_path(record_id), kind)
        except InvalidSnapshot as exc:
            raise HTTPException(422, str(exc)) from exc
        current.snapshot_status = "available"
        current.snapshot_kind = kind
        current.snapshot_captured_at = utcnow()
    store.snapshot_ready(record_id)
    return "saved"


async def pdf_for_download(store: InterviewStore, record: InterviewRecord) -> Path:
    """The Conversation PDF file for an ended attempt, or an explanation of why not."""
    if record.status not in TERMINAL:
        raise HTTPException(409, "Available after the interview ends")
    path = store.pdf_path(record.id)
    if record.pdf_status == "available" and not path.is_file():
        async with store.db_sessions.begin() as transaction:
            await transaction.execute(
                update(InterviewRecord)
                .where(InterviewRecord.id == record.id, InterviewRecord.pdf_status == "available")
                .values(pdf_status="failed", pdf_error="MissingFile")
            )
        raise HTTPException(409, "PDF file is missing; refresh to regenerate it")
    if record.pdf_status != "available":
        raise HTTPException(409, "PDF is not available yet")
    return path


async def request_pdf_regeneration(store: InterviewStore, record_id: uuid.UUID) -> None:
    """Re-queue a Conversation PDF, only after a failed generation."""
    async with store.db_sessions.begin() as transaction:
        result = await transaction.execute(
            update(InterviewRecord)
            .where(
                InterviewRecord.id == record_id,
                InterviewRecord.status.in_(TERMINAL),
                InterviewRecord.pdf_status == "failed",
            )
            .values(pdf_status="pending", pdf_error=None)
        )
    if result.rowcount != 1:
        raise HTTPException(409, "PDF regeneration is only available after failure")
    if not store.schedule_pdf(record_id):
        raise HTTPException(503, "PDF queue is full; restart to retry pending generation")


async def fail_attempt_start(
    db_sessions: async_sessionmaker[AsyncSession],
    store: InterviewStore,
    *,
    record_id: uuid.UUID,
    assignment_id: uuid.UUID,
    reason: str,
) -> None:
    """The voice connection could not be established: the attempt ends as failed.

    The assignment is marked failed too (an attempt is single-use) and a Conversation PDF is
    still produced from whatever was recorded.
    """
    async with db_sessions.begin() as transaction:
        record = await transaction.get(InterviewRecord, record_id)
        record.status = "failed"
        record.termination_reason = reason
        record.ended_at = utcnow()
        record.pdf_status = "pending"
        assigned = await transaction.get(InterviewAssignment, assignment_id)
        assigned.status = "failed"
    store.schedule_pdf(record_id)
