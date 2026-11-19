"""Room-image capture: upload validation, isolation, idempotency, races and PDF compatibility."""

import asyncio
import hashlib
import io
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support import TEST_URL, login, make_app, new_admin_email, seed_assignment

from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.models import InterviewRecord
from rehearse_ai.records.images import InvalidSnapshot, save_snapshot

OFFER = {"sdp": "synthetic-offer", "type": "offer", "snapshot_acknowledged": True}


def jpeg(size=(1280, 651), exif=False, fmt="JPEG") -> bytes:
    image = Image.new("RGB", size, (30, 90, 110))
    for x in range(0, size[0], 40):  # not a flat colour, like a real room
        image.paste((200, 60, 60), (x, 0, x + 10, size[1]))
    output = io.BytesIO()
    kwargs = {}
    if exif:
        data = Image.Exif()
        data[315] = "Secret Photographer"  # Artist
        kwargs["exif"] = data
    image.save(output, fmt, **kwargs)
    return output.getvalue()


@pytest.fixture(scope="module")
def admin_email():
    return new_admin_email()


@pytest.fixture
def live(monkeypatch):
    """A fake voice session so an attempt can be driven without providers."""
    captured = []

    class FakeLive:
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

    async def accept(_handler, _offer, callback):
        await callback(SimpleNamespace())
        return {"sdp": "synthetic-answer", "type": "answer"}

    monkeypatch.setattr("rehearse_ai.server.LiveSession", FakeLive)
    monkeypatch.setattr("rehearse_ai.server.SmallWebRTCRequestHandler.handle_web_request", accept)
    return captured


class Attempt:
    """One candidate, signed in through an invitation, with a started interview."""

    def __init__(self, tmp_path, admin_email, captured, port, *, start=True):
        self.captured = captured
        self.app = make_app(tmp_path)
        self.client_ctx = TestClient(self.app, client=(f"test-{uuid.uuid4().hex}", port))
        self.assignment_id, _version, self.token = asyncio.run(seed_assignment(admin_email))
        self.start = start

    def __enter__(self):
        self.client = self.client_ctx.__enter__()
        self.headers = {"X-CSRF-Token": self.client.get("/api/csrf").json()["token"]}
        assert (
            self.client.post(
                f"/api/invitations/{self.token}/redeem", json={}, headers=self.headers
            ).status_code
            == 200
        )
        assert (
            self.client.post(
                "/api/candidate/name", json={"full_name": "Nyla Example"}, headers=self.headers
            ).status_code
            == 200
        )
        assert self.client.post("/api/offer", json=OFFER, headers=self.headers).status_code == 200
        self.record_id = uuid.UUID(self.captured[-1]["session_id"])
        self.store = self.app.state.interview_store
        self.log = SessionLog(self.store.logs, str(self.record_id))
        if self.start:
            self.event("session_started")
        return self

    def event(self, name, **values):
        self.client.portal.call(self.store.apply_event, self.log.record(name, **values))

    def post(self, body, kind="room", *, content_type="image/jpeg", headers=None, record_id=None):
        merged = {**self.headers, "Content-Type": content_type}
        if kind is not None:
            merged["X-Capture-Kind"] = kind
        merged.update(headers or {})
        return self.client.post(
            f"/api/interviews/{record_id or self.record_id}/snapshot", content=body, headers=merged
        )

    def row(self):
        async def read():
            engine = create_async_engine(TEST_URL)
            try:
                async with async_sessionmaker(engine)() as db:
                    return await db.get(InterviewRecord, self.record_id)
            finally:
                await engine.dispose()

        return asyncio.run(read())

    def finish(self):
        self.event("interview_complete")
        self.client.portal.call(self.store.pdf_jobs.join)

    def __exit__(self, *exc):
        self.log.close()
        return self.client_ctx.__exit__(*exc)

    @property
    def image_path(self):
        return self.store.snapshot_path(self.record_id)


def test_a_consented_active_attempt_saves_one_room_image_and_the_pdf_labels_it(
    tmp_path, admin_email, live
):
    with Attempt(tmp_path, admin_email, live, 51001) as attempt:
        assert attempt.row().snapshot_status == "pending"
        response = attempt.post(jpeg(exif=True))
        assert response.status_code == 201 and response.json() == {
            "status": "saved",
            "kind": "room",
        }
        row = attempt.row()
        assert row.snapshot_status == "available" and row.snapshot_kind == "room"
        assert row.snapshot_captured_at is not None
        stored = Image.open(attempt.image_path)
        assert stored.format == "JPEG" and stored.size == (1280, 651)  # the room's own proportions
        assert not stored.getexif()  # re-encoded: client metadata is not kept
        assert oct(attempt.image_path.stat().st_mode & 0o777) == "0o600"
        attempt.finish()
        reader = PdfReader(attempt.store.pdf_path(attempt.record_id))
        text = " ".join("\n".join(page.extract_text() for page in reader.pages).split())
        assert (
            "INTERVIEW ROOM IMAGE" in text and "browser-rendered view of the interview room" in text
        )
        assert sum(len(page.images) for page in reader.pages) == 1


