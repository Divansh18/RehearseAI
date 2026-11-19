"""The real application writes lifecycle and HTTP events to the operational log."""

import json
import uuid

from fastapi.testclient import TestClient
from support import make_app

from rehearse_ai.config import LogSettings
from rehearse_ai.operational_log import configure_logging


def test_application_lifecycle_and_failed_requests_are_logged(tmp_path):
    handle = configure_logging(
        LogSettings(directory=tmp_path / "logs", console=False), build_id="integration"
    )
    token = "I" * 43
    try:
        with TestClient(make_app(tmp_path), client=(f"test-{uuid.uuid4().hex}", 50020)) as client:
            missing = client.get(f"/api/invitations/{token}")
            assert missing.status_code == 404
            assert client.get("/healthz").status_code == 200
            request_id = missing.headers["x-request-id"]
    finally:
        handle.shutdown()
    written = (tmp_path / "logs" / "application.jsonl").read_text()
    assert token not in written
    rows = [json.loads(line) for line in written.splitlines()]
    events = [r["event"] for r in rows]
    assert events[0] == "log.configured"
    assert events.index("app.started") < events.index("app.stopping") < events.index("app.stopped")
    started = next(r for r in rows if r["event"] == "app.started")
    assert started["session_dir"].startswith("/") and started["build_id"] == "integration"
    rejected = next(r for r in rows if r["event"] == "http.request_rejected")
    assert rejected["route"] == "/api/invitations/{token}" and rejected["request_id"] == request_id
    assert not [r for r in rows if r.get("route") == "/healthz"]  # successes are not logged
