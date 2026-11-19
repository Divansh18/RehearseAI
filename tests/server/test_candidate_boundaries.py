"""Stage 1 boundary regressions on the live assigned path: events, context, plans, access."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support import (
    FUTURE_QUESTION,
    SECRET_BASIS,
    SECRET_EVIDENCE,
    SECRET_GUIDANCE,
    TEST_URL,
    login,
    make_app,
    new_admin_email,
    plan_dict,
    seed_assignment,
)

from rehearse_ai.interview.browser_events import candidate_event
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.models import (
    CandidateSession,
    InterviewAssignment,
    InterviewRecord,
)

PRIVATE_MARKERS = (SECRET_GUIDANCE, SECRET_BASIS, SECRET_EVIDENCE)


def deliver(session):
    utterance = session.controller.pending
    context = f"assigned-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def settle():
    for _ in range(4):
        await asyncio.sleep(0)


# --- browser events -----------------------------------------------------------------


def test_allowlist_drops_internal_events_and_unknown_fields():
    internal = [
        {"event": "decision_proposed", "text": "RAW-MODEL-TEXT", "action": "advance"},
        {"event": "inference_started", "purpose": "assigned_decision"},
        {"event": "inference_completed", "validated": True},
        {"event": "question_covered_from_prior_turn", "evidence_quote": "quote"},
        {"event": "fact_remembered", "wording": "memory"},
        {"event": "stt_configured", "model": "x"},
        {"event": "assignment_session_authenticated", "assignment_id": "secret"},
        {"event": "coverage_summary", "summary": {"asked": [1]}},
        {"event": "questions_skipped_for_time", "question_numbers": [3]},
        {"event": "something_new_and_unknown"},
        {"no_event_key": True},
    ]
    assert [candidate_event(entry) for entry in internal] == [None] * len(internal)


def test_allowed_events_are_rebuilt_with_minimal_fields():
    prepared = candidate_event(
        {
            "event": "agent_prepared",
            "utterance_id": 4,
            "text": "Tell me about it.",
            "utterance_kind": "question",
            "question_number": 2,
            "generation": 9,
            "raw_prompt": "SYSTEM PROMPT",
            "plan": {"questions": []},
        }
    )
    assert prepared == {
        "event": "agent_prepared",
        "utterance_id": 4,
        "text": "Tell me about it.",
        "utterance_kind": "question",
        "question_number": 2,
    }
    error = candidate_event(
        {
            "event": "response_error",
            "failure_stage": "assigned_decision",
            "processor": "Traceback (most recent call last)",
            "error_type": "KeyError",
            "error": "secret detail",
            "error_code": "unexpected",
        }
    )
    assert error == {
        "event": "response_error",
        "failure_stage": "assigned_decision",
        "processor": None,
        "error_code": None,
    }
    assert candidate_event({"event": "agent_prepared", "utterance_id": "x", "text": "t"}) is None
    assert candidate_event({"event": "interview_complete", "extra": "x"}) == {
        "event": "interview_complete"
    }


@pytest.mark.asyncio
async def test_live_assigned_session_publishes_only_allowlisted_minimal_events(tmp_path):
    published, spoken = [], []

    async def infer(kind, payload):
        if kind == "assigned_closing":
            return {"text": "Thank you for your time. The hiring team will share updates."}
        return {"action": "advance", "text": "RAW-MODEL-TEXT", "covered_future_questions": []}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "boundary-events"),
        infer,
        speak,
        published.append,
        interview_plan=plan_dict(),
        opening=False,
        self_reported_name="Nyla Example",
    )
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built the ingestion service with two colleagues.")
    await settle()
    assert spoken[-1].text == plan_dict()["questions"][1]["question"]
    events = [message["event"] for message in published]
    names = {event["event"] for event in events}
    assert "decision_proposed" not in names and "inference_completed" not in names
    assert names <= {
        "session_started",
        "agent_prepared",
        "agent_output_started",
        "agent_delivery_reported",
        "candidate_started",
        "candidate_turn_completed",
        "persistence_error",
    }
    blob = json.dumps(published)
    for forbidden in (*PRIVATE_MARKERS, "RAW-MODEL-TEXT", "CLOSING-INSTRUCTION-TEXT"):
        assert forbidden not in blob
    # The internal log still keeps the evidence for the admin-side record.
    log = (tmp_path / "boundary-events.jsonl").read_text()
    assert "decision_proposed" in log


# --- model context ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_context_excludes_future_questions_and_private_plan_fields(tmp_path):
    payloads = []

    async def infer(kind, payload):
        payloads.append((kind, json.dumps(payload)))
        if kind == "assigned_closing":
            return {"text": "Thank you for your time. The hiring team will share updates."}
        return {"action": "advance", "text": "", "covered_future_questions": []}

    async def speak(_utterance):
        return None

    session = InterviewSession(
        SessionLog(tmp_path, "boundary-context"),
        infer,
        speak,
        lambda _event: None,
        interview_plan=plan_dict(),
        opening=False,
    )
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built the ingestion service with two colleagues.")
    await settle()
    first_kind, first = payloads[0]
    assert first_kind == "assigned_decision"
    plan = plan_dict()
    # The current question and its own guidance are supplied...
    assert plan["questions"][0]["question"] in first
    assert plan["questions"][0]["follow_up_guidance"] in first
    # ...future question text, guidance and every private field are not.
    for forbidden in (FUTURE_QUESTION, *PRIVATE_MARKERS, "CLOSING-INSTRUCTION-TEXT"):
        assert forbidden not in first
    assert "evidence_quote" not in first and "basis" not in first
    # Later questions appear only as short topic labels, for coverage recognition.
    assert '"topic": "Failure handling"' in first
    assert '"topic": "Mentoring"' in first
    data = json.loads(first)
    assert set(data["current_question"]) == {
        "question_number",
        "of",
        "topic",
        "question",
        "follow_up_guidance",
        "required",
    }
    assert "assigned_interview" not in data and "active_question_plan" not in data
    # Drive to the closing. It is fixed text, so no model sees the plan's closing notes.
    deliver(session)
    await session.user_started()
    await session.user_stopped("We retried with idempotent workers.")
    await settle()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I pair with them weekly.")
    await settle()
    assert not [text for kind, text in payloads if kind == "assigned_closing"]
    assert all("CLOSING-INSTRUCTION-TEXT" not in text for _, text in payloads)
    for _, text in payloads:
        data = json.loads(text)
        current = (data.get("current_question") or {}).get("question_number")
        for forbidden in (SECRET_BASIS, SECRET_EVIDENCE):
            assert forbidden not in text
        # A question's guidance is supplied only while that question is the current one.
        if current != 2:
            assert SECRET_GUIDANCE not in text


@pytest.mark.asyncio
async def test_decision_marked_validated_only_after_validation_succeeds(tmp_path):
    answers = iter(
        [
            {"action": "follow_up", "text": "One? Two?", "covered_future_questions": []},
            {"action": "advance", "text": "", "covered_future_questions": []},
        ]
    )

    async def infer(kind, payload):
        if kind == "assigned_closing":
            return {"text": "Thank you for your time."}
        return next(answers)

    async def speak(_utterance):
        return None

    session = InterviewSession(
        SessionLog(tmp_path, "boundary-validated"),
        infer,
        speak,
        lambda _event: None,
        interview_plan=plan_dict(),
        opening=False,
    )
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built the ingestion service with two colleagues.")
    await settle()
    rows = [
        json.loads(line)
        for line in (tmp_path / "boundary-validated.jsonl").read_text().splitlines()
    ]
    proposed = [row for row in rows if row["event"] == "decision_proposed"]
    assert proposed and proposed[0]["validated"] is False
    assert any(
        row["event"] == "decision_rejected" and row["reason"] == "invalid_spoken_text"
        for row in rows
    )
    assert any(row["event"] == "decision_fallback_advance" for row in rows)
    # No decision is called validated when the proposed follow-up was refused.
    assert not [
        row
        for row in rows
        if row["event"] == "inference_completed" and row.get("action") == "follow_up"
    ]


# --- stored plan validation and attempt consumption -----------------------------------


def candidate_for(app, token):
    """Redeem an invitation and confirm the name; returns a client context manager's pieces."""
    client = TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50001))
    return client


