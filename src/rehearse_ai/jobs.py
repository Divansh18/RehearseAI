"""A small PostgreSQL-backed job runner for plan preparation and invitation delivery.

Design:
- Jobs are rows. They are written in the same transaction as the thing that needs them,
  before any external call is made.
- Claiming uses ``FOR UPDATE SKIP LOCKED`` and a per-claim random token. Every later state
  change is fenced by that token, so a worker that lost its lease cannot overwrite a newer
  claim (stale results are rejected).
- Handlers run with no database transaction held open: they open short transactions
  themselves around the reads and writes, never around extraction, model or SMTP calls.
- Failures retry with backoff up to ``max_attempts``; the last failure is stored as a short
  error code, never as document or model content.
- Restart recovery: ``recover()`` re-queues jobs a previous process left ``running``. This
  assumes one server process, which the loopback-only local deployment guarantees. Leases
  additionally let another process reclaim a job that has stopped making progress.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.accounts_db import InterviewJob

LOGGER = logging.getLogger(__name__)
ERROR_MAX = 64


class JobError(Exception):
    """A failure with a short, content-free code. Retried unless ``retryable`` is false."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code[:ERROR_MAX]
        self.retryable = retryable


class JobCancelled(Exception):
    """The job no longer applies (assignment cancelled or started, or work already done)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:ERROR_MAX]


class StaleJobError(Exception):
    """This worker no longer owns the job; its result must be discarded."""


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    assignment_id: uuid.UUID
    kind: str
    attempts: int
    max_attempts: int
    claim_token: str
    payload: dict
    created_by: uuid.UUID


class JobHandler(Protocol):
    timeout_secs: float

    async def run(self, job: ClaimedJob) -> None: ...

    async def exhausted(self, job: ClaimedJob, code: str) -> None: ...


def utcnow() -> datetime:
    return datetime.now(UTC)


async def verify_claim(db: AsyncSession, job: ClaimedJob) -> InterviewJob:
    """Lock the job row and raise unless this worker still owns it."""
    row = await db.get(InterviewJob, job.id, with_for_update=True)
    if row is None or row.status != "running" or row.claim_token != job.claim_token:
        raise StaleJobError
    return row


async def enqueue(
    db: AsyncSession,
    *,
    assignment_id: uuid.UUID,
    kind: str,
    created_by: uuid.UUID,
    payload: dict | None = None,
    max_attempts: int = 3,
) -> InterviewJob:
    """Add a queued job inside the caller's transaction."""
    instant = utcnow()
    job = InterviewJob(
        id=uuid.uuid4(),
        assignment_id=assignment_id,
        kind=kind,
        status="queued",
        attempts=0,
        max_attempts=max_attempts,
        run_after=instant,
        payload=payload or {},
        created_by=created_by,
        created_at=instant,
        updated_at=instant,
    )
    db.add(job)
    return job


