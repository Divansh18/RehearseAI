"""Application composition: build the services, wire the routers, own the lifespan.

This is the only place that creates the database engine, the job runner, the interview store and
the voice-session registry. Routes reach them through ``ServerContext`` (see ``dependencies``).
Business rules live in the capability packages, not here.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from rehearse_ai.accounts.service import AccountService
from rehearse_ai.config import ConfigurationError, Settings
from rehearse_ai.database.connection import open_database
from rehearse_ai.database.models import (
    Account,
    InterviewAssignment,
    InterviewJob,
    InterviewPlan,
    InterviewRecord,
    utcnow,
)
from rehearse_ai.jobs.runner import JobRunner
from rehearse_ai.operational_log import configure_logging
from rehearse_ai.operational_log import event as log_event
from rehearse_ai.plans.preparation import (
    OpenAIPlanGenerator,
    PrepareHandler,
    ReviseHandler,
)
from rehearse_ai.records.store import InterviewStore
from rehearse_ai.scheduling.service import InviteHandler
from rehearse_ai.server import pages
from rehearse_ai.server.build import prompt_version, source_build_id
from rehearse_ai.server.context import ServerContext
from rehearse_ai.server.request_logging import RequestLoggingMiddleware
from rehearse_ai.server.routes import assignments, attempts, auth, candidate, records
from rehearse_ai.voice.registry import LiveSessionRegistry

LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    build_id = source_build_id()
    engine, db_sessions = open_database(settings.auth.database_url, pool_pre_ping=True)
    accounts = AccountService(settings.auth, db_sessions)
    webrtc = SmallWebRTCRequestHandler()
    live = LiveSessionRegistry()
    store = InterviewStore(
        db_sessions,
        settings.session_dir,
        settings.pdf_dir,
        live.report_storage_failure,
        settings.snapshot_dir,
    )

    def current_generator():
        # Looked up at call time so tests can substitute a fake generator on app.state.
        return app.state.plan_generator

    runner = JobRunner(
        db_sessions,
        {
            "prepare": PrepareHandler(db_sessions, current_generator),
            "revise": ReviseHandler(db_sessions, current_generator),
            "invite": InviteHandler(db_sessions, lambda: accounts.mailer),
        },
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with db_sessions() as db:
                await db.execute(select(Account.id).limit(1))
                await db.execute(select(InterviewRecord.id).limit(1))
                await db.execute(select(InterviewPlan.id).limit(1))
                await db.execute(select(InterviewJob.id).limit(1))
        except SQLAlchemyError:
            log_event(LOGGER, "app.startup_failed", level=logging.CRITICAL, reason="database")
            raise RuntimeError(
                "Interview database is unavailable or unmigrated. "
                "Start PostgreSQL and run `uv run alembic upgrade head`."
            ) from None
        async with db_sessions.begin() as db:
            await db.execute(
                update(InterviewAssignment)
                .where(
                    InterviewAssignment.status == "assigned",
                    InterviewAssignment.latest_start_at < utcnow(),
                )
                .values(status="expired", updated_at=utcnow())
            )
        await store.start()
        if settings.job_runner:
            await runner.start()
        log_event(
            LOGGER,
            "app.started",
            host=settings.host,
            port=settings.port,
            environment=settings.auth.environment,
            job_runner=settings.job_runner,
            tts_provider=settings.tts_provider,
            session_dir=str(settings.session_dir.resolve()),
            pdf_dir=str(settings.pdf_dir.resolve()),
            snapshot_dir=str(settings.snapshot_dir.resolve()),
            document_dir=str(settings.document_dir.resolve()),
            prompt_version=prompt_version(),
        )
        yield
        log_event(LOGGER, "app.stopping", running_voice_sessions=len(live.sessions))
        await runner.stop()
        await live.shutdown()
        await store.stop()
        await webrtc.close()
        await engine.dispose()
        log_event(LOGGER, "app.stopped")

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(RequestLoggingMiddleware)
    app.state.context = ServerContext(
        settings=settings,
        build_id=build_id,
        prompt_version=prompt_version(),
        db_sessions=db_sessions,
        accounts=accounts,
        store=store,
        jobs=runner,
        live=live,
        webrtc=webrtc,
    )
    # Public handles that tests and operators use directly.
    app.state.build_id = build_id
    app.state.accounts = accounts
    app.state.interview_store = store
    app.state.active_sessions = live.sessions
    app.state.jobs = runner
    app.state.plan_generator = OpenAIPlanGenerator(settings.openai_api_key, settings.llm_model)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return JSONResponse({"detail": fields}, status_code=422)

    for router in (
        pages.router,
        auth.router,
        candidate.router,
        assignments.router,
        records.router,
        attempts.router,
    ):
        app.include_router(router)
    return app


def main() -> None:
    try:
        settings = Settings.load()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    logging_handle = configure_logging(settings.log, build_id=source_build_id())
    try:
        app = create_app(settings)
        print(f"RehearseAI build {app.state.build_id}", flush=True)
        uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)
    finally:
        logging_handle.shutdown()
