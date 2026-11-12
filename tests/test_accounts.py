"""Account routes against the dedicated PostgreSQL test database."""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from rehearse_ai.accounts import MailDeliveryError, digest, utcnow
from rehearse_ai.accounts_db import ActionToken, SessionToken
from rehearse_ai.config import AuthConfig, Settings
from rehearse_ai.server import create_app

TEST_URL = os.environ.get(
    "REHEARSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
)
assert TEST_URL.rsplit("/", 1)[-1].endswith("_test"), "Refusing to test against a non-test database"
PASSWORD = "correct horse battery 1234"


@pytest.fixture
def account_client(tmp_path):
    auth = AuthConfig(
        database_url=TEST_URL, secret="test-only-secret-at-least-thirty-two-characters"
    )
    settings = Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        auth=auth,
        session_dir=tmp_path,
    )
    app = create_app(settings)
    sent = []

    async def capture(to, purpose, token):
        sent.append((to, purpose, token))

    app.state.accounts.mailer.send_link = capture
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50000)) as client:
        csrf = client.get("/api/csrf").json()["token"]
        headers = {"X-CSRF-Token": csrf}
        yield client, headers, sent


def address():
    return f"candidate-{uuid.uuid4().hex}@example.com"


def post(client, headers, path, **body):
    return client.post(path, json=body, headers=headers)


def signup(client, headers, email):
    response = post(client, headers, "/api/auth/signup", email=email, password=PASSWORD)
    assert response.status_code == 202


def verify(client, headers, sent):
    token = next(token for _, purpose, token in reversed(sent) if purpose == "verify")
    response = post(client, headers, "/api/auth/verify", token=token)
    assert response.status_code == 200
    return token


def expire_token(token):
    async def change():
        engine = create_async_engine(TEST_URL)
        async with engine.begin() as db:
            await db.execute(
                update(ActionToken)
                .where(ActionToken.digest == digest(token))
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )
        await engine.dispose()

    asyncio.run(change())


def expire_session(token):
    async def change():
        engine = create_async_engine(TEST_URL)
        async with engine.begin() as db:
            await db.execute(
                update(SessionToken)
                .where(SessionToken.token == token)
                .values(created_at=utcnow() - timedelta(days=1))
            )
        await engine.dispose()

    asyncio.run(change())


def test_signup_duplicate_and_unverified_denial(account_client):
    client, headers, sent = account_client
    email = address()
    weak = post(client, headers, "/api/auth/signup", email=email, password="short")
    assert weak.status_code == 400
    assert sent == []
    signup(client, headers, email)
    signup(client, headers, email.upper())
    assert len(sent) == 1
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 401
    )
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    assert client.get("/interview", follow_redirects=False).status_code == 303
    assert post(client, headers, "/api/offer", type="bad").status_code == 401


def test_verification_expiry_reuse_and_resend_limit(account_client):
    client, headers, sent = account_client
    email = address()
    signup(client, headers, email)
    old = sent[-1][2]
    expire_token(old)
    assert post(client, headers, "/api/auth/verify", token=old).status_code == 400
    assert post(client, headers, "/api/auth/resend-verification", email=email).status_code == 202
    new = sent[-1][2]
    assert new != old
    assert post(client, headers, "/api/auth/verify", token=old).status_code == 400
    assert post(client, headers, "/api/auth/verify", token=new).status_code == 200
    assert post(client, headers, "/api/auth/verify", token=new).status_code == 400
    other = address()
    signup(client, headers, other)
    initial = sent[-1][2]
    for _ in range(3):
        assert (
            post(client, headers, "/api/auth/resend-verification", email=other).status_code == 202
        )
    assert post(client, headers, "/api/auth/resend-verification", email=other).status_code == 429
    assert post(client, headers, "/api/auth/verify", token=initial).status_code == 400
    assert post(client, headers, "/api/auth/verify", token=sent[-1][2]).status_code == 200