class JobRunner:
    def __init__(
        self,
        db_sessions: async_sessionmaker[AsyncSession],
        handlers: dict[str, JobHandler],
        *,
        concurrency: int = 2,
        poll_secs: float = 2.0,
        lease_secs: float = 300.0,
        backoff_secs: tuple[float, ...] = (30.0, 120.0, 300.0),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db_sessions = db_sessions
        self.handlers = handlers
        self.concurrency = concurrency
        self.poll_secs = poll_secs
        self.lease_secs = lease_secs
        self.backoff_secs = backoff_secs
        self.clock = clock
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()

    # --- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        await self.recover()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self, grace_secs: float = 5.0) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop_task:
            await asyncio.gather(self._loop_task, return_exceptions=True)
        if self._running:
            _, pending = await asyncio.wait(self._running, timeout=grace_secs)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def recover(self) -> int:
        """Re-queue work a previous process left running. Safe because there is one process."""
        async with self.db_sessions.begin() as db:
            result = await db.execute(
                update(InterviewJob)
                .where(InterviewJob.status == "running")
                .values(
                    status="queued",
                    claim_token=None,
                    lease_expires_at=None,
                    run_after=self.clock(),
                    updated_at=self.clock(),
                )
            )
        return result.rowcount or 0

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                while len(self._running) < self.concurrency and not self._stopping:
                    job = await self.claim_next()
                    if job is None:
                        break
                    task = asyncio.create_task(self.execute(job))
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
            except Exception:
                LOGGER.exception("Job claim loop error")
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_secs)
            except TimeoutError:
                pass
            self._wake.clear()

    # --- claiming -----------------------------------------------------------------

    async def claim_next(self) -> ClaimedJob | None:
        while True:
            exhausted: ClaimedJob | None = None
            async with self.db_sessions.begin() as db:
                instant = self.clock()
                row = await db.scalar(
                    select(InterviewJob)
                    .where(
                        or_(
                            and_(
                                InterviewJob.status == "queued", InterviewJob.run_after <= instant
                            ),
                            and_(
                                InterviewJob.status == "running",
                                InterviewJob.lease_expires_at < instant,
                            ),
                        )
                    )
                    .order_by(InterviewJob.run_after)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                if row.attempts >= row.max_attempts:
                    # A final attempt died without reporting (crash or lost lease).
                    row.status = "failed"
                    row.last_error = "lease_expired"
                    row.finished_at = instant
                    row.updated_at = instant
                    exhausted = self._snapshot(row)
                else:
                    row.status = "running"
                    row.attempts += 1
                    row.claim_token = uuid.uuid4().hex
                    row.lease_expires_at = instant + timedelta(seconds=self.lease_secs)
                    row.updated_at = instant
                    return self._snapshot(row)
            if exhausted is not None:
                await self._notify_exhausted(exhausted, "lease_expired")

    @staticmethod
    def _snapshot(row: InterviewJob) -> ClaimedJob:
        return ClaimedJob(
            id=row.id,
            assignment_id=row.assignment_id,
            kind=row.kind,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            claim_token=row.claim_token or "",
            payload=dict(row.payload or {}),
            created_by=row.created_by,
        )

    # --- execution ----------------------------------------------------------------

    async def run_once(self) -> bool:
        """Claim and execute one due job. Used by the loop's tests and by manual draining."""
        job = await self.claim_next()
        if job is None:
            return False
        await self.execute(job)
        return True

    async def drain(self, limit: int = 50) -> int:
        count = 0
        while count < limit and await self.run_once():
            count += 1
        return count

    async def execute(self, job: ClaimedJob) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            await self._fail(job, "unknown_job_kind", retryable=False)
            return
        try:
            await asyncio.wait_for(handler.run(job), handler.timeout_secs)
        except asyncio.CancelledError:
            raise  # shutdown: leave the job running; recover() or its lease re-queues it
        except StaleJobError:
            LOGGER.warning("Discarded stale %s job result", job.kind)
        except JobCancelled as exc:
            await self._finish(job, "cancelled", exc.code)
        except JobError as exc:
            await self._fail(job, exc.code, retryable=exc.retryable)
        except TimeoutError:
            await self._fail(job, "timeout", retryable=True)
        except Exception as exc:
            LOGGER.error("Job %s failed with %s", job.kind, type(exc).__name__)
            await self._fail(job, "internal_error", retryable=True)
        else:
            await self._finish(job, "succeeded", None)

    async def _finish(self, job: ClaimedJob, status: str, code: str | None) -> bool:
        instant = self.clock()
        async with self.db_sessions.begin() as db:
            result = await db.execute(
                update(InterviewJob)
                .where(
                    InterviewJob.id == job.id,
                    InterviewJob.claim_token == job.claim_token,
                    InterviewJob.status == "running",
                )
                .values(
                    status=status,
                    last_error=code,
                    finished_at=instant,
                    updated_at=instant,
                    lease_expires_at=None,
                )
            )
        return result.rowcount == 1

    async def _fail(self, job: ClaimedJob, code: str, *, retryable: bool) -> None:
        instant = self.clock()
        if retryable and job.attempts < job.max_attempts:
            delay = self.backoff_secs[min(job.attempts - 1, len(self.backoff_secs) - 1)]
            async with self.db_sessions.begin() as db:
                result = await db.execute(
                    update(InterviewJob)
                    .where(
                        InterviewJob.id == job.id,
                        InterviewJob.claim_token == job.claim_token,
                        InterviewJob.status == "running",
                    )
                    .values(
                        status="queued",
                        claim_token=None,
                        lease_expires_at=None,
                        last_error=code,
                        run_after=instant + timedelta(seconds=delay),
                        updated_at=instant,
                    )
                )
            if result.rowcount != 1:
                LOGGER.warning("Could not re-queue stale %s job", job.kind)
            return
        if await self._finish(job, "failed", code):
            await self._notify_exhausted(job, code)

    async def _notify_exhausted(self, job: ClaimedJob, code: str) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            return
        try:
            await handler.exhausted(job, code)
        except Exception as exc:
            LOGGER.error("Job exhaustion handler failed with %s", type(exc).__name__)
