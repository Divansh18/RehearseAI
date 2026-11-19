"""Validated interview plans and restricted invitation access."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rehearse_ai.models import CandidateInterviewSession, InterviewAssignment

SESSION_COOKIE = "rehearse_invitee"


def now() -> datetime:
    return datetime.now(UTC)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CoreQuestion(BaseModel):
    topic: str = Field(min_length=2, max_length=100)
    question: str = Field(min_length=8, max_length=400)
    required: bool = True
    follow_up_guidance: str = Field(default="", max_length=500)
    max_probes: int = Field(default=1, ge=0, le=2)
    # Fields below are optional so historical frozen plans remain valid. They are private to
    # administrators: the live interview model and candidate APIs never receive them.
    priority: Literal["must", "should", "could"] = "should"
    budget_seconds: int | None = Field(default=None, ge=30, le=900)
    source: Literal["resume", "job", "both", "general"] = "general"
    evidence_quote: str = Field(default="", max_length=300)
    basis: str = Field(default="", max_length=240)


class BlueprintContent(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    purpose: str = Field(min_length=3, max_length=500)
    questions: list[CoreQuestion] = Field(min_length=1, max_length=12)
    focus_areas: list[str] = Field(default_factory=list, max_length=8)
    target_minutes: int = Field(ge=5, le=120)
    interviewer_style: str = Field(min_length=3, max_length=500)
    closing_instructions: str = Field(min_length=3, max_length=500)

    @field_validator("focus_areas")
    @classmethod
    def valid_focus(cls, areas: list[str]) -> list[str]:
        if any(not area.strip() or len(area) > 120 for area in areas):
            raise ValueError("Each focus area needs 1–120 characters")
        return areas


class AssignmentInput(BaseModel):
    blueprint_version_id: uuid.UUID
    candidate_email: EmailStr
    candidate_name: str | None = Field(default=None, max_length=120)
    focus_areas: list[str] = Field(default_factory=list, max_length=8)
    candidate_instructions: str = Field(default="", max_length=1000)
    scheduled_start_at: datetime
    latest_start_at: datetime
    timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_window(self) -> "AssignmentInput":
        if self.scheduled_start_at.tzinfo is None or self.latest_start_at.tzinfo is None:
            raise ValueError("Start times need explicit UTC offsets")
        if self.latest_start_at <= self.scheduled_start_at:
            raise ValueError("Latest permitted start must be after scheduled start")
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use an IANA timezone such as Asia/Kolkata") from exc
        if self.candidate_name is not None and not self.candidate_name.strip():
            raise ValueError("Candidate name cannot be blank")
        if (
            self.scheduled_start_at.astimezone(zone).utcoffset()
            != self.scheduled_start_at.utcoffset()
            or self.latest_start_at.astimezone(zone).utcoffset() != self.latest_start_at.utcoffset()
        ):
            raise ValueError("The timestamp offsets must match the named timezone")
        if any(not item.strip() or len(item) > 120 for item in self.focus_areas):
            raise ValueError("Each focus area needs 1–120 characters")
        return self


def invitation_token() -> str:
    return secrets.token_urlsafe(32)


def plan_state(assignment: InterviewAssignment) -> str:
    """What a candidate may know about preparation: ready, preparing or unavailable."""
    if assignment.blueprint_version_id is not None and assignment.preparation_status == "ready":
        return "ready"
    return "unavailable" if assignment.preparation_status == "failed" else "preparing"


def assignment_public(assignment: InterviewAssignment, blueprint: BlueprintContent | None) -> dict:
    """Candidate-facing fields only: never plan content, résumé or private instructions."""
    state = plan_state(assignment)
    ready = state == "ready" and blueprint is not None
    return {
        "title": blueprint.title if (ready and blueprint) else "Candidate interview",
        "candidate_name": assignment.candidate_name,
        "self_reported_name": assignment.self_reported_name,
        "candidate_instructions": assignment.candidate_instructions,
        "scheduled_start_at": assignment.scheduled_start_at.isoformat(),
        "latest_start_at": assignment.latest_start_at.isoformat(),
        "timezone": assignment.timezone,
        "target_minutes": assignment.target_minutes
        or (blueprint.target_minutes if blueprint else None),
        "question_count": len(blueprint.questions) if (ready and blueprint) else None,
        # A ready-looking assignment whose stored plan no longer validates is unavailable.
        "plan_state": "ready"
        if ready
        else ("preparing" if state == "preparing" else "unavailable"),
        "status": (
            "expired"
            if assignment.status == "assigned" and assignment.latest_start_at < now()
            else assignment.status
        ),
    }


async def invitation_assignment(db: AsyncSession, token: str) -> InterviewAssignment:
    if len(token) < 32 or len(token) > 128:
        raise HTTPException(404, "Invitation unavailable")
    assignment = await db.scalar(
        select(InterviewAssignment).where(
            InterviewAssignment.invitation_digest == token_digest(token)
        )
    )
    if (
        assignment is None
        or assignment.status != "assigned"
        or assignment.invitation_expires_at <= now()
    ):
        raise HTTPException(404, "Invitation unavailable")
    return assignment


async def candidate_session(db: AsyncSession, request: Request) -> InterviewAssignment:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 128:
        raise HTTPException(401, "Open your private invitation to continue")
    session = await db.get(CandidateInterviewSession, token_digest(token))
    if session is None or session.revoked_at or session.expires_at <= now():
        raise HTTPException(401, "Your invitation session expired. Open the invitation again")
    assignment = await db.get(InterviewAssignment, session.assignment_id)
    if assignment is None or assignment.status not in {
        "assigned",
        "starting",
        "in_progress",
        "completed",
        "ended_early",
        "interrupted",
        "failed",
    }:
        raise HTTPException(403, "Interview unavailable")
    return assignment


def set_candidate_cookie(
    response: Response, token: str, *, secure: bool, expires: datetime
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(1, int((expires - now()).total_seconds())),
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def session_expiry(assignment: InterviewAssignment) -> datetime:
    return min(assignment.invitation_expires_at + timedelta(hours=4), now() + timedelta(hours=12))
