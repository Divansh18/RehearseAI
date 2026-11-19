"""Shared helpers for scheduling, preparation and execution tests. All data is invented."""

import asyncio
import copy
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehearse_ai.accounts.cli import create_admin
from rehearse_ai.config import AuthConfig, Settings
from rehearse_ai.database.models import (
    Account,
    InterviewAssignment,
    InterviewPlan,
    InterviewPlanVersion,
)
from rehearse_ai.invitations.access import invitation_token, token_digest
from rehearse_ai.plans.preparation import PlanProviderError
from rehearse_ai.server import create_app

TEST_URL = os.environ.get(
    "REHEARSE_TEST_DATABASE_URL",
    "postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test",
)
assert TEST_URL.rsplit("/", 1)[-1].endswith("_test")
SECRET = "test-secret-at-least-thirty-two-characters"
PASSWORD = "invented test password 1234"

SECRET_GUIDANCE = "PRIVATE-GUIDANCE-Q2-DO-NOT-LEAK"
SECRET_BASIS = "PRIVATE-BASIS-NOTE"
SECRET_EVIDENCE = "Built the PRIVATE-EVIDENCE-SPAN ingestion service"
FUTURE_QUESTION = "FUTURE-QUESTION-TEXT: how did you handle partial failures in the pipeline?"

RESUME_TEXT = (
    "Nyla Example. Senior backend engineer. Built the PRIVATE-EVIDENCE-SPAN ingestion service "
    "that processed two million documents each day using queues and idempotent workers. "
    "Led a migration from a monolith to services, reduced p99 latency by forty percent, and "
    "mentored four engineers. Experience with Postgres, Python, Kafka and observability tooling."
)
JOB_TEXT = (
    "We are hiring a backend engineer to own our document processing platform. You will design "
    "reliable services, work with queues and databases, improve observability and mentor others. "
    "Strong Python, PostgreSQL and distributed systems experience is required for this role."
)


def plan_dict(**overrides) -> dict:
    """A valid generated-style plan including private fields that must never reach candidates."""
    plan = {
        "title": "Backend engineer interview",
        "purpose": "Explore backend design and ownership",
        "questions": [
            {
                "topic": "Ingestion service",
                "question": "Tell me about the ingestion service you built and your role in it.",
                "required": True,
                "follow_up_guidance": "Listen for ownership and scale",
                "max_probes": 1,
                "priority": "must",
                "budget_seconds": 300,
                "source": "resume",
                "evidence_quote": SECRET_EVIDENCE,
                "basis": SECRET_BASIS,
            },
            {
                "topic": "Failure handling",
                "question": FUTURE_QUESTION,
                "required": True,
                "follow_up_guidance": SECRET_GUIDANCE,
                "max_probes": 1,
                "priority": "must",
                "budget_seconds": 300,
                "source": "job",
            },
            {
                "topic": "Mentoring",
                "question": "How do you usually help less experienced engineers grow?",
                "required": False,
                "follow_up_guidance": "Concrete example",
                "max_probes": 0,
                "priority": "could",
                "budget_seconds": 240,
                "source": "general",
            },
        ],
        "focus_areas": ["Queues"],
        "target_minutes": 20,
        "interviewer_style": "Calm and curious",
        "closing_instructions": "CLOSING-INSTRUCTION-TEXT thank the candidate briefly",
    }
    plan.update(overrides)
    return plan


def generated_plan(resume_text: str = RESUME_TEXT, minutes: int = 30) -> dict:
    """Model-style output (before Python normalisation) for the scheduling tests."""
    quote = "Built the PRIVATE-EVIDENCE-SPAN ingestion service"
    assert quote in resume_text
    topics = [
        ("Ingestion service", "Tell me about the ingestion service you built.", "resume", quote),
        (
            "Reliability",
            "How do you design services so partial failures stay recoverable?",
            "job",
            "",
        ),
        (
            "Migration",
            "Walk me through how you led the migration to services.",
            "resume",
            "Led a migration from a monolith to services",
        ),
        ("Observability", "What do you instrument first when a new service goes live?", "job", ""),
        ("Mentoring", "How do you help less experienced engineers grow?", "general", ""),
    ]
    return {
        "title": "Backend engineer interview",
        "purpose": "Explore backend design, ownership and mentoring.",
        "interviewer_style": "Calm, concise and curious.",
        "closing_instructions": "Thank the candidate warmly and briefly.",
        "focus_areas": ["Reliability", "Ownership"],
        "questions": [
            {
                "topic": topic,
                "question": question,
                "priority": "must" if index < 3 else "could" if index == 4 else "should",
                "source": source,
                "evidence_quote": evidence,
                "basis": f"Private note {index}",
                "follow_up_guidance": "Listen for concrete detail.",
                "max_probes": 1,
                "budget_seconds": 200,
            }
            for index, (topic, question, source, evidence) in enumerate(topics)
        ],
    }