def open_invitation(client, token):
    headers = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
    assert (
        client.post(f"/api/invitations/{token}/redeem", json={}, headers=headers).status_code == 200
    )
    assert (
        client.post(
            "/api/candidate/name", json={"full_name": "Nyla Example"}, headers=headers
        ).status_code
        == 200
    )
    return headers


def run(coro):
    async def wrapper():
        engine = create_async_engine(TEST_URL)
        try:
            async with async_sessionmaker(engine).begin() as db:
                return await coro(db)
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


OFFER = {"sdp": "synthetic-offer", "type": "offer", "snapshot_acknowledged": True}


def test_invalid_stored_plan_fails_safely_without_consuming_the_attempt(tmp_path):
    admin_email = new_admin_email()
    broken = plan_dict()
    broken["questions"] = []  # no longer satisfies the current schema
    assignment_id, _version, token = asyncio.run(seed_assignment(admin_email, content=broken))
    app = make_app(tmp_path)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50002)) as candidate:
        headers = open_invitation(candidate, token)
        info = candidate.get("/api/candidate/interview").json()
        assert info["plan_state"] == "unavailable" and info["can_begin"] is False
        response = candidate.post("/api/offer", json=OFFER, headers=headers)
        assert response.status_code == 409
        assert "unavailable" in response.json()["detail"].lower()
        assert "questions" not in response.text
        # The attempt was not consumed: the assignment is still unstarted and has no record.
        again = candidate.post("/api/offer", json=OFFER, headers=headers)
        assert again.status_code == 409 and "already started" not in again.text

    async def check(db):
        record = await db.scalar(
            select(func.count())
            .select_from(InterviewRecord)
            .where(InterviewRecord.assignment_id == uuid.UUID(assignment_id))
        )
        status = await db.scalar(
            select(InterviewAssignment.status).where(
                InterviewAssignment.id == uuid.UUID(assignment_id)
            )
        )
        return record, status

    assert run(check) == (0, "assigned")


