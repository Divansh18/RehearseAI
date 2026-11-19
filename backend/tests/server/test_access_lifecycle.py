"""Which access changes end a running voice session, and which must not.

A voice session is authorised by a candidate session. Ending that access (the candidate leaves)
ends the call; administrator sign-out and password reset concern a different principal and must
leave candidate interviews alone.
"""

import asyncio
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from support import (
    login,
    make_app,
    new_admin_email,
    seed_assignment,
)

from rehearse_ai.models import utcnow  # noqa: F401  (kept for parity with sibling tests)


class FakeVoiceSession:
    def __init__(self, assignment_id: str) -> None:
        self.assignment_id = assignment_id
        self.log = SimpleNamespace(session_id=str(uuid.uuid4()))
        self.stopped = False
        self.disconnected = False
        self.connection = SimpleNamespace(disconnect=self._disconnect)

    async def _disconnect(self) -> None:
        self.disconnected = True

    async def stop(self) -> None:
        self.stopped = True


def redeem(client, token) -> dict:
    headers = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
    response = client.post(f"/api/invitations/{token}/redeem", json={}, headers=headers)
    assert response.status_code == 200
    return headers


def test_candidate_leaving_stops_only_that_assignments_voice_session(tmp_path):
    admin_email = new_admin_email()
    mine, _v, token = asyncio.run(seed_assignment(admin_email))
    other, _v2, _t2 = asyncio.run(seed_assignment(admin_email))
    app = make_app(tmp_path)
    registry = app.state.context.live
    ours, theirs = FakeVoiceSession(mine), FakeVoiceSession(other)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50010)) as candidate:
        headers = redeem(candidate, token)
        registry.sessions.update({ours, theirs})
        assert candidate.get("/api/candidate/interview").status_code == 200

        assert candidate.post("/api/candidate/logout", headers=headers).status_code == 204

        assert ours.stopped and ours.disconnected
        assert not theirs.stopped and not theirs.disconnected
        assert candidate.get("/api/candidate/interview").status_code == 401


def test_candidate_logout_without_a_session_stops_nothing(tmp_path):
    app = make_app(tmp_path)
    bystander = FakeVoiceSession(str(uuid.uuid4()))
    app.state.context.live.sessions.add(bystander)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50011)) as anonymous:
        headers = {"X-CSRF-Token": anonymous.get("/api/csrf").json()["token"]}
        assert anonymous.post("/api/candidate/logout", headers=headers).status_code == 204
        assert not bystander.stopped


def test_administrator_sign_out_leaves_candidate_interviews_running(tmp_path):
    admin_email = new_admin_email()
    assignment_id, _v, _token = asyncio.run(seed_assignment(admin_email))
    app = make_app(tmp_path)
    running = FakeVoiceSession(assignment_id)
    app.state.context.live.sessions.add(running)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50012)) as admin:
        headers = login(admin, admin_email)
        assert admin.post("/api/auth/logout", headers=headers).status_code == 204
        assert admin.get("/api/auth/me").status_code == 401
        assert not running.stopped and not running.disconnected


def test_administrator_password_reset_leaves_candidate_interviews_running(tmp_path):
    admin_email = new_admin_email()
    assignment_id, _v, _token = asyncio.run(seed_assignment(admin_email))
    app = make_app(tmp_path)
    sent = []

    async def capture(to, purpose, token):
        sent.append(token)

    app.state.accounts.mailer.send_link = capture
    running = FakeVoiceSession(assignment_id)
    app.state.context.live.sessions.add(running)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50013)) as admin:
        headers = login(admin, admin_email)
        assert (
            admin.post(
                "/api/auth/request-reset", json={"email": admin_email}, headers=headers
            ).status_code
            == 202
        )
        changed = admin.post(
            "/api/auth/reset-password",
            json={"token": sent[-1], "password": "another correct horse battery 5678"},
            headers=headers,
        )
        assert changed.status_code == 200
        assert admin.get("/api/auth/me").status_code == 401  # the admin's own sessions do end
        assert not running.stopped and not running.disconnected
