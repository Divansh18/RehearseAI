"""Operational logging: structure, correlation, privacy, rotation, overflow, shutdown, resilience.

Everything here uses invented data. Where a test needs a "secret" it is a recognisable marker so
that its absence from every written file can be asserted.
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rehearse_ai.config import ConfigurationError, LogSettings, Settings, repository_root
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.operational_log import bind, bound, configure_logging, event
from rehearse_ai.operational_log import configuration as configuration_module
from rehearse_ai.operational_log.configuration import resolve_directory
from rehearse_ai.operational_log.formatter import JsonFormatter
from rehearse_ai.operational_log.handlers import (
    BoundedQueueHandler,
    BoundedQueueListener,
    PrunedRotatingFileHandler,
)
from rehearse_ai.operational_log.redaction import REDACTED, clean, clean_fields, is_sensitive_name
from rehearse_ai.server.request_logging import RequestLoggingMiddleware

LOGGER = logging.getLogger("rehearse_ai.tests.operational")
INTERVIEW_LOGGER = logging.getLogger("rehearse_ai.interview.tests")
SECRET = "SECRET-MARKER-DO-NOT-LOG"


def settings_for(tmp_path: Path, **overrides) -> LogSettings:
    values = {"directory": tmp_path / "logs", "console": False, "level": "DEBUG"}
    values.update(overrides)
    return LogSettings(**values)


@pytest.fixture
def logging_in(tmp_path):
    handles = []

    def start(**overrides):
        handle = configure_logging(settings_for(tmp_path, **overrides), build_id="test-build")
        handles.append(handle)
        return handle

    yield start
    for handle in handles:
        handle.shutdown()


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def application_log(tmp_path: Path) -> list[dict]:
    return lines(tmp_path / "logs" / "application.jsonl")


def everything_written(tmp_path: Path) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "logs").glob("*"))


# --- structure and correlation ---


def test_events_are_json_with_one_envelope(tmp_path, logging_in):
    handle = logging_in()
    event(LOGGER, "job.succeeded", job_kind="prepare", duration_ms=12)
    handle.shutdown()
    record = next(row for row in application_log(tmp_path) if row["event"] == "job.succeeded")
    assert record["level"] == "INFO"
    assert record["category"] == "job"
    assert record["logger"] == "rehearse_ai.tests.operational"
    assert record["service"] == "rehearse-ai" and record["build_id"] == "test-build"
    assert record["job_kind"] == "prepare" and record["duration_ms"] == 12
    assert record["ts"].endswith("+00:00") and "T" in record["ts"]
    assert record["ts"][19] == "." and len(record["ts"].split(".")[1]) == len("123+00:00")


def test_interview_and_voice_events_go_to_their_own_file(tmp_path, logging_in):
    handle = logging_in()
    event(INTERVIEW_LOGGER, "interview.session_ended")
    event(LOGGER, "app.started")
    handle.shutdown()
    assert [row["event"] for row in lines(tmp_path / "logs" / "interview.jsonl")] == [
        "interview.session_ended"
    ]
    assert "interview.session_ended" not in {r["event"] for r in application_log(tmp_path)}


def test_bound_ids_correlate_every_record_in_scope_including_child_tasks(tmp_path, logging_in):
    handle = logging_in()

    async def scenario():
        with bound(request_id="req-1", assignment_id="asg-1"):
            event(LOGGER, "http.request_rejected", status=404)

            async def child():
                event(LOGGER, "job.started")

            await asyncio.create_task(child())
        event(LOGGER, "app.after_scope")

    asyncio.run(scenario())
    handle.shutdown()
    rows = {row["event"]: row for row in application_log(tmp_path)}
    assert rows["http.request_rejected"]["request_id"] == "req-1"
    assert rows["job.started"]["request_id"] == "req-1"
    assert rows["job.started"]["assignment_id"] == "asg-1"
    assert "request_id" not in rows["app.after_scope"]


def test_a_call_site_may_state_its_own_id_and_cannot_overwrite_the_envelope(tmp_path, logging_in):
    handle = logging_in()
    with bound(record_id="from-context"):
        event(LOGGER, "persistence.queue_full", record_id="explicit", level=logging.ERROR)
        event(LOGGER, "app.started", level="x" and logging.INFO, event="forged", ts="forged")
    handle.shutdown()
    rows = application_log(tmp_path)
    assert (
        next(r for r in rows if r["event"] == "persistence.queue_full")["record_id"] == "explicit"
    )
    forged = next(r for r in rows if r["event"] == "app.started")
    assert forged["ts"] != "forged"


def test_bind_in_a_task_does_not_leak_into_its_parent():
    async def scenario():
        async def child():
            bind(job_id="job-9")
            from rehearse_ai.operational_log.context import current

            return current()

        seen = await asyncio.create_task(child())
        from rehearse_ai.operational_log.context import current

        return seen, current()

    seen, parent = asyncio.run(scenario())
    assert seen == {"job_id": "job-9"} and parent == {}


# --- privacy ---


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "api_key",
        "authorization",
        "cookie",
        "csrf_token",
        "invitation_token",
        "candidate_email",
        "candidate_name",
        "resume_text",
        "job_description",
        "interviewer_instructions",
        "transcript",
        "text",
        "request_body",
        "model_prompt",
        "candidate_text",
        "memory_value",
        "token_digest",
    ],
)
def test_field_names_that_say_content_or_credential_are_never_written(name):
    assert is_sensitive_name(name)
    assert clean_fields({name: SECRET}) == {name: REDACTED}


@pytest.mark.parametrize(
    "name",
    [
        "duration_ms",
        "tts_context_elapsed_ms",
        "tts_context_id",
        "error_code",
        "job_kind",
        "heard_ms",
        "status",
        "reason",
        "record_id",
        "word_count",
    ],
)
def test_diagnostic_field_names_are_kept(name):
    assert not is_sensitive_name(name)


def test_secret_looking_strings_are_scrubbed_from_allowed_fields():
    leaked = {
        "reason": "Bearer abcdef.ghijkl.mnopqr failed",
        "detail": "key sk-live-1234567890abcdef rejected",
        "who": "nyla.example@example.com",
        "blob": "A" * 40,
        "note": "password=hunter2 was used",
    }
    cleaned = clean_fields(leaked)
    assert "abcdef" not in cleaned["reason"] and "sk-live" not in cleaned["detail"]
    assert "@" not in cleaned["who"] and "A" * 40 not in cleaned["blob"]
    assert "hunter2" not in cleaned["note"]


def test_uuid_objects_dates_and_enums_are_rendered_not_replaced_by_their_class_name():
    import uuid
    from datetime import UTC, datetime
    from enum import Enum

    class Kind(Enum):
        PREPARE = "prepare"

    identifier = uuid.uuid4()
    assert clean(identifier) == str(identifier)
    assert clean(datetime(2026, 1, 2, tzinfo=UTC)).startswith("2026-01-02T")
    assert clean(Kind.PREPARE) == "prepare"


def test_uuids_are_kept_and_long_or_nested_values_are_bounded():
    identifier = "0f6b3b9c-52ae-4c43-8d49-7b8e6f8b2a10"
    assert clean(identifier) == identifier
    assert len(clean("x" * 500)) <= 201
    deep = clean({"a": {"b": {"c": "deep"}}})
    assert deep["a"]["b"] == REDACTED
    assert len(clean(list(range(100)))) == 20


def test_an_exception_message_never_reaches_the_log_only_its_class_and_location(
    tmp_path, logging_in
):
    handle = logging_in()
    try:
        raise ValueError(f"candidate résumé says {SECRET}")
    except ValueError:
        event(LOGGER, "job.attempt_failed", level=logging.ERROR, exc_info=True, job_kind="prepare")
    handle.shutdown()
    written = everything_written(tmp_path)
    assert SECRET not in written and "résumé" not in written
    record = next(r for r in application_log(tmp_path) if r["event"] == "job.attempt_failed")
    assert record["error"]["type"] == "ValueError"
    assert any("test_operational_logging.py" in frame for frame in record["error"]["frames"])


def test_a_plain_logging_call_is_kept_but_scrubbed(tmp_path, logging_in):
    handle = logging_in()
    LOGGER.error("provider said Bearer abc.def.ghi to user nyla@example.com")
    handle.shutdown()
    record = next(r for r in application_log(tmp_path) if r["event"] == "log.unstructured")
    assert "abc.def.ghi" not in record["message"] and "nyla@example.com" not in record["message"]


def test_saved_conversation_events_are_mirrored_without_any_text(tmp_path, logging_in):
    handle = logging_in()
    log = SessionLog(tmp_path / "sessions", "0f6b3b9c-52ae-4c43-8d49-7b8e6f8b2a10")
    log.record("agent_interrupted", text=SECRET, heard_ms=310, heard_enough=False, generation=4)
    log.record("unheard_prompt_recovery", candidate_text=SECRET)
    log.record("candidate_final", text=SECRET)  # not mirrored at all
    log.record("decision_proposed", text=SECRET, action="advance")  # not mirrored either
    log.record("interruption_rejected", reason="brief_sound", tentative_ms=210, bot_speaking=True)
    log.record("repeat_requested", text=SECRET)
    log.record(
        "response_error",
        failure_stage="pipeline",
        processor="CartesiaTTSService",
        error_code="tts_provider_response",
        provider_status="429",
    )
    log.close()
    handle.shutdown()
    written = everything_written(tmp_path)
    assert SECRET not in written
    rows = {r["event"]: r for r in lines(tmp_path / "logs" / "interview.jsonl")}
    assert rows["interview.agent_interrupted"]["heard_ms"] == 310
    assert (
        rows["interview.agent_interrupted"]["record_id"] == "0f6b3b9c-52ae-4c43-8d49-7b8e6f8b2a10"
    )
    assert rows["interview.interruption_rejected"]["tentative_ms"] == 210
    assert rows["interview.control"]["control"] == "repeat_requested"
    assert rows["provider.error"]["level"] == "ERROR"
    assert rows["provider.error"]["provider_status"] == "429"
    assert "interview.prompt_replayed" in rows
    assert not {"candidate_final", "decision_proposed"} & {r["event"] for r in rows.values()}
    # The protected conversation file still holds everything, exactly as before.
    assert (
        SECRET in (tmp_path / "sessions" / "0f6b3b9c-52ae-4c43-8d49-7b8e6f8b2a10.jsonl").read_text()
    )


# --- no duplicate handlers or events ---


def test_configuring_twice_replaces_the_handler_and_writes_each_event_once(tmp_path, logging_in):
    logging_in()
    logging_in()
    logger = logging.getLogger("rehearse_ai")
    attached = [h for h in logger.handlers if isinstance(h, BoundedQueueHandler)]
    assert len(attached) == 1
    event(LOGGER, "app.started", marker="once")
    configuration_module._active.shutdown()
    rows = [r for r in application_log(tmp_path) if r.get("marker") == "once"]
    assert len(rows) == 1
    assert not [h for h in logger.handlers if isinstance(h, BoundedQueueHandler)]


def test_shutdown_twice_is_harmless_and_clears_the_active_configuration(logging_in):
    handle = logging_in()
    handle.shutdown()
    handle.shutdown()
    assert configuration_module._active is None


# --- rotation and retention ---


def test_files_rotate_by_size_and_keep_only_the_configured_backups(tmp_path, logging_in):
    handle = logging_in(max_bytes=4000, backups=2, retention_days=30)
    for number in range(400):
        event(LOGGER, "app.started", sequence=number, padding="p" * 60)
    handle.shutdown()
    names = sorted(path.name for path in (tmp_path / "logs").glob("application.jsonl*"))
    assert names == ["application.jsonl", "application.jsonl.1", "application.jsonl.2"]
    for path in (tmp_path / "logs").glob("application.jsonl*"):
        assert path.stat().st_size < 4000 + 400  # one record of slack past the limit
    assert (tmp_path / "logs" / "application.jsonl").stat().st_mode & 0o777 == 0o600


def test_old_rotated_files_are_deleted_by_age_and_the_active_file_never(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    active = directory / "application.jsonl"
    old = directory / "application.jsonl.3"
    recent = directory / "application.jsonl.1"
    for path in (active, old, recent):
        path.write_text("{}\n")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(active, (long_ago, long_ago))  # even an old active file stays
    handler = PrunedRotatingFileHandler(active, max_bytes=1000, backups=3, retention_days=14)
    handler.close()
    assert not old.exists() and recent.exists() and active.exists()


def test_the_log_directory_is_private(tmp_path, logging_in):
    logging_in()
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700


# --- queue overflow and shutdown ---


class BlockingWriter(logging.Handler):
    """A writer that never finishes: stands in for a stalled disk."""

    def __init__(self) -> None:
        super().__init__()
        self.unblock = threading.Event()
        self.written: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.unblock.wait()
        self.written.append(record)


def isolated_logger(handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger(f"rehearse_ai.tests.isolated.{id(handler)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


def test_a_stalled_writer_never_blocks_the_caller_and_overflow_is_counted_then_reported():
    queue_handler = BoundedQueueHandler(maxsize=10)
    writer = BlockingWriter()
    writer.setFormatter(JsonFormatter())
    listener = BoundedQueueListener(queue_handler.queue, writer)
    listener.start()
    logger = isolated_logger(queue_handler)
    started = time.monotonic()
    for number in range(2000):
        event(logger, "app.started", sequence=number)
    event(logger, "persistence.write_failed", level=logging.ERROR)
    assert time.monotonic() - started < 2.0  # nothing waited for the writer
    assert sum(queue_handler.dropped.values()) > 1900
    assert queue_handler.dropped["ERROR"] == 1  # the policy is "drop the new record", by level
    writer.unblock.set()
    time.sleep(0.2)  # let the writer drain, then room exists again
    event(logger, "app.started", sequence=-1)
    listener.stop()
    names = [record.event for record in writer.written]
    assert "log.records_dropped" in names
    summary = next(r for r in writer.written if r.event == "log.records_dropped")
    assert summary.fields["dropped"] > 1900 and summary.fields["by_level"]["ERROR"] == 1
    assert not queue_handler.dropped  # reported, so counters restart


def test_shutdown_flushes_everything_queued_before_it(tmp_path, logging_in):
    handle = logging_in()
    for number in range(500):
        event(LOGGER, "app.started", sequence=number)
    handle.shutdown()
    assert len([r for r in application_log(tmp_path) if r["event"] == "app.started"]) == 500


def test_shutdown_gives_up_on_a_stuck_writer_instead_of_hanging():
    queue_handler = BoundedQueueHandler(maxsize=5)
    writer = BlockingWriter()
    listener = BoundedQueueListener(queue_handler.queue, writer)
    listener.start()
    logger = isolated_logger(queue_handler)
    for number in range(50):  # the queue is full, so even the stop marker needs room
        event(logger, "app.started", sequence=number)
    started = time.monotonic()
    listener.stop(timeout=0.3)
    assert time.monotonic() - started < 2.0
    writer.unblock.set()


# --- logging can never interrupt the interview ---


def test_a_failing_queue_a_broken_file_and_a_bad_field_do_not_raise_into_the_caller(
    tmp_path, logging_in, monkeypatch, capsys
):
    handle = logging_in()

    def refuse(_record):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(handle.handler.queue, "put_nowait", refuse)
    event(LOGGER, "app.started")  # must not raise
    assert handle.handler.failures >= 1
    monkeypatch.undo()

    class Broken:
        def write(self, _data):
            raise OSError("disk full")

        def flush(self):
            raise OSError("disk full")

        def close(self):
            pass

    handle.file_handlers[0].stream = Broken()
    for _ in range(3):
        event(LOGGER, "app.started")
    handle.shutdown()  # still shuts down cleanly with a broken file
    assert capsys.readouterr().err.count("not writable") == 1  # said once, not per event


def test_a_field_that_cannot_be_formatted_is_reported_not_raised(tmp_path, logging_in):
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no string for you")

    handle = logging_in()
    event(LOGGER, "app.started", details={Unprintable(): 1})
    event(LOGGER, "app.after")
    handle.shutdown()
    events = [r["event"] for r in application_log(tmp_path)]
    assert "log.format_failed" in events and "app.after" in events


def test_a_failing_operational_mirror_cannot_stop_the_conversation_record(tmp_path, monkeypatch):
    def broken(_entry):
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr("rehearse_ai.interview.session_log.mirror", broken)
    log = SessionLog(tmp_path, "mirror-failure")
    entry = log.record("session_started", build_id="b")
    log.close()
    assert entry["event"] == "session_started"
    assert (tmp_path / "mirror-failure.jsonl").read_text().count("session_started") == 1


def test_an_unwritable_log_directory_disables_files_not_the_application(tmp_path, capsys):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied")
    handle = configure_logging(LogSettings(directory=blocker / "logs", console=False), build_id="b")
    try:
        event(LOGGER, "app.started")  # still safe to call
        assert handle.directory is None
        assert "log files disabled" in capsys.readouterr().err
    finally:
        handle.shutdown()


# --- paths and configuration ---


def test_a_relative_log_directory_resolves_the_same_from_the_root_or_from_backend(
    monkeypatch,
):
    root = repository_root()
    assert root is not None
    results = set()
    for start in (root, root / "backend"):
        monkeypatch.chdir(start)
        results.add(resolve_directory(Path("logs")))
    assert results == {root / "logs"}


def test_outside_a_checkout_a_relative_directory_uses_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(configuration_module, "repository_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    assert resolve_directory(Path("logs")) == (tmp_path / "logs").resolve()
    assert resolve_directory(Path("/var/log/rehearse")) == Path("/var/log/rehearse")


def required_env(**extra) -> dict[str, str]:
    values = {
        "DEEPGRAM_API_KEY": "x",
        "OPENAI_API_KEY": "x",
        "CARTESIA_API_KEY": "x",
        "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5433/db_test",
        "REHEARSE_AUTH_SECRET": "s" * 40,
        "REHEARSE_PUBLIC_BASE_URL": "http://127.0.0.1:7860",
        "REHEARSE_SMTP_HOST": "127.0.0.1",
        "REHEARSE_SMTP_FROM": "RehearseAI <no-reply@localhost>",
    }
    values.update(extra)
    return values


def test_log_settings_have_safe_defaults_and_read_the_environment(tmp_path):
    defaults = Settings.load(env=required_env(), env_file=tmp_path / "absent.env").log
    assert defaults == LogSettings()
    assert defaults.directory == Path("logs") and defaults.console and defaults.files
    custom = Settings.load(
        env=required_env(
            REHEARSE_LOG_DIR="/var/log/rehearse",
            REHEARSE_LOG_LEVEL="warning",
            REHEARSE_LOG_CONSOLE="off",
            REHEARSE_LOG_FILES="on",
            REHEARSE_LOG_MAX_MB="2",
            REHEARSE_LOG_BACKUPS="3",
            REHEARSE_LOG_RETENTION_DAYS="7",
            REHEARSE_LOG_QUEUE_SIZE="500",
        ),
        env_file=tmp_path / "absent.env",
    ).log
    assert custom == LogSettings(
        directory=Path("/var/log/rehearse"),
        level="WARNING",
        console=False,
        files=True,
        max_bytes=2 * 1024 * 1024,
        backups=3,
        retention_days=7,
        queue_size=500,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"REHEARSE_LOG_LEVEL": "LOUD"},
        {"REHEARSE_LOG_CONSOLE": "maybe"},
        {"REHEARSE_LOG_MAX_MB": "0"},
        {"REHEARSE_LOG_BACKUPS": "0"},
        {"REHEARSE_LOG_QUEUE_SIZE": "5"},
        {"REHEARSE_LOG_DIR": " "},
    ],
)
def test_invalid_log_settings_are_refused(tmp_path, bad):
    with pytest.raises(ConfigurationError):
        Settings.load(env=required_env(**bad), env_file=tmp_path / "absent.env")


def test_the_console_writer_emits_the_same_json_lines(tmp_path, capsys):
    handle = configure_logging(
        LogSettings(directory=tmp_path / "logs", console=True, files=False), build_id="b"
    )
    event(LOGGER, "app.started", port=7860)
    handle.shutdown()
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert any(r["event"] == "app.started" and r["port"] == 7860 for r in printed)
    assert not (tmp_path / "logs").exists()


# --- HTTP ---


def http_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/api/invitations/{token}")
    async def invitation(token: str):
        return {"ok": True} if token == "good" else JSONResponse404()

    @app.get("/boom")
    async def boom():
        raise RuntimeError(f"failure containing {SECRET}")

    @app.get("/fine")
    async def fine():
        return {"ok": True}

    return app


def JSONResponse404():  # noqa: N802 (reads like a response type in the route above)
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "Invitation unavailable"}, status_code=404)


def test_http_failures_are_logged_by_route_template_with_a_request_id(tmp_path, logging_in):
    handle = logging_in()
    token = "T" * 43
    with TestClient(http_app(), raise_server_exceptions=False) as client:
        rejected = client.get(
            f"/api/invitations/{token}?email=nyla@example.com",
            headers={"X-Request-ID": "client-chosen"},
        )
        client.get("/fine")
        crashed = client.get("/boom")
    handle.shutdown()
    written = everything_written(tmp_path)
    assert token not in written and "nyla@example.com" not in written and SECRET not in written
    rows = {r["event"]: r for r in application_log(tmp_path)}
    assert rows["http.request_rejected"]["route"] == "/api/invitations/{token}"
    assert rows["http.request_rejected"]["status"] == 404
    assert rows["http.request_rejected"]["request_id"] == rejected.headers["x-request-id"]
    assert rows["http.request_rejected"]["request_id"] != "client-chosen"
    assert rows["http.request_failed"]["level"] == "ERROR"
    assert rows["http.request_failed"]["error"]["type"] == "RuntimeError"
    assert rows["http.request_failed"]["route"] == "/boom"
    assert crashed.status_code == 500
    assert sum(1 for r in application_log(tmp_path) if r["event"].startswith("http.")) == 2


# --- persistence and PDF failures ---


def test_persistence_and_pdf_failures_are_logged_with_ids_and_no_content(tmp_path, logging_in):
    import uuid

    from rehearse_ai.records.store import InterviewStore

    handle = logging_in()
    store = InterviewStore(None, tmp_path / "sessions", tmp_path / "pdfs", None, tmp_path / "img")
    session_id = str(uuid.uuid4())
    record_id = uuid.uuid4()

    async def scenario():
        for number in range(store.events.maxsize):
            store.events.put_nowait({"session_id": "filler", "sequence": number})
        assert store.enqueue({"session_id": session_id, "sequence": 1, "text": SECRET}) is False
        for _ in range(store.pdf_jobs.maxsize):
            store.pdf_jobs.put_nowait(uuid.uuid4())
        assert store.schedule_pdf(record_id) is False

        async def failing(_record_id):
            raise RuntimeError(f"cannot render {SECRET}")

        store.generate_pdf = failing
        queue_ = asyncio.Queue()
        queue_.put_nowait(record_id)
        store.pdf_jobs = queue_
        task = asyncio.create_task(store._make_pdfs())
        await queue_.join()
        task.cancel()

    asyncio.run(scenario())
    handle.shutdown()
    assert SECRET not in everything_written(tmp_path)
    rows = {r["event"]: r for r in application_log(tmp_path)}
    assert rows["persistence.queue_full"]["record_id"] == session_id
    assert rows["persistence.queue_full"]["level"] == "ERROR"
    assert rows["pdf.queue_full"]["record_id"] == str(record_id)
    assert rows["pdf.generation_failed"]["error"]["type"] == "RuntimeError"
    assert rows["pdf.generation_failed"]["record_id"] == str(record_id)
