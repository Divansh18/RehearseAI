"""The Schedule interview form: validation, atomic persistence, idempotency and privacy."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support import (
    JOB_TEXT,
    RESUME_TEXT,
    TEST_URL,
    FakeGenerator,
    generated_plan,
    login,
    make_app,
    make_docx,
    make_pdf,
    new_admin_email,
)

from rehearse_ai.accounts_db import (
    InterviewAssignment,
    InterviewJob,
    InterviewPreparation,
)
from rehearse_ai.scheduling import ScheduleRejected, resolve_window

PRIVATE_INSTRUCTIONS = "PRIVATE-INSTRUCTION-TEXT emphasise reliability and mentoring"


@pytest.fixture(scope="module")
def admin_email():
    return new_admin_email()


@pytest.fixture(autouse=True)
def quiet_job_queue():
    """The test database is shared: park leftover work so one test never runs another's jobs."""

    async def cancel(db):
        await db.execute(
            update(InterviewJob)
            .where(InterviewJob.status.in_(["queued", "running"]))
            .values(status="cancelled")
        )
        await db.commit()

    db_run(cancel)


def future_local(hours: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def fields(**overrides) -> dict:
    base = {
        "candidate_name": "Nyla Example",
        "candidate_email": f"candidate-{uuid.uuid4().hex[:10]}@example.com",
        "interviewer_instructions": PRIVATE_INSTRUCTIONS,
        "job_description_text": JOB_TEXT,
        "scheduled_local": future_local(),
        "timezone": "Etc/UTC",
        "target_minutes": "30",
        "idempotency_key": uuid.uuid4().hex,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def pdf_file(text: str = RESUME_TEXT, name: str = "resume.pdf", **kwargs):
    return ("resume", (name, make_pdf(text, **kwargs), "application/pdf"))


def submit(client, headers, data: dict, *files):
    # Always multipart, even without files, as the browser form does.
    parts = list(files) or [("unused", (None, "x"))]
    return client.post("/api/admin/schedule", data=data, files=parts, headers=headers)


def db_run(coro):
    async def wrapper():
        engine = create_async_engine(TEST_URL)
        try:
            async with async_sessionmaker(engine)() as db:
                return await coro(db)
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def by_key(key: str):
    async def query(db):
        prep = await db.scalar(
            select(InterviewPreparation).where(InterviewPreparation.idempotency_key == key)
        )
        if prep is None:
            return None, None, []
        assignment = await db.get(InterviewAssignment, prep.assignment_id)
        jobs = (
            await db.scalars(
                select(InterviewJob)
                .where(InterviewJob.assignment_id == prep.assignment_id)
                .order_by(InterviewJob.kind)
            )
        ).all()
        return assignment, prep, list(jobs)

    return db_run(query)


def test_schedule_saves_interview_and_queues_preparation_and_invitation(tmp_path, admin_email):
    generator = FakeGenerator(generated_plan())
    app = make_app(tmp_path, generator)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50100)) as admin:
        headers = login(admin, admin_email)
        data = fields()
        response = submit(admin, headers, data, pdf_file())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["created"] is True and set(body) == {"assignment_id", "created"}
        assert "PRIVATE" not in response.text
        # Saved and queued before any external call: nothing generated, nothing sent yet.
        assert generator.calls == [] and app.state.sent_mail == []
        assignment, prep, jobs = by_key(data["idempotency_key"])
        assert str(assignment.id) == body["assignment_id"]
        assert assignment.blueprint_version_id is None
        assert assignment.preparation_status == "pending" and assignment.mail_status == "pending"
        assert assignment.candidate_instructions == ""  # private instructions stay separate
        assert assignment.target_minutes == 30
        assert assignment.latest_start_at - assignment.scheduled_start_at == timedelta(minutes=30)
        assert prep.interviewer_instructions == PRIVATE_INSTRUCTIONS
        assert "PRIVATE-EVIDENCE-SPAN" in prep.resume_text
        assert [(job.kind, job.status) for job in jobs] == [
            ("invite", "queued"),
            ("prepare", "queued"),
        ]
        stored = list((tmp_path / "documents").iterdir())
        assert len(stored) == 1 and oct(stored[0].stat().st_mode & 0o777) == "0o600"
        # Running the jobs delivers the invitation and prepares the plan, independently.
        admin.portal.call(app.state.jobs.drain)
        assignment, prep, jobs = by_key(data["idempotency_key"])
        assert {job.kind: job.status for job in jobs} == {
            "invite": "succeeded",
            "prepare": "succeeded",
        }
        assert assignment.mail_status == "sent" and assignment.preparation_status == "ready"
        assert prep.blueprint_id is not None and assignment.blueprint_version_id is not None
        ((to, token, details),) = app.state.sent_mail
        assert to == data["candidate_email"].casefold()
        assert details["title"] == "Candidate interview" and details["target_minutes"] == 30
        assert len(token) >= 32
        prompt, payload = generator.calls[0]
        assert prompt == "plan_prepare" and payload["target_minutes"] == 30
        assert payload["private_interviewer_instructions"] == PRIVATE_INSTRUCTIONS


