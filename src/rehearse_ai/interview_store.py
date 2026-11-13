"""Ordered, bounded persistence of server-owned interview events."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.accounts_db import Candidate, InterviewRecord, TranscriptEntry
from rehearse_ai.transcript_pdf import build_pdf

LOGGER = logging.getLogger(__name__)
TERMINAL = {"completed", "ended_early", "interrupted", "failed"}


def event_time(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(event["timestamp_utc"])


class InterviewStore:
    def __init__(
        self,
        db_sessions: async_sessionmaker[AsyncSession],
        logs: Path,
        pdf_dir: Path,
        on_failure: Callable[[str], Awaitable[None]] | None = None,
        snapshot_dir: Path | None = None,
    ) -> None:
        self.db_sessions = db_sessions
        self.logs = logs
        self.pdf_dir = pdf_dir
        self.snapshot_dir = snapshot_dir or pdf_dir / "snapshots"
        self.on_failure = on_failure
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self.pdf_jobs: asyncio.Queue[uuid.UUID] = asyncio.Queue(maxsize=64)
        self.failed_ids: set[str] = set()
        self._event_task: asyncio.Task[None] | None = None
        self._pdf_task: asyncio.Task[None] | None = None
        self._reconciling = False
        self._snapshot_events: dict[uuid.UUID, asyncio.Event] = {}

    async def start(self) -> None:
        self._reconciling = True
        try:
            await self.reconcile()
        finally:
            self._reconciling = False
        self._event_task = asyncio.create_task(self._write_events())
        self._pdf_task = asyncio.create_task(self._make_pdfs())
        async with self.db_sessions.begin() as db:
            available = (
                await db.scalars(
                    select(InterviewRecord.id).where(InterviewRecord.pdf_status == "available")
                )
            ).all()
            for record_id in available:
                if not self.pdf_path(record_id).is_file():
                    await db.execute(
                        update(InterviewRecord)
                        .where(InterviewRecord.id == record_id)
                        .values(pdf_status="pending", pdf_error=None)
                    )
            await db.execute(
                update(InterviewRecord)
                .where(InterviewRecord.pdf_status == "generating")
                .values(pdf_status="pending")
            )
            pending = (
                await db.scalars(
                    select(InterviewRecord.id).where(InterviewRecord.pdf_status == "pending")
                )
            ).all()
        for record_id in pending:
            await self.pdf_jobs.put(record_id)

    async def stop(self) -> None:
        try:
            await asyncio.wait_for(self.events.join(), timeout=15)
            await asyncio.wait_for(self.pdf_jobs.join(), timeout=30)
        except TimeoutError:
            LOGGER.error("Persistence flush timed out; raw logs and PDF jobs remain for restart")
        for task in (self._event_task, self._pdf_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def create_attempt(
        self, candidate_id: uuid.UUID, build_id: str, prompt_version: str
    ) -> uuid.UUID:
        record_id = uuid.uuid4()
        async with self.db_sessions.begin() as db:
            db.add(
                InterviewRecord(
                    id=record_id,
                    candidate_id=candidate_id,
                    created_at=datetime.now(UTC),
                    status="starting",
                    build_id=build_id,
                    prompt_version=prompt_version,
                    last_event_sequence=0,
                    pdf_status="not_ready",
                    snapshot_status="not_started",
                )
            )
        return record_id

    def enqueue(self, event: dict[str, Any]) -> bool:
        session_id = event["session_id"]
        if session_id in self.failed_ids:
            return False
        try:
            self.events.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.failed_ids.add(session_id)
            LOGGER.error("Interview persistence queue full for session %s", session_id)
            return False

    async def _notify_failure(self, session_id: str) -> None:
        if self.on_failure:
            await self.on_failure(session_id)

    async def _write_events(self) -> None:
        while True:
            event = await self.events.get()
            try:
                session_id = event["session_id"]
                await self.apply_event(event)
            except Exception:
                session_id = event["session_id"]
                if session_id not in self.failed_ids:
                    self.failed_ids.add(session_id)
                    LOGGER.exception("Interview persistence failed for session %s", session_id)
                    try:
                        await self._notify_failure(session_id)
                    except Exception:
                        LOGGER.exception("Could not notify browser of persistence failure")
            finally:
                self.events.task_done()

    async def apply_event(self, event: dict[str, Any]) -> None:
        record_id = uuid.UUID(event["session_id"])
        seq = int(event["sequence"])
        pdf_needed = False
        async with self.db_sessions.begin() as db:
            record = await db.get(InterviewRecord, record_id, with_for_update=True)
            if record is None or seq <= record.last_event_sequence:
                return
            if seq != record.last_event_sequence + 1:
                raise ValueError(f"Missing event before sequence {seq} for {record_id}")
            kind = event["event"]
            timestamp = event_time(event)
            utterance_id = event.get("utterance_id")
            if record.status in TERMINAL:
                record.last_event_sequence = seq
                return
            if kind == "session_started" and record.status == "starting":
                record.status = "in_progress"
                record.started_at = timestamp
                record.snapshot_status = "pending"
            elif kind == "candidate_final":
                boundary_id = event.get("boundary_frame_id")
                turn_number = event.get("turn_number")
                if isinstance(boundary_id, int) and boundary_id > 0:
                    source_id = f"{record_id}:boundary:{boundary_id}"
                elif isinstance(turn_number, int) and turn_number > 0:
                    source_id = f"{record_id}:turn:{turn_number}"
                else:
                    source_id = f"{record_id}:event:{seq}"
                existing = await db.scalar(
                    select(TranscriptEntry.id).where(
                        TranscriptEntry.interview_id == record_id,
                        TranscriptEntry.source_event_id == source_id,
                    )
                )
                if existing is None:
                    db.add(
                        TranscriptEntry(
                            interview_id=record_id,
                            source_event_id=source_id,
                            sequence=seq,
                            speaker="candidate",
                            text=event.get("text") or "",
                            timestamp_utc=timestamp,
                            status="machine_transcription",
                            interrupted=False,
                        )
                    )
            elif kind == "agent_prepared" and utterance_id is not None:
                existing = await db.scalar(
                    select(TranscriptEntry.id).where(
                        TranscriptEntry.interview_id == record_id,
                        TranscriptEntry.utterance_id == utterance_id,
                    )
                )
                if existing is None:
                    db.add(
                        TranscriptEntry(
                            interview_id=record_id,
                            source_event_id=f"{record_id}:utterance:{utterance_id}",
                            sequence=seq,
                            utterance_id=utterance_id,
                            speaker="agent",
                            text=event.get("text") or "",
                            timestamp_utc=timestamp,
                            status="prepared",
                            interrupted=False,
                        )
                    )
            elif (
                kind
                in {
                    "agent_output_started",
                    "agent_delivery_reported",
                    "agent_interrupted",
                    "agent_cancellation",
                    "response_error",
                }
                and utterance_id is not None
            ):
                entry = await db.scalar(
                    select(TranscriptEntry).where(
                        TranscriptEntry.interview_id == record_id,
                        TranscriptEntry.utterance_id == utterance_id,
                    )
                )
                if entry is not None:
                    if kind == "agent_output_started" and entry.status == "prepared":
                        entry.status = "output_started"
                        entry.output_started_at = timestamp
                    elif kind == "agent_delivery_reported" and entry.status == "output_started":
                        entry.status = "output_completed"
                        entry.output_ended_at = timestamp
                    elif kind == "agent_interrupted" and entry.status not in {
                        "output_completed",
                        "failed",
                    }:
                        entry.status = "interrupted" if entry.output_started_at else "cancelled"
                        entry.interrupted = True
                        entry.output_ended_at = timestamp
                    elif kind == "agent_cancellation" and entry.status in {
                        "prepared",
                        "output_started",
                    }:
                        entry.status = "interrupted" if entry.output_started_at else "cancelled"
                        entry.interrupted = True
                        entry.output_ended_at = timestamp
                    elif kind == "response_error" and entry.status in {
                        "prepared",
                        "output_started",
                    }:
                        entry.status = "failed"
                        entry.output_ended_at = timestamp
            if kind == "interview_complete" and record.status == "in_progress":
                record.status = "completed"
                record.termination_reason = "normal_completion"
                record.ended_at = timestamp
                pdf_needed = True
            elif kind == "interview_ended_early" and record.status == "in_progress":
                record.status = "ended_early"
                record.termination_reason = "spoken_early_end"
                record.ended_at = timestamp
                pdf_needed = True
            elif kind == "session_ended" and record.status not in TERMINAL:
                record.status = (
                    "failed"
                    if event.get("session_end_kind") == "pipeline_failed"
                    else "interrupted"
                )
                record.termination_reason = (
                    "pipeline_failed" if record.status == "failed" else "session_disconnected"
                )
                record.ended_at = timestamp
                pdf_needed = True
            if pdf_needed:
                record.pdf_status = "pending"
            record.last_event_sequence = seq
        if pdf_needed and not self._reconciling:
            self.schedule_pdf(record_id)

    async def reconcile(self) -> None:
        """Replay only known attempts, then mark abandoned nonterminal attempts interrupted."""
        async with self.db_sessions() as db:
            records = (await db.scalars(select(InterviewRecord))).all()
            checkpoints = [(record.id, record.last_event_sequence) for record in records]
        for record_id, last in checkpoints:
            path = self.logs / f"{record_id}.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        LOGGER.error(
                            "Invalid raw log tail for session %s; leaving source unchanged",
                            record_id,
                        )
                        break
                    if (
                        event.get("session_id") == str(record_id)
                        and event.get("sequence", 0) > last
                    ):
                        await self.apply_event(event)
            async with self.db_sessions.begin() as db:
                record = await db.get(InterviewRecord, record_id, with_for_update=True)
                if record is not None and record.status not in TERMINAL:
                    record.status = "interrupted"
                    record.termination_reason = "server_restart"
                    record.ended_at = datetime.now(UTC)
                    record.pdf_status = "pending"
        # Pending PDFs are queued by start() after replay, including any new terminal record.

    def schedule_pdf(self, record_id: uuid.UUID) -> bool:
        try:
            self.pdf_jobs.put_nowait(record_id)
            return True
        except asyncio.QueueFull:
            LOGGER.error("PDF queue full; interview %s remains pending", record_id)
            return False

    async def _make_pdfs(self) -> None:
        while True:
            record_id = await self.pdf_jobs.get()
            try:
                await self.generate_pdf(record_id)
            except Exception:
                LOGGER.exception("PDF generation failed for interview %s", record_id)
            finally:
                self.pdf_jobs.task_done()

    async def generate_pdf(self, record_id: uuid.UUID) -> bool:
        async with self.db_sessions() as db:
            snapshot_state = await db.scalar(
                select(InterviewRecord.snapshot_status).where(InterviewRecord.id == record_id)
            )
        if snapshot_state == "pending":
            try:
                await asyncio.wait_for(
                    self._snapshot_events.setdefault(record_id, asyncio.Event()).wait(), 1.5
                )
            except TimeoutError:
                pass
        async with self.db_sessions.begin() as db:
            result = await db.execute(
                update(InterviewRecord)
                .where(
                    InterviewRecord.id == record_id,
                    InterviewRecord.status.in_(TERMINAL),
                    InterviewRecord.pdf_status.in_(["pending", "failed"]),
                )
                .values(pdf_status="generating", pdf_error=None)
            )
            claimed = result.rowcount == 1
            if claimed:
                await db.execute(
                    update(InterviewRecord)
                    .where(
                        InterviewRecord.id == record_id,
                        InterviewRecord.snapshot_status == "pending",
                    )
                    .values(snapshot_status="unavailable")
                )
        if not claimed:
            self._snapshot_events.pop(record_id, None)
            return False
        try:
            async with self.db_sessions() as db:
                record = await db.get(InterviewRecord, record_id)
                candidate = await db.get(Candidate, record.candidate_id)
                entries = (
                    await db.scalars(
                        select(TranscriptEntry)
                        .where(TranscriptEntry.interview_id == record_id)
                        .order_by(TranscriptEntry.sequence)
                    )
                ).all()
                data = {
                    "id": str(record.id),
                    "created_at": record.created_at,
                    "started_at": record.started_at,
                    "ended_at": record.ended_at,
                    "status": record.status,
                    "termination_reason": record.termination_reason,
                    "candidate_id": str(record.candidate_id),
                    "candidate_email": candidate.email,
                    "snapshot_captured_at": record.snapshot_captured_at,
                }
                snapshot_path = self.snapshot_path(record_id)
                data["snapshot_path"] = (
                    snapshot_path
                    if record.snapshot_status == "available" and snapshot_path.is_file()
                    else None
                )
                items = [
                    {
                        "speaker": entry.speaker,
                        "text": entry.text,
                        "timestamp_utc": entry.timestamp_utc,
                        "status": entry.status,
                        "interrupted": entry.interrupted,
                    }
                    for entry in entries
                ]
            target = self.pdf_path(record_id)
            await asyncio.to_thread(build_pdf, target, data, items)
        except Exception as exc:
            async with self.db_sessions.begin() as db:
                await db.execute(
                    update(InterviewRecord)
                    .where(InterviewRecord.id == record_id)
                    .values(
                        pdf_status="failed",
                        pdf_error=type(exc).__name__,
                    )
                )
            LOGGER.exception("PDF generation error for interview %s", record_id)
            self._snapshot_events.pop(record_id, None)
            return False
        async with self.db_sessions.begin() as db:
            await db.execute(
                update(InterviewRecord)
                .where(InterviewRecord.id == record_id)
                .values(
                    pdf_status="available",
                    pdf_error=None,
                )
            )
        self._snapshot_events.pop(record_id, None)
        return True

    def snapshot_path(self, record_id: uuid.UUID) -> Path:
        return self.snapshot_dir / f"{record_id}.jpg"

    def snapshot_ready(self, record_id: uuid.UUID) -> None:
        self._snapshot_events.setdefault(record_id, asyncio.Event()).set()

    def pdf_path(self, record_id: uuid.UUID) -> Path:
        return self.pdf_dir / f"{record_id}.pdf"
