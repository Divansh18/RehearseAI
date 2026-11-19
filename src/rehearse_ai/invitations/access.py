"""Restricted candidate access: invitation tokens, candidate sessions and what a candidate may see.

A candidate session is the HttpOnly cookie issued when an invitation is redeemed. It is scoped to
one assignment and is unrelated to administrator accounts and to the live voice session.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.models import (
    CandidateSession,
    InterviewAssignment,
    InterviewPlanVersion,
    utcnow,
)
from rehearse_ai.plans.schema import InterviewPlanContent

SESSION_COOKIE = "rehearse_invitee"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invitation_token() -> str:
    return secrets.token_urlsafe(32)


def plan_state(assignment: InterviewAssignment) -> str:
    """What a candidate may know about preparation: ready, preparing or unavailable."""
    if assignment.plan_version_id is not None and assignment.preparation_status == "ready":
        return "ready"
    return "unavailable" if assignment.preparation_status == "failed" else "preparing"


def assignment_public(assignment: InterviewAssignment, plan: InterviewPlanContent | None) -> dict:
    """Candidate-facing fields only: never plan content, résumé or private instructions."""
    state = plan_state(assignment)
    ready = state == "ready" and plan is not None
    return {
        "title": plan.title if (ready and plan) else "Candidate interview",
        "candidate_name": assignment.candidate_name,
        "self_reported_name": assignment.self_reported_name,
        "candidate_instructions": assignment.candidate_instructions,
        "scheduled_start_at": assignment.scheduled_start_at.isoformat(),
        "latest_start_at": assignment.latest_start_at.isoformat(),
        "timezone": assignment.timezone,
        "target_minutes": assignment.target_minutes or (plan.target_minutes if plan else None),
        "question_count": len(plan.questions) if (ready and plan) else None,
        # A ready-looking assignment whose stored plan no longer validates is unavailable.
        "plan_state": "ready"
        if ready
        else ("preparing" if state == "preparing" else "unavailable"),
        "status": (
            "expired"
            if assignment.status == "assigned" and assignment.latest_start_at < utcnow()
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
        or assignment.invitation_expires_at <= utcnow()
    ):
        raise HTTPException(404, "Invitation unavailable")
    return assignment


async def candidate_session(db: AsyncSession, request: Request) -> InterviewAssignment:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 128:
        raise HTTPException(401, "Open your private invitation to continue")
    session = await db.get(CandidateSession, token_digest(token))
    if session is None or session.revoked_at or session.expires_at <= utcnow():
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
        max_age=max(1, int((expires - utcnow()).total_seconds())),
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def session_expiry(assignment: InterviewAssignment) -> datetime:
    return min(
        assignment.invitation_expires_at + timedelta(hours=4), utcnow() + timedelta(hours=12)
    )


async def public_plan(
    db: AsyncSession, assignment: InterviewAssignment
) -> InterviewPlanContent | None:
    """The validated plan, or None while it is pending, failed or no longer valid."""
    if assignment.plan_version_id is None:
        return None
    version = await db.get(InterviewPlanVersion, assignment.plan_version_id)
    try:
        return InterviewPlanContent.model_validate(version.content) if version else None
    except ValidationError:
        return None


def candidate_interview_view(
    assignment: InterviewAssignment, plan: InterviewPlanContent | None
) -> dict:
    """What a signed-in candidate sees: the public fields plus whether Begin is allowed."""
    data = assignment_public(assignment, plan)
    instant = utcnow()
    # Begin stays unavailable until the server knows a valid plan is ready.
    data["can_begin"] = (
        assignment.status == "assigned"
        and data["plan_state"] == "ready"
        and assignment.scheduled_start_at <= instant <= assignment.latest_start_at
    )
    data["window_state"] = (
        "too_early"
        if instant < assignment.scheduled_start_at
        else "closed"
        if instant > assignment.latest_start_at
        else "open"
    )
    return data


@dataclass(frozen=True)
class RedeemedInvitation:
    """A newly issued candidate session: the cookie value and when it stops working."""

    token: str
    expires: datetime


async def redeem_invitation(
    db_sessions: async_sessionmaker[AsyncSession], token: str
) -> RedeemedInvitation:
    """Exchange a private invitation link for a candidate session, revoking any earlier one.

    Runs in one transaction under a row lock so two concurrent redemptions cannot both succeed
    and leave two live sessions for the same assignment.
    """
    async with db_sessions.begin() as transaction:
        assignment = await transaction.scalar(
            select(InterviewAssignment)
            .where(InterviewAssignment.invitation_digest == token_digest(token))
            .with_for_update()
        )
        if (
            assignment is None
            or len(token) < 32
            or len(token) > 128
            or assignment.status != "assigned"
            or assignment.invitation_expires_at <= utcnow()
        ):
            raise HTTPException(404, "Invitation unavailable")
        existing = await transaction.scalars(
            select(CandidateSession).where(
                CandidateSession.assignment_id == assignment.id,
                CandidateSession.revoked_at.is_(None),
            )
        )
        for prior in existing:
            prior.revoked_at = utcnow()
        session_token = secrets.token_urlsafe(32)
        expires = session_expiry(assignment)
        transaction.add(
            CandidateSession(
                digest=token_digest(session_token),
                assignment_id=assignment.id,
                expires_at=expires,
            )
        )
    return RedeemedInvitation(session_token, expires)


async def confirm_self_reported_name(
    db_sessions: async_sessionmaker[AsyncSession], assignment_id: uuid.UUID, full_name: str
) -> str:
    """Record the name the candidate says they go by. This does not verify identity."""
    name = " ".join(full_name.split())
    if len(name.split()) < 2 or any(char in name for char in "<>\r\n"):
        raise HTTPException(400, "Enter your full name")
    async with db_sessions.begin() as transaction:
        current = await transaction.get(InterviewAssignment, assignment_id, with_for_update=True)
        if current.status != "assigned":
            raise HTTPException(409, "Name cannot change after the interview starts")
        current.self_reported_name = name
        current.updated_at = utcnow()
    return name


async def revoke_candidate_session(
    db_sessions: async_sessionmaker[AsyncSession], cookie_token: str | None
) -> None:
    if not cookie_token:
        return
    async with db_sessions.begin() as transaction:
        row = await transaction.get(CandidateSession, token_digest(cookie_token))
        if row:
            row.revoked_at = utcnow()