def test_invitation_is_delivered_even_when_preparation_keeps_failing(tmp_path, admin_email):
    from rehearse_ai.preparation import PlanProviderError

    app = make_app(tmp_path, FakeGenerator(PlanProviderError("RateLimitError")))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50101)) as admin:
        headers = login(admin, admin_email)
        data = fields()
        assert submit(admin, headers, data, pdf_file()).status_code == 201
        admin.portal.call(app.state.jobs.drain)
        assignment, _prep, jobs = by_key(data["idempotency_key"])
        states = {job.kind: job for job in jobs}
        assert assignment.mail_status == "sent" and len(app.state.sent_mail) == 1
        assert states["prepare"].status == "queued" and states["prepare"].attempts == 1
        assert states["prepare"].last_error == "model_error"
        assert assignment.preparation_status == "preparing"


INVALID_FORMS = [
    ({"candidate_email": "not-an-email"}, "candidate_email"),
    ({"candidate_name": "x"}, "candidate_name"),
    ({"target_minutes": "3"}, "target_minutes"),
    ({"target_minutes": "500"}, "target_minutes"),
    ({"interviewer_instructions": "x" * 2001}, "interviewer_instructions"),
    ({"idempotency_key": "short"}, "idempotency_key"),
    ({"timezone": "Mars/Olympus"}, "timezone"),
    ({"timezone": "../../etc/passwd"}, "timezone"),
    ({"scheduled_local": "next tuesday"}, "scheduled_local"),
    ({"scheduled_local": "2020-01-01T10:00"}, "scheduled_local"),
    ({"join_window_minutes": "1"}, "join_window_minutes"),
    ({"job_description_text": None}, "job_description"),
    ({"job_description_text": "Too short."}, "job_description"),
]


def test_invalid_forms_are_explained_and_save_nothing(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50102)) as admin:
        headers = login(admin, admin_email)
        for overrides, field in INVALID_FORMS:
            data = fields(**overrides)
            response = submit(admin, headers, data, pdf_file())
            assert response.status_code == 422, (overrides, response.text)
            errors = response.json()["detail"]
            assert field in {error["field"] for error in errors}, (overrides, errors)
            assert all(error["message"] for error in errors)
            assert by_key(data["idempotency_key"]) == (None, None, [])
        documents = tmp_path / "documents"
        assert not documents.exists() or not list(documents.iterdir())


def test_resume_problems_are_reported_by_field_and_leave_no_files(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50103)) as admin:
        headers = login(admin, admin_email)
        cases = {
            "missing": ([], "Upload the candidate"),
            "encrypted": ([pdf_file(encrypt=True)], "password-protected"),
            "scanned": ([pdf_file("", draw_text=False)], "OCR"),
            "txt": ([("resume", ("resume.txt", b"text" * 80, "text/plain"))], "PDF or DOCX"),
            "empty": ([("resume", ("resume.pdf", b"", "application/pdf"))], "Upload the candidate"),
        }
        for _name, (files, expected) in cases.items():
            data = fields()
            response = submit(admin, headers, data, *files)
            assert response.status_code == 422, response.text
            messages = {e["field"]: e["message"] for e in response.json()["detail"]}
            assert expected in messages["resume"]
            assert by_key(data["idempotency_key"]) == (None, None, [])
        assert not (tmp_path / "documents").exists() or not list((tmp_path / "documents").iterdir())


