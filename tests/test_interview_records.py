"""PostgreSQL persistence, permission, and private PDF regressions using synthetic events."""

import asyncio
import io
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehearse_ai.accounts_db import Candidate, InterviewRecord, TranscriptEntry
from rehearse_ai.admin import promote
from rehearse_ai.config import AuthConfig, Settings
from rehearse_ai.interview_store import InterviewStore
from rehearse_ai.server import create_app
from rehearse_ai.session_log import SessionLog
from rehearse_ai.snapshot import InvalidSnapshot, save_snapshot
from rehearse_ai.transcript_pdf import build_pdf

TEST_URL = os.environ.get(
    "REHEARSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
)
assert TEST_URL.rsplit("/", 1)[-1].endswith("_test")
PASSWORD = "correct horse battery 1234"


def address() -> str:
    return f"interview-{uuid.uuid4().hex}@example.com"


async def make_candidate(sessions, *, email=None):
    candidate = Candidate(
        id=uuid.uuid4(),
        email=email or address(),
        hashed_password="test-hash",
        is_active=True,
        is_verified=True,
        is_superuser=False,
    )
    async with sessions.begin() as db:
        db.add(candidate)
    return candidate


@pytest.mark.asyncio
async def test_order_corrections_terminal_states_and_stale_events(tmp_path):
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        candidate = await make_candidate(sessions)
        store = InterviewStore(sessions, tmp_path / "logs", tmp_path / "pdf")
        record_id = await store.create_attempt(candidate.id, "build-test", "prompt-test")
        log = SessionLog(store.logs, str(record_id))

        async def record(kind, **values):
            event = log.record(kind, **values)
            await store.apply_event(event)
            return event

        await record("candidate_authenticated")
        await record("session_started")
        await record("agent_prepared", speaker="agent", utterance_id=1, text="Question one")
        await record("agent_output_started", speaker="agent", utterance_id=1)
        await record("agent_delivery_reported", speaker="agent", utterance_id=1)
        original = await record(
            "candidate_final", speaker="candidate", text="I used web books", turn_number=1
        )
        await record("candidate_final", speaker="candidate", text="duplicate", turn_number=1)
        await record("correction_applied", speaker="candidate", old="web books", new="webhooks")
        await record("agent_prepared", speaker="agent", utterance_id=2, text="Prepared only")
        await record("agent_prepared", speaker="agent", utterance_id=2, text="duplicate")
        await record("agent_cancellation", utterance_id=2)
        late = await record("agent_delivery_reported", utterance_id=2)
        await record("session_ended", session_end_kind="disconnected_before_completion")
        await store.apply_event(original)
        await store.apply_event(late)
        log.close()
        async with sessions() as db:
            interview = await db.get(InterviewRecord, record_id)
            entries = (
                await db.scalars(
                    select(TranscriptEntry)
                    .where(TranscriptEntry.interview_id == record_id)
                    .order_by(TranscriptEntry.sequence)
                )
            ).all()
            assert interview.status == "interrupted"
            assert interview.termination_reason == "session_disconnected"
            assert [entry.text for entry in entries] == [
                "Question one",
                "I used web books",
                "Prepared only",
            ]
            assert [entry.status for entry in entries] == [
                "output_completed",
                "machine_transcription",
                "cancelled",
            ]
            assert entries[-1].interrupted
        assert sorted(
            await asyncio.gather(store.generate_pdf(record_id), store.generate_pdf(record_id))
        ) == [False, True]
        async with sessions() as db:
            assert (await db.get(InterviewRecord, record_id)).snapshot_status == "unavailable"
        pdf = PdfReader(store.pdf_path(record_id))
        text = "\n".join(page.extract_text() for page in pdf.pages)
        assert "I used web books" in text
        assert "webhooks" not in text
        assert "partial interview conversation" in text
        assert "Camera snapshot unavailable" in text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_candidate_turns_use_boundary_frame_not_stale_turn_number(tmp_path):
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        candidate = await make_candidate(sessions)
        store = InterviewStore(sessions, tmp_path / "logs", tmp_path / "pdf")
        record_id = await store.create_attempt(candidate.id, "build", "prompt")
        log = SessionLog(store.logs, str(record_id))
        for boundary_id, text in (
            (101, "First completed answer"),
            (101, "Duplicate callback"),
            (207, "Next answer before speaking-start callback"),
        ):
            await store.apply_event(
                log.record(
                    "candidate_final",
                    speaker="candidate",
                    text=text,
                    turn_number=1,
                    boundary_frame_id=boundary_id,
                )
            )
        log.close()
        async with sessions() as db:
            entries = (
                await db.scalars(
                    select(TranscriptEntry)
                    .where(TranscriptEntry.interview_id == record_id)
                    .order_by(TranscriptEntry.sequence)
                )
            ).all()
            assert [entry.text for entry in entries] == [
                "First completed answer",
                "Next answer before speaking-start callback",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_replays_raw_events_and_failure_is_visible(tmp_path, monkeypatch):
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        candidate = await make_candidate(sessions)
        store = InterviewStore(sessions, tmp_path / "logs", tmp_path / "pdf")
        record_id = await store.create_attempt(candidate.id, "build-test", "prompt-test")
        log = SessionLog(store.logs, str(record_id))
        first = log.record("session_started")
        await store.apply_event(first)
        log.record("candidate_final", speaker="candidate", text="Original machine transcription")
        log.close()
        await store.reconcile()
        async with sessions() as db:
            interview = await db.get(InterviewRecord, record_id)
            entries = (
                await db.scalars(
                    select(TranscriptEntry).where(TranscriptEntry.interview_id == record_id)
                )
            ).all()
            assert interview.status == "interrupted"
            assert interview.termination_reason == "server_restart"
            assert len(entries) == 1
        await store.reconcile()
        async with sessions() as db:
            assert (
                len(
                    (
                        await db.scalars(
                            select(TranscriptEntry).where(TranscriptEntry.interview_id == record_id)
                        )
                    ).all()
                )
                == 1
            )

        def fail(*_args):
            raise OSError("synthetic PDF error")

        monkeypatch.setattr("rehearse_ai.interview_store.build_pdf", fail)
        assert not await store.generate_pdf(record_id)
        async with sessions() as db:
            interview = await db.get(InterviewRecord, record_id)
            assert interview.status == "interrupted"
            assert interview.pdf_status == "failed"
            assert interview.pdf_error == "OSError"
        monkeypatch.undo()
        assert await store.generate_pdf(record_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_normal_early_and_failed_lifecycle(tmp_path):
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        candidate = await make_candidate(sessions)
        store = InterviewStore(sessions, tmp_path / "logs", tmp_path / "pdf")
        for terminal, expected, end_kind in (
            ("interview_complete", "completed", "normal_completion"),
            ("interview_ended_early", "ended_early", "spoken_early_end"),
            (None, "failed", "pipeline_failed"),
        ):
            record_id = await store.create_attempt(candidate.id, "build-test", "prompt-test")
            log = SessionLog(store.logs, str(record_id))
            for kind, values in (
                ("session_started", {}),
                *([(terminal, {})] if terminal else []),
                ("session_ended", {"session_end_kind": end_kind}),
            ):
                await store.apply_event(log.record(kind, **values))
            log.close()
            async with sessions() as db:
                record = await db.get(InterviewRecord, record_id)
                assert record.status == expected
                assert record.ended_at is not None
            # A stale terminal callback cannot change the outcome.
            await store.apply_event(
                {
                    "session_id": str(record_id),
                    "sequence": 2,
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "event": "interview_complete",
                }
            )
            async with sessions() as db:
                assert (await db.get(InterviewRecord, record_id)).status == expected
            after_terminal = {
                "session_id": str(record_id),
                "sequence": 4 if terminal else 3,
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "event": "session_started",
            }
            await store.apply_event(after_terminal)
            await store.apply_event(
                {
                    "session_id": str(record_id),
                    "sequence": after_terminal["sequence"] + 1,
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "event": "candidate_final",
                    "text": "stale answer",
                }
            )
            async with sessions() as db:
                assert (await db.get(InterviewRecord, record_id)).status == expected
                assert (
                    await db.scalars(
                        select(TranscriptEntry).where(
                            TranscriptEntry.interview_id == record_id,
                            TranscriptEntry.text == "stale answer",
                        )
                    )
                ).first() is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_promotion_requires_existing_active_account():
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        unverified = await make_candidate(sessions)
        async with sessions.begin() as db:
            candidate = await db.get(Candidate, unverified.id)
            candidate.is_verified = False
        assert await promote(unverified.email, TEST_URL) == unverified.email
        async with sessions.begin() as db:
            candidate = await db.get(Candidate, unverified.id)
            assert candidate.is_superuser and not candidate.is_verified
            candidate.is_active = False
            candidate.is_superuser = False
        with pytest.raises(ValueError, match="Activate"):
            await promote(unverified.email, TEST_URL)
        with pytest.raises(ValueError, match="not found"):
            await promote(address(), TEST_URL)
        async with sessions() as db:
            assert not (await db.get(Candidate, unverified.id)).is_superuser
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_queue_write_failure_preserves_raw_log_for_restart(tmp_path, monkeypatch):
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        candidate = await make_candidate(sessions)
        noticed = []

        async def notice(session_id):
            noticed.append(session_id)

        store = InterviewStore(sessions, tmp_path / "logs", tmp_path / "pdf", notice)
        record_id = await store.create_attempt(candidate.id, "build", "prompt")
        log = SessionLog(store.logs, str(record_id), store.enqueue)
        log.record("session_started")
        log.record("candidate_final", speaker="candidate", text="Synthetic raw answer")
        log.close()
        original = store.apply_event
        calls = 0

        async def fail_once(event):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic database outage")
            await original(event)

        monkeypatch.setattr(store, "apply_event", fail_once)
        worker = asyncio.create_task(store._write_events())
        await store.events.join()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert noticed == [str(record_id)]
        assert store.failed_ids == {str(record_id)}
        assert "Synthetic raw answer" in log.path.read_text()
        async with sessions() as db:
            assert (await db.get(InterviewRecord, record_id)).last_event_sequence == 1
        recovered = InterviewStore(sessions, store.logs, store.pdf_dir)
        await recovered.reconcile()
        async with sessions() as db:
            record = await db.get(InterviewRecord, record_id)
            assert record.last_event_sequence == 2
            assert record.status == "interrupted"
            assert (
                await db.scalar(
                    select(TranscriptEntry).where(TranscriptEntry.interview_id == record_id)
                )
            ).text == "Synthetic raw answer"
    finally:
        await engine.dispose()


def test_long_unicode_pdf_and_escaped_transcript(tmp_path):
    path = tmp_path / "private" / "record.pdf"
    now = datetime.now(UTC)
    build_pdf(
        path,
        {
            "id": str(uuid.uuid4()),
            "candidate_email": "zoë@example.com",
            "status": "completed",
            "termination_reason": "normal_completion",
            "started_at": now,
            "ended_at": now,
        },
        [
            {
                "speaker": "candidate",
                "timestamp_utc": now,
                "text": "Zoë García <script>alert(1)</script> "
                + "Long answer with measured results. " * 45,
                "status": "machine_transcription",
                "interrupted": False,
            }
            for _ in range(25)
        ],
    )
    reader = PdfReader(path)
    assert len(reader.pages) > 2
    extracted = "\n".join(page.extract_text() for page in reader.pages)
    assert "Zoë García" in extracted
    assert "<script>alert(1)</script>" in extracted
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("status", "expected_partial"),
    [("completed", False), ("ended_early", True), ("interrupted", True)],
)
def test_pdf_metadata_and_conversation_labels(tmp_path, status, expected_partial):
    path = tmp_path / "private" / f"{status}.pdf"
    start = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    end = datetime(2026, 11, 13, 10, 4, 12, tzinfo=UTC)
    build_pdf(
        path,
        {
            "id": str(uuid.uuid4()),
            "candidate_email": "candidate@example.com",
            "candidate_name": "Zoë García",
            "created_at": start,
            "started_at": start,
            "ended_at": end,
            "status": status,
        },
        [
            {
                "speaker": "agent",
                "timestamp_utc": start,
                "text": "Tell me about your project.",
                "status": "output_completed",
            },
            {
                "speaker": "candidate",
                "timestamp_utc": end,
                "text": "I built a webhook service.",
                "status": "machine_transcription",
            },
            {
                "speaker": "agent",
                "timestamp_utc": end,
                "text": "An interrupted response.",
                "status": "interrupted",
            },
        ],
    )
    text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
    assert "Conversation PDF" in text
    assert "candidate@example.com" in text
    assert "Candidate name (self-reported)" in text and "Zoë García" in text
    assert "UTC" in text and "4m 12s" in text
    assert status.replace("_", " ").title() in text
    assert ("partial interview conversation" in text) is expected_partial
    assert text.index("Tell me about your project") < text.index("I built a webhook service")
    assert "RehearseAI" in text and "Candidate" in text
    assert "Machine transcription" in text
    assert "exact words heard unknown" in text
    assert "Page 1" in text


def test_candidate_admin_isolation_and_regeneration(tmp_path):
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        session_dir=tmp_path / "logs",
        pdf_dir=tmp_path / "pdf",
        snapshot_dir=tmp_path / "snapshots",
        auth=AuthConfig(database_url=TEST_URL, secret="test-secret-at-least-thirty-two-characters"),
    )
    app = create_app(settings)
    sent = []

    async def capture(to, purpose, token):
        sent.append((to, purpose, token))

    app.state.accounts.mailer.send_link = capture
    with TestClient(app) as client:
        unknown = uuid.uuid4()
        assert client.get(f"/api/interviews/{unknown}/pdf").status_code == 401
        assert client.get(f"/api/admin/interviews/{unknown}/pdf").status_code == 401
        assert client.post(f"/api/interviews/{unknown}/pdf/regenerate").status_code == 401
        assert client.post(f"/api/admin/interviews/{unknown}/pdf/regenerate").status_code == 401
        token = client.get("/api/csrf").json()["token"]
        headers = {"X-CSRF-Token": token}
        email_a, email_b, email_admin = address(), address(), address()

        def create_account(email, extra=None):
            response = client.post(
                "/api/auth/signup",
                json={
                    "email": email,
                    "password": PASSWORD,
                    **(extra or {}),
                },
                headers=headers,
            )
            assert response.status_code == 201
            assert sent == []
            assert (
                client.post(
                    "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=headers
                ).status_code
                == 204
            )
            return uuid.UUID(client.get("/api/auth/me").json()["id"])

        id_a = create_account(email_a, {"is_superuser": True})
        assert client.get("/api/auth/me").json()["role"] == "candidate"
        assert client.get("/admin/interviews").status_code == 403
        store = app.state.interview_store
        record_a = client.portal.call(store.create_attempt, id_a, "test", "prompt")
        log = SessionLog(store.logs, str(record_a))
        for kind, values in (
            ("session_started", {}),
            ("candidate_final", {"speaker": "candidate", "text": "A private answer"}),
            ("interview_complete", {}),
        ):
            client.portal.call(store.apply_event, log.record(kind, **values))
        log.close()
        client.portal.call(store.pdf_jobs.join)
        assert client.get(f"/api/interviews/{record_a}/pdf").status_code == 403
        assert client.get(f"/api/admin/interviews/{record_a}/pdf").status_code == 403
        assert (
            client.post(f"/api/interviews/{record_a}/pdf/regenerate", headers=headers).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/admin/interviews/{record_a}/pdf/regenerate", headers=headers
            ).status_code
            == 403
        )
        assert "pdf_status" not in client.get(f"/api/interviews/{record_a}").json()
        assert "pdf_status" not in client.get("/api/interviews").json()["items"][0]
        candidate_page = client.get(f"/interviews/{record_a}").text
        assert "pdf-link" not in candidate_page and "pdf-regenerate" not in candidate_page
        assert (
            client.get(f"/api/interviews/{record_a}/transcript").json()[0]["text"]
            == "A private answer"
        )

        id_b = create_account(email_b)
        record_b = client.portal.call(store.create_attempt, id_b, "test", "prompt")
        assert {item["id"] for item in client.get("/api/interviews").json()["items"]} == {
            str(record_b)
        }
        for route in (
            f"/interviews/{record_a}",
            f"/api/interviews/{record_a}",
            f"/api/interviews/{record_a}/transcript",
        ):
            assert client.get(route).status_code == 404
        assert client.get(f"/api/interviews/{record_a}/pdf").status_code == 403
        assert client.get("/api/admin/interviews").status_code == 403
        assert (
            client.post(f"/api/interviews/{record_a}/pdf/regenerate", headers=headers).status_code
            == 403
        )

        create_account(email_admin)
        client.portal.call(promote, email_admin, TEST_URL)
        assert client.get("/api/auth/me").json()["role"] == "admin"
        listed = client.get("/api/admin/interviews", params={"candidate_email": email_a}).json()
        assert str(record_a) in {item["id"] for item in listed["items"]}
        completed = client.get(
            "/api/admin/interviews",
            params={
                "candidate_email": email_a,
                "status": "completed",
            },
        ).json()
        assert {item["id"] for item in completed["items"]} == {str(record_a)}
        all_ids = {item["id"] for item in client.get("/api/admin/interviews").json()["items"]}
        assert {str(record_a), str(record_b)} <= all_ids
        assert client.get(f"/admin/interviews/{record_a}").status_code == 200
        admin_page = client.get(f"/admin/interviews/{record_a}").text
        assert "Download conversation PDF" in admin_page
        assert 'id="conversation"' not in admin_page
        assert client.get(f"/api/admin/interviews/{record_a}/transcript").status_code == 200
        assert client.get(f"/api/admin/interviews/{record_a}/pdf").status_code == 200
        assert client.post(f"/api/admin/interviews/{record_a}/pdf/regenerate").status_code == 403

        store.pdf_path(record_a).unlink()
        assert client.get(f"/api/admin/interviews/{record_a}/pdf").status_code == 409
        assert client.get(f"/api/admin/interviews/{record_a}").json()["pdf_status"] == "failed"
        assert (
            client.post(
                f"/api/admin/interviews/{record_a}/pdf/regenerate", headers=headers
            ).status_code
            == 202
        )
        client.portal.call(store.pdf_jobs.join)
        assert client.get(f"/api/admin/interviews/{record_a}/pdf").status_code == 200
        assert (
            client.post(
                f"/api/admin/interviews/{record_a}/pdf/regenerate", headers=headers
            ).status_code
            == 409
        )


