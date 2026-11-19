"""Admin authentication against the dedicated PostgreSQL test database."""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from rehearse_ai.accounts.cli import create_admin
from rehearse_ai.accounts.service import digest, utcnow
from rehearse_ai.config import AuthConfig, Settings
from rehearse_ai.database.models import AccountSessionToken, ActionToken
from rehearse_ai.server import create_app

TEST_URL = os.environ.get(
    "REHEARSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
)
assert TEST_URL.rsplit("/", 1)[-1].endswith("_test")
PASSWORD = "correct horse battery 1234"


@pytest.fixture
def admin_client(tmp_path):
    email = f"admin-{uuid.uuid4().hex}@example.com"
    secret = "test-secret-at-least-thirty-two-characters"
    asyncio.run(create_admin(email, PASSWORD, TEST_URL, secret))
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        auth=AuthConfig(database_url=TEST_URL, secret=secret),
        session_dir=tmp_path,
    )
    app = create_app(settings)
    sent = []

    async def capture(to, purpose, token):
        sent.append((to, purpose, token))

    app.state.accounts.mailer.send_link = capture
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50000)) as client:
        csrf = client.get("/api/csrf").json()["token"]
        yield client, {"X-CSRF-Token": csrf}, email, sent


def post(client, headers, route, **data):
    return client.post(route, json=data, headers=headers)


def change_timestamp(model, key, value, field):
    async def update_row():
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.begin() as db:
                await db.execute(update(model).where(field == key).values(**value))
        finally:
            await engine.dispose()

    asyncio.run(update_row())


def test_admin_login_expiry_logout_and_protected_routes(admin_client):
    client, headers, email, _ = admin_client
    assert client.get("/", follow_redirects=False).headers["location"] == "/login?reason=required"
    assert client.get("/admin/assignments").status_code == 401
    assert client.get("/interview").status_code == 401
    assert client.get("/api/admin/interviews").status_code == 401
    assert post(client, headers, "/api/offer", type="offer", sdp="synthetic").status_code == 401
    assert (
        post(client, headers, "/api/auth/login", email=email, password="wrong").status_code == 401
    )
    login = post(client, headers, "/api/auth/login", email=email, password=PASSWORD)
    assert login.status_code == 204
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=43200" in cookie
    assert "secure" not in cookie
    assert client.get("/api/auth/me").json()["role"] == "admin"
    assert client.get("/admin/assignments").status_code == 200
    assert client.get("/dashboard").status_code == 404
    assert client.get("/interview").status_code == 401
    token = client.cookies["rehearse_session"]
    change_timestamp(
        AccountSessionToken,
        token,
        {"created_at": utcnow() - timedelta(days=1)},
        AccountSessionToken.token,
    )
    assert client.get("/api/auth/me").status_code == 401
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 204
    )
    assert post(client, headers, "/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_no_public_signup_and_csrf(admin_client):
    client, headers, email, _ = admin_client
    assert (
        post(client, headers, "/api/auth/signup", email=email, password=PASSWORD).status_code == 410
    )
    assert client.get("/signup").status_code == 404
    assert (
        client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": email, "password": PASSWORD},
            headers={**headers, "Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/login?next=https://attacker.example").status_code == 200


def test_admin_password_reset_invalidates_session(admin_client):
    client, headers, email, sent = admin_client
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 204
    )
    assert post(client, headers, "/api/auth/request-reset", email=email).status_code == 202
    old = sent[-1][2]
    change_timestamp(
        ActionToken,
        digest(old),
        {"expires_at": utcnow() - timedelta(seconds=1)},
        ActionToken.digest,
    )
    assert (
        post(
            client,
            headers,
            "/api/auth/reset-password",
            token=old,
            password="another correct horse battery 5678",
        ).status_code
        == 400
    )
    assert post(client, headers, "/api/auth/request-reset", email=email).status_code == 202
    token = sent[-1][2]
    assert (
        post(
            client,
            headers,
            "/api/auth/reset-password",
            token=token,
            password="another correct horse battery 5678",
        ).status_code
        == 200
    )
    assert client.get("/api/auth/me").status_code == 401
    assert (
        post(
            client,
            headers,
            "/api/auth/reset-password",
            token=token,
            password="another correct horse battery 5678",
        ).status_code
        == 400
    )
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 401
    )
    assert (
        post(
            client,
            headers,
            "/api/auth/login",
            email=email,
            password="another correct horse battery 5678",
        ).status_code
        == 204
    )


def test_login_rate_limit_and_generic_reset_request(admin_client):
    client, headers, _, sent = admin_client
    absent = f"absent-{uuid.uuid4().hex}@example.com"
    assert post(client, headers, "/api/auth/request-reset", email=absent).status_code == 202
    assert sent == []
    for _ in range(10):
        assert (
            post(client, headers, "/api/auth/login", email=absent, password="wrong").status_code
            == 401
        )
    assert (
        post(client, headers, "/api/auth/login", email=absent, password="wrong").status_code == 429
    )