def test_job_description_as_docx_upload_and_both_sources_rejected(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50104)) as admin:
        headers = login(admin, admin_email)
        upload = (
            "job_description_file",
            ("role.docx", make_docx(JOB_TEXT), "application/octet-stream"),
        )
        both = submit(admin, headers, fields(), pdf_file(), upload)
        assert both.status_code == 422 and "not both" in both.text
        data = fields(job_description_text=None)
        accepted = submit(admin, headers, data, pdf_file(), upload)
        assert accepted.status_code == 201, accepted.text
        _assignment, prep, _jobs = by_key(data["idempotency_key"])
        assert prep.jd_source == "upload" and prep.jd_filename == "role.docx"
        assert "document processing platform" in prep.job_description_text
        scanned = (
            "job_description_file",
            ("role.pdf", make_pdf("", draw_text=False), "application/pdf"),
        )
        bad = submit(admin, headers, fields(job_description_text=None), pdf_file(), scanned)
        assert bad.status_code == 422
        assert "OCR" in {e["field"]: e["message"] for e in bad.json()["detail"]}["job_description"]


def test_repeated_and_conflicting_submissions_are_idempotent(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50105)) as admin:
        headers = login(admin, admin_email)
        data = fields()
        same_file = pdf_file()  # a double-click resends identical bytes
        first = submit(admin, headers, data, same_file)
        again = submit(admin, headers, data, same_file)
        assert first.status_code == 201 and again.status_code == 200
        assert again.json() == {"assignment_id": first.json()["assignment_id"], "created": False}
        changed = submit(admin, headers, {**data, "candidate_name": "Someone Else"}, same_file)
        assert changed.status_code == 409 and "already submitted" in changed.text
        _assignment, _prep, jobs = by_key(data["idempotency_key"])
        assert len(jobs) == 2
        assert len(list((tmp_path / "documents").iterdir())) == 1

        async def count(db):
            return await db.scalar(
                select(func.count())
                .select_from(InterviewAssignment)
                .where(InterviewAssignment.candidate_email == data["candidate_email"].casefold())
            )

        assert db_run(count) == 1


def test_form_requires_admin_csrf_and_bounded_size(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50106)) as admin:
        assert submit(admin, {}, fields(), pdf_file()).status_code == 401
        headers = login(admin, admin_email)
        assert submit(admin, {}, fields(), pdf_file()).status_code == 403
        huge = ("resume", ("resume.pdf", b"%PDF-" + b"0" * (12 * 1024 * 1024), "application/pdf"))
        assert submit(admin, headers, fields(), huge).status_code == 413
        big_but_allowed = (
            "resume",
            ("resume.pdf", b"%PDF-" + b"0" * (6 * 1024 * 1024), "application/pdf"),
        )
        too_large = submit(admin, headers, fields(), big_but_allowed)
        assert too_large.status_code == 422 and "larger than" in too_large.text
        plain = admin.post("/api/admin/schedule", json=fields(), headers=headers)
        assert plain.status_code == 415