def test_login_session_expiry_logout_and_cookie_settings(account_client):
    client, headers, sent = account_client
    email = address()
    signup(client, headers, email)
    verify(client, headers, sent)
    assert (
        post(client, headers, "/api/auth/login", email=email, password="wrong").status_code == 401
    )
    login = post(client, headers, "/api/auth/login", email=email, password=PASSWORD)
    assert login.status_code == 204
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=43200" in cookie
    assert "secure" not in cookie
    assert client.get("/api/auth/me").json()["email"] == email
    expire_session(client.cookies["rehearse_session"])
    assert client.get("/api/auth/me").status_code == 401
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 204
    )
    assert post(client, headers, "/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_reset_expiry_reuse_and_existing_sessions_revoked(account_client):
    client, headers, sent = account_client
    email = address()
    signup(client, headers, email)
    verify(client, headers, sent)
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 204
    )
    assert post(client, headers, "/api/auth/request-reset", email=email).status_code == 202
    old = sent[-1][2]
    expire_token(old)
    assert (
        post(
            client,
            headers,
            "/api/auth/reset-password",
            token=old,
            password="new correct horse battery 5678",
        ).status_code
        == 400
    )
    assert post(client, headers, "/api/auth/request-reset", email=email).status_code == 202
    new = sent[-1][2]
    assert (
        post(
            client, headers, "/api/auth/reset-password", token=new, password="too short"
        ).status_code
        == 400
    )
    assert (
        post(
            client,
            headers,
            "/api/auth/reset-password",
            token=new,
            password="new correct horse battery 5678",
        ).status_code
        == 200
    )
    assert client.get("/api/auth/me").status_code == 401
    assert (
        post(
            client,
            headers,
            "/api/auth/reset-password",
            token=new,
            password="another correct horse battery 9",
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
            password="new correct horse battery 5678",
        ).status_code
        == 204
    )


def test_protected_routes_csrf_and_honest_dashboard(account_client):
    client, headers, sent = account_client
    invalid = post(
        client,
        headers,
        "/api/auth/signup",
        email="not-an-email",
        password="sensitive-test-password",
    )
    assert invalid.status_code == 422
    assert "sensitive-test-password" not in invalid.text
    assert (
        client.post("/api/auth/signup", json={"email": address(), "password": PASSWORD}).status_code
        == 403
    )
    email = address()
    assert (
        client.post(
            "/api/auth/signup",
            json={"email": email, "password": PASSWORD},
            headers={**headers, "Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    signup(client, headers, email)
    verify(client, headers, sent)
    assert (
        post(client, headers, "/api/auth/login", email=email, password=PASSWORD).status_code == 204
    )
    assert client.get("/dashboard").status_code == 200
    page = client.get("/dashboard").text
    assert "No interviews have been assigned yet" in page
    assert "No completed interviews are saved yet" in page
    assert client.get("/interview").status_code == 200
    assert client.post("/api/offer", json={"type": "bad"}).status_code == 403
    assert post(client, headers, "/api/offer", type="bad").status_code == 400
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/login?next=https://attacker.example").status_code == 200


def test_production_cookie_is_secure():
    auth = AuthConfig(
        environment="production", public_base_url="https://rehearse.example", smtp_starttls=True
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from rehearse_ai.accounts import AccountService

    service = AccountService(auth, async_sessionmaker())
    assert service.cookies.cookie_secure is True
    assert service.cookies.cookie_httponly is True
    assert service.cookies.cookie_samesite == "lax"
    assert "secure" in service.csrf_response().headers["set-cookie"].lower()


def test_login_rate_limit_and_generic_reset_request(account_client):
    client, headers, sent = account_client
    email = address()
    for _ in range(10):
        response = post(client, headers, "/api/auth/login", email=email, password="wrong")
        assert response.status_code == 401
    response = post(client, headers, "/api/auth/login", email=email, password="wrong")
    assert response.status_code == 429
    assert post(client, headers, "/api/auth/request-reset", email=email).status_code == 202
    assert sent == []


def test_smtp_failure_never_marks_account_verified(account_client):
    client, headers, _ = account_client
    email = address()

    async def fail(_to, _purpose, _token):
        raise MailDeliveryError("local capture unavailable")

    client.app.state.accounts.mailer.send_link = fail
    response = post(client, headers, "/api/auth/signup", email=email, password=PASSWORD)
    assert response.status_code == 503
    response = post(client, headers, "/api/auth/login", email=email, password=PASSWORD)
    assert response.status_code == 401
