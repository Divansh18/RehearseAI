"""Assigned interview security and controller regressions with invented data."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehearse_ai.accounts.cli import create_admin
from rehearse_ai.config import AuthConfig, Settings
from rehearse_ai.interview.controller import (
    InterviewController,
    InvalidTransition,
    ReplyKind,
    Stage,
)
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.invitations.access import invitation_token, token_digest
from rehearse_ai.models import (
    Account,
    InterviewAssignment,
    InterviewPlan,
    InterviewPlanVersion,
)
from rehearse_ai.server import create_app

TEST_URL = os.environ.get(
    "REHEARSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
)
assert TEST_URL.rsplit("/", 1)[-1].endswith("_test")
PASSWORD = "invented test password 1234"
PLAN = {
    "title": "Backend interview",
    "purpose": "Discuss backend engineering experience",
    "questions": [
        {
            "topic": "System design",
            "question": "Describe a backend system you designed.",
            "required": True,
            "follow_up_guidance": "Ask about ownership",
            "max_probes": 1,
        },
        {
            "topic": "Trade-offs",
            "question": "What trade-off mattered most?",
            "required": True,
            "follow_up_guidance": "Ask about alternatives",
            "max_probes": 0,
        },
    ],
    "focus_areas": ["APIs"],
    "target_minutes": 25,
    "interviewer_style": "Calm, concise and curious",
    "closing_instructions": "Thank the candidate briefly",
}


async def existing_assignment(admin_email, *, version_id=None, candidate_email=None, start=None):
    """Seed a frozen historical plan/assignment without a retired authoring API."""
    engine = create_async_engine(TEST_URL)
    token = invitation_token()
    instant = datetime.now(UTC)
    start = start or instant - timedelta(minutes=2)
    try:
        async with async_sessionmaker(engine).begin() as db:
            if version_id is None:
                admin_id = await db.scalar(select(Account.id).where(Account.email == admin_email))
                blueprint = InterviewPlan(
                    created_by=admin_id,
                    draft=PLAN.copy(),
                    created_at=instant,
                    updated_at=instant,
                )
                db.add(blueprint)
                await db.flush()
                version = InterviewPlanVersion(
                    plan_id=blueprint.id,
                    version=1,
                    content=PLAN.copy(),
                    published_at=instant,
                )
                db.add(version)
                await db.flush()
                version_id = version.id
            assignment = InterviewAssignment(
                plan_version_id=version_id,
                candidate_email=candidate_email or f"invented-{uuid.uuid4().hex}@example.com",
                candidate_name="Nyla Example",
                adjustments={"focus_areas": ["Queues"]},
                candidate_instructions="Have a quiet place ready.",
                scheduled_start_at=start,
                latest_start_at=instant + timedelta(minutes=30),
                timezone="Etc/UTC",
                status="assigned",
                invitation_digest=token_digest(token),
                invitation_expires_at=instant + timedelta(minutes=30),
                mail_status="sent",
                created_at=instant,
                updated_at=instant,
            )
            db.add(assignment)
            await db.flush()
            return str(assignment.id), str(version_id), token
    finally:
        await engine.dispose()


def deliver(session):
    utterance = session.controller.pending
    context = f"assigned-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_assigned_questions_feedback_refusal_and_closing(tmp_path):
    spoken = []
    kinds = []

    async def infer(kind, payload):
        kinds.append(kind)
        if kind == "assigned_closing":
            return {"text": "Thank you for your time. The hiring team will be in touch."}
        return {"action": "advance", "text": ""}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "assigned-unit"),
        infer,
        speak,
        lambda _event: None,
        interview_plan=PLAN,
        opening=False,
        self_reported_name="Nyla Example",
    )
    assert session.controller.preferred_name == "Nyla Example"
    await session.start()
    assert spoken[-1].text == PLAN["questions"][0]["question"]
    deliver(session)
    await session.user_started()
    await session.user_stopped("Could you give me feedback?")
    assert "can't provide interview feedback" in spoken[-1].text
    assert session.controller.answers[0] == ()
    assert session.controller.remaining_follow_ups == 1
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built a service for document ingestion.")
    await settle()
    assert spoken[-1].text == PLAN["questions"][1]["question"]
    deliver(session)
    await session.user_started()
    await session.user_stopped("We chose simple queues over a broker.")
    await settle()
    assert spoken[-1].text == "That concludes the interview. Thank you for your time, Nyla."
    assert "hiring" not in spoken[-1].text and "feedback" not in kinds
    assert "assigned_closing" not in kinds  # the closing is fixed text, never model-written
    assert session.controller.stage is Stage.DELIVERING
    deliver(session)
    assert session.controller.stage is Stage.COMPLETE


@pytest.mark.asyncio
async def test_correction_cancels_unheard_probe_and_replans(tmp_path):
    spoken = []

    async def infer(kind, payload):
        if kind == "assigned_decision":
            if "technical_terms" not in payload["session_memory"]:
                return {"action": "follow_up", "text": "Which web books did you use?"}
            return {"action": "advance", "text": ""}
        return {"text": "Thank you for your time."}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "assigned-correction"),
        infer,
        speak,
        lambda _event: None,
        interview_plan=PLAN,
        opening=False,
    )
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I used web books for event delivery.")
    await settle()
    old_probe = session.controller.pending
    assert old_probe.text == "Which web books did you use?"
    assert session.controller.remaining_follow_ups == 0
    await session.user_started()
    await session.user_stopped("I said webhooks, not web books.")
    acknowledgement = session.controller.pending
    assert acknowledgement.id != old_probe.id
    assert session.controller.answers[0] == ("I used web books for event delivery.",)
    assert session.controller.remaining_follow_ups == 1
    assert session.controller.memory.get("technical_terms").value == "webhooks"
    deliver(session)
    await settle()
    assert session.controller.pending.text == PLAN["questions"][1]["question"]
    assert not session.controller.delivery_finished(old_probe.id)


@pytest.mark.asyncio
async def test_spoken_stop_latches_over_pending_assigned_response(tmp_path):
    spoken = []

    async def infer(kind, _payload):
        return {"action": "follow_up", "text": "Which component did you own?"}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "assigned-stop"),
        infer,
        speak,
        lambda _event: None,
        interview_plan=PLAN,
        opening=False,
    )
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built the ingestion service.")
    await settle()
    pending_probe = session.controller.pending
    await session.user_started()
    await session.user_stopped("Please end the interview.")
    assert session.controller.pending.text.startswith("Thank you for your time")
    assert not session.controller.delivery_finished(pending_probe.id)
    deliver(session)
    assert session.controller.stage is Stage.ENDED_EARLY
    await session.user_started()
    await session.user_stopped("Maybe answer another question")
    assert session.controller.stage is Stage.ENDED_EARLY


def test_required_coverage_and_probe_limits():
    controller = InterviewController(
        tuple(item["question"] for item in PLAN["questions"]),
        probe_limits=(1, 0),
        required=(True, True),
    )
    first = controller.start()
    assert first.text == PLAN["questions"][0]["question"]
    controller.delivery_finished(first.id)
    with pytest.raises(InvalidTransition):
        controller.skip_current()
    controller.submit_answer("I designed the ingestion service")
    follow_up = controller.propose(ReplyKind.FOLLOW_UP, "What did you own?")
    controller.delivery_finished(follow_up.id)
    controller.submit_answer("I owned its API")
    next_question = controller.propose(ReplyKind.ADVANCE)
    assert next_question.text == PLAN["questions"][1]["question"]
    controller.delivery_finished(next_question.id)
    controller.submit_answer("We chose a queue")
    assert controller.remaining_follow_ups == 0
    assert controller.propose(ReplyKind.ADVANCE) is None
    closing = controller.prepare_closing("Thank you for your time.")
    assert controller.stage is Stage.DELIVERING
    assert controller.delivery_finished(closing.id)
    assert controller.stage is Stage.COMPLETE


@pytest.mark.asyncio
async def test_prior_answer_coverage_skips_redundant_authored_question(tmp_path):
    plan = {
        **PLAN,
        "questions": [
            *PLAN["questions"],
            {
                "topic": "Testing",
                "question": "How did you test the system?",
                "required": True,
                "follow_up_guidance": "Ask about validation",
                "max_probes": 0,
            },
        ],
    }
    spoken = []

    async def infer(kind, _payload):
        if kind == "assigned_decision":
            return {
                "action": "advance",
                "text": "",
                "covered_future_questions": [
                    {"question_number": 2, "evidence_quote": "chose queues over a broker"},
                    {"question_number": 3, "evidence_quote": "not in the answer"},
                ],
            }
        return {"text": "Thank you for your time."}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "assigned-coverage"),
        infer,
        speak,
        lambda _event: None,
        interview_plan=plan,
        opening=False,
    )
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built the API and chose queues over a broker.")
    await settle()
    assert spoken[-1].text == "How did you test the system?"
    assert session.controller.covered_source_turns[1] == 1
    assert session.controller.covered_source_turns[2] is None
    assert session.controller.answers[1] == ("I built the API and chose queues over a broker.",)


def test_admin_plan_invitation_and_restricted_candidate_session(tmp_path, monkeypatch):
    captured = []

    class FakeLiveSession:
        def __init__(self, connection, _settings, **kwargs):
            self.connection = connection
            self.assignment_id = kwargs["assignment_id"]
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

    monkeypatch.setattr("rehearse_ai.server.routes.attempts.LiveSession", FakeLiveSession)
    monkeypatch.setattr(
        "rehearse_ai.server.app.SmallWebRTCRequestHandler.handle_web_request", accept
    )
    admin_email = f"admin-{uuid.uuid4().hex}@example.com"
    asyncio.run(
        create_admin(admin_email, PASSWORD, TEST_URL, "test-secret-at-least-thirty-two-characters")
    )
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        auth=AuthConfig(database_url=TEST_URL, secret="test-secret-at-least-thirty-two-characters"),
        session_dir=tmp_path / "sessions",
        pdf_dir=tmp_path / "pdfs",
        snapshot_dir=tmp_path / "snapshots",
    )
    app = create_app(settings)
    invitations = []

    async def capture(to, token, **details):
        invitations.append((to, token, details))

    app.state.accounts.mailer.send_invitation = capture
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50000)) as admin:
        csrf = admin.get("/api/csrf").json()["token"]
        headers = {"X-CSRF-Token": csrf}
        assert admin.get("/admin/assignments").status_code == 401
        assert (
            admin.post(
                "/api/auth/signup", json={"email": "candidate@example.com", "password": PASSWORD}
            ).status_code
            == 410
        )
        login = admin.post(
            "/api/auth/login", json={"email": admin_email, "password": PASSWORD}, headers=headers
        )
        assert login.status_code == 204
        assert admin.get("/admin/blueprints").status_code == 404
        assert admin.post("/api/admin/blueprints", json=PLAN, headers=headers).status_code == 404
        assert admin.get("/api/admin/blueprints").status_code == 404
        assert admin.get("/admin/assignments").status_code == 200
        dashboard = admin.get("/admin/interviews").text
        assert "Schedule interview" in dashboard and "being updated" not in dashboard
        assert admin.get("/admin/schedule").status_code == 200

        assignment_id, version_id, token = asyncio.run(existing_assignment(admin_email))
        payload = {
            "candidate_email": admin.get(f"/api/admin/assignments/{assignment_id}").json()[
                "candidate_email"
            ]
        }
        assert (
            admin.get(f"/api/admin/blueprint-versions/{version_id}").json()["content"]["title"]
            == PLAN["title"]
        )
        assert (
            admin.post("/api/admin/assignments", json=payload, headers=headers).status_code == 405
        )
        assert (
            admin.put(
                f"/api/admin/assignments/{assignment_id}", json=payload, headers=headers
            ).status_code
            == 405
        )
        assert len(invitations) == 0
        candidate_app = create_app(settings)
        with TestClient(candidate_app, client=(f"test-{uuid.uuid4().hex}", 50001)) as candidate:
            assert candidate.get(f"/join/{token}").status_code == 200
            preview = candidate.get(f"/api/invitations/{token}")
            assert preview.status_code == 200
            assert "questions" not in preview.text
            assert candidate.get(f"/api/invitations/{token}").status_code == 200
            csrf_candidate = candidate.get("/api/csrf").json()["token"]
            candidate_headers = {"X-CSRF-Token": csrf_candidate}
            assert candidate.post(f"/api/invitations/{token}/redeem", json={}).status_code == 403
            redeemed = candidate.post(
                f"/api/invitations/{token}/redeem", json={}, headers=candidate_headers
            )
            assert redeemed.status_code == 200, redeemed.text
            assert "httponly" in redeemed.headers["set-cookie"].lower()
            assert candidate.get(f"/api/admin/blueprint-versions/{version_id}").status_code == 401
            assert candidate.get("/api/admin/interviews").status_code == 401
            info = candidate.get("/api/candidate/interview")
            assert info.status_code == 200
            assert "questions" not in info.text
            assert (
                candidate.post(
                    "/api/candidate/name",
                    json={"full_name": "Nyla Example"},
                    headers=candidate_headers,
                ).status_code
                == 200
            )
            assert candidate.get("/interview").status_code == 200
            assert candidate.get("/api/interviews").status_code == 404
            assert candidate.get(f"/api/interviews/{uuid.uuid4()}/pdf").status_code == 401
            offer = {
                "sdp": "synthetic-offer",
                "type": "offer",
                "snapshot_acknowledged": True,
                "request_data": {"candidate_id": str(uuid.uuid4())},
            }
            assert candidate.post("/api/offer", json=offer).status_code == 403
            assert (
                candidate.post(
                    "/api/offer",
                    json={**offer, "snapshot_acknowledged": False},
                    headers=candidate_headers,
                ).status_code
                == 400
            )
            accepted = candidate.post("/api/offer", json=offer, headers=candidate_headers)
            assert accepted.status_code == 200, accepted.text
            first = uuid.UUID(captured[0]["session_id"])
            assert captured[0]["assignment_id"] == assignment_id
            # The live plan is the schema-validated dump: authored fields preserved, defaults added.
            assert [
                {key: item[key] for key in PLAN["questions"][0]}
                for item in captured[0]["interview_plan"]["questions"]
            ] == PLAN["questions"]
            assert (
                candidate.post("/api/offer", json=offer, headers=candidate_headers).status_code
                == 409
            )
            store = candidate_app.state.interview_store
            assert candidate.get("/api/candidate/interview").json()["status"] == "starting"
            log = SessionLog(store.logs, str(first))
            for event, values in (
                ("session_started", {}),
                ("candidate_final", {"speaker": "candidate", "text": "A synthetic answer"}),
                ("interview_complete", {}),
            ):
                candidate.portal.call(store.apply_event, log.record(event, **values))
            log.close()
            candidate.portal.call(store.pdf_jobs.join)
            assert candidate.get("/api/candidate/interview").json()["status"] == "completed"
            assert candidate.get(f"/api/admin/interviews/{first}/pdf").status_code == 401
            assert candidate.get(f"/api/interviews/{first}/transcript").status_code == 404
            assert (
                candidate.post(
                    f"/api/admin/interviews/{first}/pdf/regenerate", headers=candidate_headers
                ).status_code
                == 401
            )
        listed = admin.get(
            "/api/admin/interviews", params={"candidate_email": payload["candidate_email"]}
        )
        assert str(first) in {item["id"] for item in listed.json()["items"]}
        assert admin.get(f"/api/admin/interviews/{first}").status_code == 200
        assert (
            admin.get(f"/api/admin/interviews/{first}/transcript").json()[0]["text"]
            == "A synthetic answer"
        )
        assert admin.get(f"/api/admin/interviews/{first}/pdf").status_code == 200

        async def legacy_record():
            async with app.state.interview_store.db_sessions.begin() as db:
                account = Account(
                    id=uuid.uuid4(),
                    email=f"historical-{uuid.uuid4().hex}@example.com",
                    hashed_password="historical-test-hash",
                    is_active=True,
                    is_verified=False,
                    is_superuser=False,
                )
                db.add(account)
            return await app.state.interview_store.create_attempt(
                account.id, "historical-build", "historical-prompt"
            )

        historical = admin.portal.call(legacy_record)
        assert admin.get(f"/api/admin/interviews/{historical}").status_code == 200
        second_id, _, old_token = asyncio.run(
            existing_assignment(
                admin_email,
                version_id=uuid.UUID(version_id),
                candidate_email=f"later-{uuid.uuid4().hex}@example.com",
                start=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        assert (
            admin.post(f"/api/admin/assignments/{second_id}/resend", headers=headers).status_code
            == 200
        )
        new_token = invitations[-1][1]
        assert old_token != new_token
        other_app = create_app(settings)
        with TestClient(other_app, client=(f"test-{uuid.uuid4().hex}", 50002)) as other:
            assert other.get(f"/api/invitations/{old_token}").status_code == 404
            assert other.get(f"/api/invitations/{new_token}").status_code == 200
            other_csrf = other.get("/api/csrf").json()["token"]
            other_headers = {"X-CSRF-Token": other_csrf}
            assert (
                other.post(
                    f"/api/invitations/{new_token}/redeem", json={}, headers=other_headers
                ).status_code
                == 200
            )
            assert (
                other.post(
                    "/api/candidate/name", json={"full_name": "Ravi Example"}, headers=other_headers
                ).status_code
                == 200
            )
            assert other.get("/api/candidate/interview").json()["window_state"] == "too_early"
            with pytest.raises(ValueError, match="joining window"):
                other.portal.call(
                    other_app.state.interview_store.create_assignment_attempt,
                    uuid.UUID(second_id),
                    "build",
                    "prompt",
                )
            assert (
                other.post(
                    f"/api/interviews/{first}/snapshot",
                    content=b"synthetic",
                    headers={**other_headers, "Content-Type": "image/jpeg"},
                ).status_code
                == 404
            )
            assert (
                admin.post(
                    f"/api/admin/assignments/{second_id}/cancel", headers=headers
                ).status_code
                == 200
            )
            assert other.get("/api/candidate/interview").status_code == 401
            assert other.get(f"/api/invitations/{new_token}").status_code == 404
