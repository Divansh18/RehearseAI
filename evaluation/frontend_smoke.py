"""Browser smoke test of every page: navigation, asset loading and console errors.

Starts a throwaway RehearseAI server against the isolated ``*_test`` database with invented data
(a synthetic administrator, one assignment, an invitation, a candidate session), then drives
Chromium through the sign-in, administrator, invitation and interview-room pages and fails on any
console error, failed request or non-200 asset. It does not start a voice interview, so it says
nothing about microphones, providers or WebRTC.

Playwright is deliberately not a project dependency. Run with a Python that has it installed:

    python3 evaluation/frontend_smoke.py            # from the repository root
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "backend" / ".venv" / "bin" / "python"
PORT = int(os.environ.get("SMOKE_PORT", "7971"))
TEST_URL = os.environ.get(
    "REHEARSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
)
assert TEST_URL.rsplit("/", 1)[-1].endswith("_test"), "refusing to use a non-test database"

SEED = """
import asyncio, json, sys
sys.path.insert(0, "backend/tests")
from support import new_admin_email, seed_assignment, PASSWORD
email = new_admin_email()
assignment_id, _version, token = asyncio.run(seed_assignment(email))
print("SEED" + json.dumps({"email": email, "password": PASSWORD, "token": token}))
"""


def main() -> int:
    seeded = subprocess.run(
        [str(PYTHON), "-c", SEED],
        cwd=ROOT,
        env={**os.environ, "REHEARSE_TEST_DATABASE_URL": TEST_URL},
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(next(x for x in seeded.stdout.splitlines() if x.startswith("SEED"))[4:])
    work = tempfile.mkdtemp(prefix="rehearse-smoke-")
    env = {
        **os.environ,
        "DATABASE_URL": TEST_URL,
        "REHEARSE_AUTH_SECRET": "smoke-test-secret-at-least-thirty-two-characters",
        "REHEARSE_PUBLIC_BASE_URL": f"http://127.0.0.1:{PORT}",
        "REHEARSE_PORT": str(PORT),
        "REHEARSE_SMTP_HOST": "127.0.0.1",
        "REHEARSE_SMTP_FROM": "RehearseAI <no-reply@localhost>",
        "REHEARSE_JOB_RUNNER": "off",
        "DEEPGRAM_API_KEY": "unused",
        "OPENAI_API_KEY": "unused",
        "CARTESIA_API_KEY": "unused",
        "REHEARSE_SESSION_DIR": f"{work}/sessions",
        "REHEARSE_PDF_DIR": f"{work}/pdfs",
        "REHEARSE_SNAPSHOT_DIR": f"{work}/snapshots",
        "REHEARSE_DOCUMENT_DIR": f"{work}/documents",
    }
    server = subprocess.Popen(
        [str(PYTHON), "-m", "rehearse_ai"], cwd=work, env=env, stdout=subprocess.DEVNULL
    )
    base = f"http://127.0.0.1:{PORT}"
    problems: list[str] = []
    visited: list[str] = []
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("server did not start")
        with sync_playwright() as p:
            browser = p.chromium.launch()

            def page_in(context):
                page = context.new_page()
                page.on(
                    "console",
                    lambda m: (
                        problems.append(f"console {m.type}: {m.text} @ {page.url}")
                        if m.type in {"error", "warning"}
                        else None
                    ),
                )
                page.on("pageerror", lambda e: problems.append(f"pageerror: {e} @ {page.url}"))
                page.on(
                    "response",
                    lambda r: (
                        problems.append(f"{r.status} {r.url}")
                        if r.status >= 400 and "/api/auth/me" not in r.url
                        else None
                    ),
                )
                return page

            def visit(page, path, expect_text=None):
                response = page.goto(f"{base}{path}", wait_until="networkidle")
                visited.append(f"{response.status} {path} -> {page.url.removeprefix(base)}")
                if expect_text and expect_text not in page.inner_text("body"):
                    problems.append(f"{path}: missing text {expect_text!r}")

            # Administrator.
            admin = browser.new_context()
            page = page_in(admin)
            visit(page, "/login", "Sign in")
            page.fill("input[type=email]", data["email"])
            page.fill("input[type=password]", data["password"])
            page.click("button[type=submit]")
            page.wait_for_url("**/admin/interviews", timeout=10000)
            visit(page, "/admin/interviews")
            visit(page, "/admin/assignments")
            visit(page, "/admin/schedule", "Schedule")
            admin.close()

            # Candidate: invitation, welcome, interview room shell.
            candidate = browser.new_context()
            page = page_in(candidate)
            visit(page, f"/join/{data['token']}")
            page.click("button")
            page.wait_for_url("**/candidate/welcome", timeout=10000)
            visit(page, "/candidate/welcome")
            token = candidate.request.get(f"{base}/api/csrf").json()["token"]
            named = candidate.request.post(
                f"{base}/api/candidate/name",
                data=json.dumps({"full_name": "Nyla Example"}),
                headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
            )
            if named.status != 200:
                problems.append(f"name confirmation failed: {named.status}")
            visit(page, "/interview", "Begin")
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    print("\n".join(visited))
    if problems:
        print("\nPROBLEMS:\n" + "\n".join(sorted(set(problems))))
        return 1
    print("\nOK: no console errors, no failed requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