def test_candidate_and_invitation_apis_never_expose_documents_or_private_instructions(
    tmp_path, admin_email
):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50107)) as admin:
        headers = login(admin, admin_email)
        data = fields()
        assert submit(admin, headers, data, pdf_file()).status_code == 201
        admin.portal.call(app.state.jobs.drain)
        token = app.state.sent_mail[-1][1]
        assignment, _prep, _jobs = by_key(data["idempotency_key"])
        listing = admin.get("/api/admin/assignments").text
        detail = admin.get(f"/api/admin/assignments/{assignment.id}")
        assert detail.status_code == 200
        for text in (listing, detail.text):
            assert "PRIVATE-EVIDENCE-SPAN" not in text and "PRIVATE-INSTRUCTION-TEXT" not in text
        assert detail.json()["preparation"]["has_private_instructions"] is True
        assert detail.json()["preparation"]["resume_filename"] == "resume.pdf"
        assert detail.json()["plan_versions"][0]["version"] == 1
        # Admins can inspect the generated plan, including private grounding notes.
        plan = admin.get(
            f"/api/admin/blueprint-versions/{detail.json()['blueprint_version_id']}"
        ).json()
        assert plan["content"]["questions"][0]["evidence_quote"]
    with TestClient(make_app(tmp_path), client=(f"test-{uuid.uuid4().hex}", 50108)) as candidate:
        collected = [
            candidate.get(f"/api/invitations/{token}").text,
            candidate.get(f"/join/{token}").text,
        ]
        csrf = {"X-CSRF-Token": candidate.get("/api/csrf").json()["token"]}
        assert (
            candidate.post(f"/api/invitations/{token}/redeem", json={}, headers=csrf).status_code
            == 200
        )
        collected.append(candidate.get("/api/candidate/interview").text)
        collected.append(candidate.get("/candidate/welcome").text)
        collected.append(candidate.get("/interview").text)
        blob = "\n".join(collected)
        for private in (
            "PRIVATE-EVIDENCE-SPAN",
            "PRIVATE-INSTRUCTION-TEXT",
            "Private note",
            "follow_up_guidance",
            "evidence_quote",
            "ingestion service",
        ):
            assert private not in blob
        info = json.loads(collected[2])
        assert info["plan_state"] == "ready" and "questions" not in info
        assert candidate.get(f"/api/admin/assignments/{assignment.id}").status_code == 401
        assert candidate.get("/api/admin/assignments").status_code == 401
        assert candidate.post("/api/admin/schedule", headers=csrf).status_code == 401


def test_revision_and_retry_endpoints_enforce_state(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50109)) as admin:
        headers = login(admin, admin_email)
        data = fields()
        assert submit(admin, headers, data, pdf_file()).status_code == 201
        assignment, _prep, _jobs = by_key(data["idempotency_key"])
        base = f"/api/admin/assignments/{assignment.id}"
        # Not ready yet: a revision is refused and retry is meaningless.
        assert (
            admin.post(
                f"{base}/revise", json={"request": "Add a testing question"}, headers=headers
            ).status_code
            == 409
        )
        assert admin.post(f"{base}/retry-preparation", headers=headers).status_code == 409
        admin.portal.call(app.state.jobs.drain)
        assert (
            admin.post(f"{base}/revise", json={"request": "x"}, headers=headers).status_code == 422
        )
        queued = admin.post(
            f"{base}/revise", json={"request": "Add a testing question"}, headers=headers
        )
        assert queued.status_code == 202
        # Only one change can be in flight.
        again = admin.post(f"{base}/revise", json={"request": "Make it shorter"}, headers=headers)
        assert again.status_code == 409
        assert admin.post(f"{base}/revise", json={"request": "Hello there"}).status_code == 403


def test_local_time_conversion_rejects_daylight_saving_gaps_and_overlaps():
    instant = datetime(2030, 1, 1, tzinfo=UTC)
    # Clocks move forward at 02:00 on 10 March 2030 in New York: 02:30 does not exist.
    with pytest.raises(ScheduleRejected) as gap:
        resolve_window("2030-03-10T02:30", "America/New_York", 30, instant=instant)
    assert "does not exist" in gap.value.errors[0]["message"]
    # Clocks move back on 3 November 2030: 01:30 happens twice.
    with pytest.raises(ScheduleRejected) as overlap:
        resolve_window("2030-11-03T01:30", "America/New_York", 30, instant=instant)
    assert "occurs twice" in overlap.value.errors[0]["message"]
    start, latest = resolve_window("2030-11-03T03:00", "America/New_York", 45, instant=instant)
    assert start == datetime(2030, 11, 3, 8, 0, tzinfo=UTC)  # EST, UTC-5
    assert latest - start == timedelta(minutes=45)
    start, _ = resolve_window("2030-07-01T09:00", "Asia/Kolkata", 30, instant=instant)
    assert start == datetime(2030, 7, 1, 3, 30, tzinfo=UTC)
    with pytest.raises(ScheduleRejected):
        resolve_window("2030-01-01T10:00+02:00", "Asia/Kolkata", 30, instant=instant)
    with pytest.raises(ScheduleRejected) as far:
        resolve_window("2035-06-01T10:00", "Etc/UTC", 30, instant=instant)
    assert "next year" in far.value.errors[0]["message"]