def test_a_second_or_concurrent_upload_never_replaces_the_first(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51002) as attempt:
        first = jpeg()
        assert attempt.post(first).status_code == 201
        digest = hashlib.sha256(attempt.image_path.read_bytes()).hexdigest()
        again = attempt.post(jpeg(size=(1000, 500)))
        assert again.status_code == 200 and again.json() == {"status": "already_saved"}
        assert hashlib.sha256(attempt.image_path.read_bytes()).hexdigest() == digest
        assert Image.open(attempt.image_path).size == (1280, 651)
    with Attempt(tmp_path / "race", admin_email, live, 51003) as racer:
        results = []

        def send(size):
            results.append(racer.post(jpeg(size=size)).status_code)

        threads = [threading.Thread(target=send, args=((1200 + i * 10, 600),)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(results).count(201) == 1 and set(results) <= {200, 201}
        assert racer.row().snapshot_status == "available"


def test_other_candidates_and_unauthenticated_callers_cannot_use_or_read_an_attempt(
    tmp_path, admin_email, live
):
    with Attempt(tmp_path / "a", admin_email, live, 51004) as owner:
        with Attempt(tmp_path / "b", admin_email, live, 51005) as other:
            # Another candidate's cookie cannot touch this record, whatever id the client sends.
            assert other.post(jpeg(), record_id=owner.record_id).status_code == 404
            assert not owner.image_path.exists() and owner.row().snapshot_kind is None
            assert not other.image_path.exists() and other.row().snapshot_kind is None
            # An id the server never issued is just as unreachable.
            assert other.post(jpeg(), record_id=uuid.uuid4()).status_code == 404
        anonymous = TestClient(make_app(tmp_path / "c"), client=(f"test-{uuid.uuid4().hex}", 51006))
        with anonymous:
            token = {"X-CSRF-Token": anonymous.get("/api/csrf").json()["token"]}
            url = f"/api/interviews/{owner.record_id}/snapshot"
            assert (
                anonymous.post(
                    url, content=jpeg(), headers={**token, "Content-Type": "image/jpeg"}
                ).status_code
                == 401
            )
        # The owning candidate cannot read the image or the PDF back: no such route exists for them.
        # Starting further apps runs startup recovery, which closes abandoned attempts; the
        # owner's own app is untouched, so start a fresh attempt for the read-back checks.
        owner.finish()
        assert owner.client.get(f"/api/interviews/{owner.record_id}/snapshot").status_code == 405
        assert owner.client.get(f"/api/admin/interviews/{owner.record_id}/pdf").status_code == 401
        assert owner.client.get(f"/api/interviews/{owner.record_id}/pdf").status_code == 401
        assert owner.client.get(f"/api/admin/interviews/{owner.record_id}").status_code == 401


def test_admins_download_the_pdf_and_it_contains_the_image(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51007) as attempt:
        assert attempt.post(jpeg()).status_code == 201
        attempt.finish()
        with TestClient(
            make_app(tmp_path / "admin"), client=(f"test-{uuid.uuid4().hex}", 51008)
        ) as admin:
            login(admin, admin_email)
            pdf = admin.get(f"/api/admin/interviews/{attempt.record_id}/pdf")
            assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
            assert pdf.headers["cache-control"] == "private, no-store"
            assert pdf.content.startswith(b"%PDF")


def test_csrf_origin_content_type_kind_and_size_are_enforced(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51009) as attempt:
        url = f"/api/interviews/{attempt.record_id}/snapshot"
        no_csrf = attempt.client.post(url, content=jpeg(), headers={"Content-Type": "image/jpeg"})
        assert no_csrf.status_code == 403
        assert attempt.post(jpeg(), kind="screen").status_code == 400  # unknown kind
        assert attempt.post(jpeg(), content_type="image/png").status_code == 415
        assert attempt.post(jpeg(), content_type="text/plain").status_code == 415
        assert attempt.post(b"\xff\xd8" + b"0" * 1_600_000).status_code == 413  # over any limit
        assert attempt.post(b"\xff\xd8" + b"0" * 1_100_000, kind="camera").status_code == 413
        assert attempt.row().snapshot_status == "pending"  # nothing above changed the attempt
        assert attempt.post(jpeg()).status_code == 201  # and a valid image is still accepted


@pytest.mark.parametrize(
    ("label", "body", "kind"),
    [
        ("not an image", b"this is not a jpeg" * 100, "room"),
        ("png named jpeg", None, "room"),
        ("truncated jpeg", "truncated", "room"),
        ("room too small", (200, 200), "room"),
        ("room too wide", (2500, 900), "room"),
        ("room too tall", (900, 2600), "room"),
        ("room too many pixels", (1900, 2300), "room"),
        ("camera shaped like a room", (748, 1480), "camera"),
        ("camera tiny", (8, 8), "camera"),
    ],
)
def test_invalid_images_are_rejected_by_decoding_not_by_trusting_the_client(
    tmp_path, admin_email, live, label, body, kind
):
    with Attempt(tmp_path, admin_email, live, 51010) as attempt:
        if body is None:
            body = jpeg(fmt="PNG")
        elif body == "truncated":
            body = jpeg()[:900]
        elif isinstance(body, tuple):
            body = jpeg(size=body)
        response = attempt.post(body, kind=kind)
        assert response.status_code == 422, (label, response.text)
        assert not attempt.image_path.exists()
        assert attempt.row().snapshot_status == "pending" and attempt.row().snapshot_kind is None


def test_the_camera_only_fallback_is_stored_and_labelled_as_such(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51011) as attempt:
        response = attempt.post(jpeg(size=(1280, 720)), kind="camera")
        assert response.status_code == 201 and response.json()["kind"] == "camera"
        assert Image.open(attempt.image_path).size == (640, 360)  # camera stills stay small
        assert attempt.row().snapshot_kind == "camera"
        attempt.finish()
        text = " ".join(
            "\n".join(
                p.extract_text() for p in PdfReader(attempt.store.pdf_path(attempt.record_id)).pages
            ).split()
        )
        assert "CANDIDATE CAMERA SNAPSHOT" in text and "INTERVIEW ROOM IMAGE" not in text
        assert "Camera only: the interview-room image could not be captured" in text


def test_clients_that_send_no_kind_header_are_treated_as_camera_only(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51012) as attempt:
        assert attempt.post(jpeg(size=(1280, 720)), kind=None).status_code == 201
        assert attempt.row().snapshot_kind == "camera"


def test_an_image_before_begin_or_after_the_interview_ends_is_refused_and_the_pdf_still_builds(
    tmp_path, admin_email, live
):
    with Attempt(tmp_path / "early", admin_email, live, 51013, start=False) as early:
        # The attempt exists but the voice session has not started: no capture yet.
        assert early.row().snapshot_status == "not_started"
        assert early.post(jpeg()).status_code == 409 and not early.image_path.exists()
    with Attempt(tmp_path / "late", admin_email, live, 51014) as late:
        late.finish()  # End won the race
        assert late.row().status == "completed"
        refused = late.post(jpeg())
        assert refused.status_code == 409 and not late.image_path.exists()
        assert late.row().snapshot_kind is None
        text = " ".join(
            "\n".join(
                p.extract_text() for p in PdfReader(late.store.pdf_path(late.record_id)).pages
            ).split()
        )
        assert "No interview image was saved for this interview" in text
        assert late.row().pdf_status == "available"  # the missing image did not prevent the PDF


def test_a_pending_image_never_blocks_pdf_generation(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51015) as attempt:
        attempt.finish()  # ends while the image is still pending: it times out, then is unavailable
        row = attempt.row()
        assert row.pdf_status == "available" and row.snapshot_status == "unavailable"


def test_the_browser_cannot_overwrite_the_stored_kind_or_attempt_through_headers(
    tmp_path, admin_email, live
):
    with Attempt(tmp_path, admin_email, live, 51016) as attempt:
        spoof = {
            "X-Interview-Id": str(uuid.uuid4()),
            "X-Assignment-Id": str(uuid.uuid4()),
            "X-Filename": "../../etc/passwd.jpg",
        }
        assert attempt.post(jpeg(), headers=spoof).status_code == 201
        row = attempt.row()
        assert row.id == attempt.record_id and row.snapshot_kind == "room"
        assert attempt.image_path.name == f"{attempt.record_id}.jpg"
        assert not list(attempt.image_path.parent.glob("*passwd*"))


def test_save_snapshot_applies_per_kind_limits_and_strips_metadata(tmp_path):
    target = tmp_path / "private" / "x.jpg"
    assert save_snapshot(jpeg(exif=True), target, "room") == (1280, 651)
    assert not Image.open(target).getexif()
    assert save_snapshot(jpeg(size=(1920, 1080)), tmp_path / "private" / "y.jpg", "camera") == (
        640,
        360,
    )
    assert save_snapshot(jpeg(size=(748, 1480)), tmp_path / "private" / "z.jpg", "room") == (
        748,
        1480,
    )
    assert save_snapshot(jpeg(size=(1900, 1600)), tmp_path / "private" / "w.jpg", "room")[0] <= 1280
    with pytest.raises(InvalidSnapshot):
        save_snapshot(jpeg(), tmp_path / "private" / "q.jpg", "unknown")
    with pytest.raises(InvalidSnapshot):
        save_snapshot(b"", tmp_path / "private" / "e.jpg", "room")
    assert oct(target.parent.stat().st_mode & 0o777) == "0o700"
    assert not list(target.parent.glob(".*tmp"))


def test_historical_records_keep_their_files_and_are_labelled_camera_only(
    tmp_path, admin_email, live
):
    """A record from before room images: NULL kind with an old still on disk."""
    with Attempt(tmp_path, admin_email, live, 51017) as attempt:
        attempt.image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (90, 90, 90)).save(attempt.image_path, "JPEG")

        async def make_historical():
            async with attempt.store.db_sessions.begin() as db:
                await db.execute(
                    update(InterviewRecord)
                    .where(InterviewRecord.id == attempt.record_id)
                    .values(snapshot_status="available", snapshot_kind=None)
                )

        attempt.client.portal.call(make_historical)
        attempt.finish()
        reader = PdfReader(attempt.store.pdf_path(attempt.record_id))
        text = " ".join("\n".join(p.extract_text() for p in reader.pages).split())
        assert "CANDIDATE CAMERA SNAPSHOT" in text
        assert "from before interview-room images were introduced" in text
        assert "INTERVIEW ROOM IMAGE" not in text and "not an image of the interview room" in text
        assert attempt.image_path.is_file()  # the old file is untouched

        # Regeneration works from the same stored evidence and does not relabel it.
        async def fail_pdf():
            async with attempt.store.db_sessions.begin() as db:
                await db.execute(
                    update(InterviewRecord)
                    .where(InterviewRecord.id == attempt.record_id)
                    .values(pdf_status="failed", pdf_error="Test")
                )

        attempt.client.portal.call(fail_pdf)
        with TestClient(
            make_app(tmp_path / "admin"), client=(f"test-{uuid.uuid4().hex}", 51018)
        ) as admin:
            headers = login(admin, admin_email)
            queued = admin.post(
                f"/api/admin/interviews/{attempt.record_id}/pdf/regenerate", headers=headers
            )
            assert queued.status_code == 202
        attempt.client.portal.call(attempt.store.generate_pdf, attempt.record_id)
        again = " ".join(
            "\n".join(
                p.extract_text() for p in PdfReader(attempt.store.pdf_path(attempt.record_id)).pages
            ).split()
        )
        assert "from before interview-room images were introduced" in again


def test_new_transcript_rows_record_utterance_kind_and_question_number(tmp_path, admin_email, live):
    with Attempt(tmp_path, admin_email, live, 51019) as attempt:
        attempt.event(
            "agent_prepared",
            speaker="agent",
            utterance_id=1,
            text="Describe your role.",
            utterance_kind="question",
            question_number=1,
        )
        attempt.event(
            "agent_prepared",
            speaker="agent",
            utterance_id=2,
            text="Odd one.",
            utterance_kind="x" * 80,
            question_number=-4,
        )
        attempt.event(
            "candidate_final",
            speaker="candidate",
            text="I led it.",
            turn_number=1,
            question_number=1,
        )
        rows = attempt.client.portal.call(_transcript, attempt.store, attempt.record_id)
        assert [(r.speaker, r.utterance_kind, r.question_number) for r in rows] == [
            ("agent", "question", 1),
            ("agent", "x" * 24, None),  # bounded, and an invalid number is dropped
            ("candidate", None, None),  # candidate rows are never labelled with a question
        ]


async def _transcript(store, record_id):
    from rehearse_ai.models import TranscriptEntry

    async with store.db_sessions() as db:
        return list(
            (
                await db.scalars(
                    select(TranscriptEntry)
                    .where(TranscriptEntry.interview_id == record_id)
                    .order_by(TranscriptEntry.sequence)
                )
            ).all()
        )