def test_pending_preparation_blocks_begin_but_allows_welcome_and_setup(tmp_path):
    admin_email = new_admin_email()
    assignment_id, _version, token = asyncio.run(
        seed_assignment(admin_email, plan=False, preparation_status="pending", target_minutes=25)
    )
    app = make_app(tmp_path)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50003)) as candidate:
        preview = candidate.get(f"/api/invitations/{token}")
        assert preview.status_code == 200
        assert preview.json()["plan_state"] == "preparing"
        assert preview.json()["target_minutes"] == 25
        assert "questions" not in preview.text
        headers = open_invitation(candidate, token)
        assert candidate.get("/candidate/welcome").status_code == 200
        assert candidate.get("/interview").status_code == 200
        info = candidate.get("/api/candidate/interview").json()
        assert info["can_begin"] is False and info["plan_state"] == "preparing"
        response = candidate.post("/api/offer", json=OFFER, headers=headers)
        assert response.status_code == 409 and "prepared" in response.json()["detail"]

    async def mark_ready_without_plan(db):
        await db.execute(
            update(InterviewAssignment)
            .where(InterviewAssignment.id == uuid.UUID(assignment_id))
            .values(preparation_status="ready")
        )

    # A "ready" flag with no plan version never lets Begin through either.
    run(mark_ready_without_plan)
    with TestClient(make_app(tmp_path), client=(f"test-{uuid.uuid4().hex}", 50004)) as other:
        headers = open_invitation(other, token)
        assert other.get("/api/candidate/interview").json()["can_begin"] is False
        assert other.post("/api/offer", json=OFFER, headers=headers).status_code == 409

    async def count(db):
        return await db.scalar(
            select(func.count())
            .select_from(InterviewRecord)
            .where(InterviewRecord.assignment_id == uuid.UUID(assignment_id))
        )

    assert run(count) == 0


# --- rotation ------------------------------------------------------------------------


def test_resend_rotates_token_and_revokes_older_candidate_sessions(tmp_path):
    admin_email = new_admin_email()
    assignment_id, _version, token = asyncio.run(seed_assignment(admin_email))
    admin_app = make_app(tmp_path)
    with TestClient(admin_app, client=(f"test-{uuid.uuid4().hex}", 50005)) as admin:
        admin_headers = login(admin, admin_email)
        with TestClient(
            make_app(tmp_path), client=(f"test-{uuid.uuid4().hex}", 50006)
        ) as candidate:
            open_invitation(candidate, token)
            assert candidate.get("/api/candidate/interview").status_code == 200
            resend = admin.post(
                f"/api/admin/assignments/{assignment_id}/resend", headers=admin_headers
            )
            assert resend.status_code == 200 and resend.json() == {"mail_status": "sent"}
            new_token = admin_app.state.sent_mail[-1][1]
            assert new_token != token
            # Old cookie, old link: both dead at once. The new link works.
            assert candidate.get("/api/candidate/interview").status_code == 401
            assert candidate.get(f"/api/invitations/{token}").status_code == 404

            async def live_sessions(db):
                return await db.scalar(
                    select(func.count())
                    .select_from(CandidateSession)
                    .where(
                        CandidateSession.assignment_id == uuid.UUID(assignment_id),
                        CandidateSession.revoked_at.is_(None),
                    )
                )

            assert run(live_sessions) == 0
            open_invitation(candidate, new_token)
            assert candidate.get("/api/candidate/interview").status_code == 200
            assert run(live_sessions) == 1


@pytest.mark.parametrize("status", ["starting", "in_progress", "completed", "cancelled"])
def test_resend_is_refused_once_the_interview_has_started_or_ended(tmp_path, status):
    admin_email = new_admin_email()
    assignment_id, _version, token = asyncio.run(seed_assignment(admin_email))

    async def set_status(db):
        await db.execute(
            update(InterviewAssignment)
            .where(InterviewAssignment.id == uuid.UUID(assignment_id))
            .values(status=status, updated_at=datetime.now(UTC) + timedelta(seconds=1))
        )

    run(set_status)
    app = make_app(tmp_path)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50007)) as admin:
        headers = login(admin, admin_email)
        response = admin.post(f"/api/admin/assignments/{assignment_id}/resend", headers=headers)
        assert response.status_code == 409
        assert app.state.sent_mail == []

    async def digest_unchanged(db):
        return await db.scalar(
            select(InterviewAssignment.invitation_digest).where(
                InterviewAssignment.id == uuid.UUID(assignment_id)
            )
        )

    from rehearse_ai.invitations.access import token_digest

    assert run(digest_unchanged) == token_digest(token)