def test_accepted_offer_uses_server_authenticated_candidate(tmp_path, monkeypatch):
    captured = []

    class FakeLiveSession:
        def __init__(self, connection, _settings, **kwargs):
            self.connection = connection
            self.candidate_id = kwargs["candidate_id"]
            self.log = SimpleNamespace(session_id=kwargs["session_id"])
            self.done = asyncio.Event()
            captured.append(kwargs)

        async def run(self):
            await self.done.wait()

        async def stop(self):
            self.done.set()

    async def accept(_handler, _request, callback):
        await callback(SimpleNamespace())
        return {"sdp": "synthetic-answer", "type": "answer"}

    monkeypatch.setattr("rehearse_ai.server.LiveSession", FakeLiveSession)
    monkeypatch.setattr("rehearse_ai.server.SmallWebRTCRequestHandler.handle_web_request", accept)
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        session_dir=tmp_path / "logs",
        pdf_dir=tmp_path / "pdf",
        snapshot_dir=tmp_path / "snapshots",
        auth=AuthConfig(database_url=TEST_URL, secret="test-secret-at-least-thirty-two-characters"),
    )
    app = create_app(settings)
    sent = []

    async def capture(to, purpose, token):
        sent.append(token)

    app.state.accounts.mailer.send_link = capture
    with TestClient(app) as client:
        csrf = client.get("/api/csrf").json()["token"]
        headers = {"X-CSRF-Token": csrf}
        email = address()
        assert (
            client.post(
                "/api/auth/signup", json={"email": email, "password": PASSWORD}, headers=headers
            ).status_code
            == 201
        )
        assert sent == []
        assert (
            client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=headers
            ).status_code
            == 204
        )
        candidate_id = client.get("/api/auth/me").json()["id"]
        assert client.get("/interview").status_code == 200
        assert client.get("/api/interviews").json()["items"] == []
        assert (
            client.post(
                "/api/offer", json={"sdp": "synthetic-offer", "type": "offer"}, headers=headers
            ).status_code
            == 400
        )
        response = client.post(
            "/api/offer",
            json={
                "sdp": "synthetic-offer",
                "type": "offer",
                "snapshot_acknowledged": True,
                "request_data": {"candidate_id": str(uuid.uuid4())},
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert captured[0]["candidate_id"] == candidate_id
        record_id = uuid.UUID(captured[0]["session_id"])
        assert client.get("/api/interviews").json()["items"][0]["id"] == str(record_id)

        async def lookup():
            async with app.state.interview_store.db_sessions() as db:
                return await db.get(InterviewRecord, record_id)

        saved = client.portal.call(lookup)
        assert str(saved.candidate_id) == candidate_id
        assert saved.status == "starting"

        store = app.state.interview_store
        log = SessionLog(store.logs, str(record_id))
        client.portal.call(store.apply_event, log.record("session_started"))
        image = Image.new("RGB", (800, 450), "#234b59")
        output = io.BytesIO()
        image.save(output, "JPEG", exif=b"Exif\x00\x00synthetic metadata")
        jpeg = output.getvalue()
        url = f"/api/interviews/{record_id}/snapshot"
        assert (
            client.post(url, content=jpeg, headers={"Content-Type": "image/jpeg"}).status_code
            == 403
        )
        assert (
            client.post(
                url, content=b"not a jpeg", headers={**headers, "Content-Type": "image/jpeg"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                url, content=b"x" * 1_000_001, headers={**headers, "Content-Type": "image/jpeg"}
            ).status_code
            == 413
        )
        assert (
            client.post(
                url, content=jpeg, headers={**headers, "Content-Type": "image/jpeg"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                url, content=jpeg, headers={**headers, "Content-Type": "image/jpeg"}
            ).status_code
            == 409
        )
        with Image.open(store.snapshot_path(record_id)) as saved_image:
            assert saved_image.size == (640, 360)
            assert not saved_image.getexif()
        other = address()
        assert (
            client.post(
                "/api/auth/signup", json={"email": other, "password": PASSWORD}, headers=headers
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/auth/login", json={"email": other, "password": PASSWORD}, headers=headers
            ).status_code
            == 204
        )
        assert (
            client.post(
                url, content=jpeg, headers={**headers, "Content-Type": "image/jpeg"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=headers
            ).status_code
            == 204
        )
        client.portal.call(store.apply_event, log.record("interview_complete"))
        assert (
            client.post(
                url, content=jpeg, headers={**headers, "Content-Type": "image/jpeg"}
            ).status_code
            == 409
        )
        client.portal.call(store.pdf_jobs.join)
        pdf = PdfReader(store.pdf_path(record_id))
        assert "Candidate camera snapshot" in "\n".join(page.extract_text() for page in pdf.pages)
        assert client.portal.call(lookup).snapshot_status == "available"
        log.close()


def test_snapshot_validation_and_private_reencoding(tmp_path):
    target = tmp_path / "private" / "still.jpg"
    for invalid in (b"", b"not an image", b"x" * 1_000_001):
        with pytest.raises(InvalidSnapshot):
            save_snapshot(invalid, target)
    png = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(png, "PNG")
    with pytest.raises(InvalidSnapshot):
        save_snapshot(png.getvalue(), target)
    oversized_dimensions = io.BytesIO()
    Image.new("RGB", (2000, 1100), "white").save(oversized_dimensions, "JPEG")
    with pytest.raises(InvalidSnapshot):
        save_snapshot(oversized_dimensions.getvalue(), target)
    assert not target.exists()