class FakeGenerator:
    """Scripted model: each call pops the next item (a dict result or an exception)."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []

    async def generate(self, prompt_name: str, payload: dict) -> dict:
        self.calls.append((prompt_name, copy.deepcopy(payload)))
        if not self.results:
            raise PlanProviderError("exhausted")
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


def make_settings(tmp_path, **extra) -> Settings:
    return Settings(
        deepgram_api_key="test-dg",
        openai_api_key="test-oa",
        cartesia_api_key="test-ca",
        auth=AuthConfig(database_url=TEST_URL, secret=SECRET),
        session_dir=tmp_path / "sessions",
        pdf_dir=tmp_path / "pdfs",
        snapshot_dir=tmp_path / "snapshots",
        document_dir=tmp_path / "documents",
        **extra,
    )


def make_app(tmp_path, generator=None, **extra):
    """An app with a recording mailer and (optionally) a scripted plan generator."""
    app = create_app(make_settings(tmp_path, **extra))
    sent: list[tuple[str, str, dict]] = []

    async def capture(to, token, **details):
        sent.append((to, token, details))

    app.state.accounts.mailer.send_invitation = capture
    if generator is not None:
        app.state.plan_generator = generator
    app.state.sent_mail = sent
    return app


def new_admin_email() -> str:
    email = f"admin-{uuid.uuid4().hex}@example.com"
    asyncio.run(create_admin(email, PASSWORD, TEST_URL, SECRET))
    return email


def login(client, email: str) -> dict:
    token = client.get("/api/csrf").json()["token"]
    headers = {"X-CSRF-Token": token}
    response = client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=headers
    )
    assert response.status_code == 204, response.text
    return headers


async def seed_assignment(
    admin_email: str,
    *,
    content: dict | None = None,
    plan: bool = True,
    preparation_status: str = "ready",
    start: datetime | None = None,
    latest: datetime | None = None,
    target_minutes: int | None = None,
    candidate_email: str | None = None,
    mail_status: str = "sent",
):
    """Seed a (possibly plan-less) assignment directly, returning (id, version_id, token)."""
    engine = create_async_engine(TEST_URL)
    token = invitation_token()
    instant = datetime.now(UTC)
    start = start or instant - timedelta(minutes=2)
    latest = latest or instant + timedelta(minutes=30)
    try:
        async with async_sessionmaker(engine).begin() as db:
            admin_id = await db.scalar(select(Account.id).where(Account.email == admin_email))
            version_id = None
            if plan:
                blueprint = InterviewPlan(
                    created_by=admin_id,
                    draft=content or plan_dict(),
                    created_at=instant,
                    updated_at=instant,
                )
                db.add(blueprint)
                await db.flush()
                version = InterviewPlanVersion(
                    plan_id=blueprint.id,
                    version=1,
                    content=content or plan_dict(),
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
                candidate_instructions="",
                scheduled_start_at=start,
                latest_start_at=latest,
                timezone="Etc/UTC",
                status="assigned",
                invitation_digest=token_digest(token),
                invitation_expires_at=latest,
                mail_status=mail_status,
                preparation_status=preparation_status,
                target_minutes=target_minutes,
                created_at=instant,
                updated_at=instant,
            )
            db.add(assignment)
            await db.flush()
            return str(assignment.id), str(version_id) if version_id else None, token
    finally:
        await engine.dispose()


# --- invented documents -------------------------------------------------------------


def make_pdf(text: str, *, pages: int = 1, encrypt: bool = False, draw_text: bool = True) -> bytes:
    """A real PDF. ``draw_text=False`` yields an image-only page like a scan."""
    import io
    import textwrap

    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    for _ in range(pages):
        if draw_text:
            lines = textwrap.wrap(text, 85)
            obj = pdf.beginText(50, 740)
            for line in lines:
                obj.textLine(line)
            pdf.drawText(obj)
        else:
            pdf.rect(50, 50, 300, 300, fill=1)
        pdf.showPage()
    pdf.save()
    data = buffer.getvalue()
    if not encrypt:
        return data
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt("owner-password")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_docx(
    text: str, *, extra_files: dict[str, bytes] | None = None, xml: str | None = None
) -> bytes:
    import io
    import zipfile
    from xml.sax.saxutils import escape

    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in text.split("\n"))
    document = xml or (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{namespace}">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
        for name, data in (extra_files or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()
