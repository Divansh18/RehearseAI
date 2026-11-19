"""The HTTP surface: the exact set of routes, and who may reach the administrator ones.

The inventory is a tripwire for the router split and for later changes: adding or removing a
route must be a deliberate edit here. The sweeps prove that every administrator route refuses
callers with no session and callers holding only a candidate (invitation) session.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from support import make_app, new_admin_email, seed_assignment

EXPECTED_ROUTES = [
    ("/", "GET"),
    ("/admin/assignments", "GET"),
    ("/admin/interviews", "GET"),
    ("/admin/interviews/{record_id}", "GET"),
    ("/admin/schedule", "GET"),
    ("/api/admin/assignments", "GET"),
    ("/api/admin/assignments/{assignment_id}", "GET"),
    ("/api/admin/assignments/{assignment_id}/cancel", "POST"),
    ("/api/admin/assignments/{assignment_id}/resend", "POST"),
    ("/api/admin/assignments/{assignment_id}/retry-preparation", "POST"),
    ("/api/admin/assignments/{assignment_id}/revise", "POST"),
    ("/api/admin/blueprint-versions/{version_id}", "GET"),
    ("/api/admin/interviews", "GET"),
    ("/api/admin/interviews/{record_id}", "GET"),
    ("/api/admin/interviews/{record_id}/pdf", "GET"),
    ("/api/admin/interviews/{record_id}/pdf/regenerate", "POST"),
    ("/api/admin/interviews/{record_id}/transcript", "GET"),
    ("/api/admin/schedule", "POST"),
    ("/api/auth/login", "POST"),
    ("/api/auth/logout", "POST"),
    ("/api/auth/me", "GET"),
    ("/api/auth/request-reset", "POST"),
    ("/api/auth/reset-password", "POST"),
    ("/api/auth/signup", "POST"),
    ("/api/candidate/interview", "GET"),
    ("/api/candidate/logout", "POST"),
    ("/api/candidate/name", "POST"),
    ("/api/csrf", "GET"),
    ("/api/interviews/{record_id}/pdf", "GET"),
    ("/api/interviews/{record_id}/pdf/regenerate", "POST"),
    ("/api/interviews/{record_id}/snapshot", "POST"),
    ("/api/invitations/{token}", "GET"),
    ("/api/invitations/{token}/redeem", "POST"),
    ("/api/offer", "POST"),
    ("/assets/{name}", "GET"),
    ("/candidate/welcome", "GET"),
    ("/healthz", "GET"),
    ("/interview", "GET"),
    ("/join/{token}", "GET"),
    ("/login", "GET"),
    ("/reset-password", "GET"),
]


def routes_of(app) -> list[tuple[str, str]]:
    return sorted(
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    )


def concrete(path: str) -> str:
    """Fill path parameters with throwaway values."""
    out = path
    for name in ("record_id", "assignment_id", "version_id"):
        out = out.replace("{" + name + "}", str(uuid.uuid4()))
    return out.replace("{token}", "t" * 40).replace("{name}", "styles/theme.css")


def admin_routes(app) -> list[tuple[str, str]]:
    return [
        (path, method)
        for path, method in routes_of(app)
        if "/admin/" in path
        or path.startswith("/api/interviews/")
        and path.endswith(("pdf", "regenerate"))
    ]


def test_the_route_inventory_is_exactly_the_documented_one(tmp_path):
    assert routes_of(make_app(tmp_path)) == sorted(EXPECTED_ROUTES)


def test_there_are_administrator_routes_to_check(tmp_path):
    assert len(admin_routes(make_app(tmp_path))) >= 19


@pytest.mark.parametrize("session", ["none", "candidate"])
def test_every_administrator_route_refuses_anonymous_and_candidate_callers(tmp_path, session):
    app = make_app(tmp_path)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 52001)) as client:
        headers = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
        if session == "candidate":
            _id, _version, token = asyncio.run(seed_assignment(new_admin_email()))
            redeemed = client.post(f"/api/invitations/{token}/redeem", json={}, headers=headers)
            assert redeemed.status_code == 200
        for path, method in admin_routes(app):
            url = concrete(path)
            response = client.request(
                method,
                url,
                headers=headers,
                json={"request": "please change this"},
                follow_redirects=False,
            )
            # API routes answer 401; HTML pages send an anonymous visitor to sign in (303).
            allowed = {303, 401} if not path.startswith("/api/") else {401}
            assert response.status_code in allowed, (method, path, response.status_code)


def test_candidate_routes_refuse_callers_without_an_invitation_session(tmp_path):
    app = make_app(tmp_path)
    protected = [
        ("GET", "/candidate/welcome"),
        ("GET", "/interview"),
        ("GET", "/api/candidate/interview"),
        ("POST", "/api/candidate/name"),
        ("POST", "/api/offer"),
        ("POST", f"/api/interviews/{uuid.uuid4()}/snapshot"),
    ]
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 52002)) as client:
        headers = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
        for method, url in protected:
            response = client.request(method, url, headers=headers, json={"full_name": "Pat Doe"})
            assert response.status_code == 401, (method, url, response.status_code)


def test_public_paths_are_the_only_unauthenticated_pages_and_endpoints(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 52003)) as client:
        assert client.get("/healthz").json()["status"] == "ready"
        assert client.get("/login").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/assets/scripts/candidate/interview_room.js").status_code == 200
        assert client.get("/assets/styles/theme.css").status_code == 200
        # Only the allowlisted files are served: old flat names, unlisted files and traversal.
        for name in ("app.js", "not-listed.js", "pages/sign_in.html", "../../config.py"):
            assert client.get(f"/assets/{name}").status_code == 404, name
        assert client.post("/api/auth/signup").status_code == 410
