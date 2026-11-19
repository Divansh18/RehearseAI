"""Restricted candidate access: invitation tokens, candidate sessions and what a candidate may see.

A candidate session is the HttpOnly cookie issued when an invitation is redeemed. It is scoped to
one assignment and is unrelated to administrator accounts and to the live voice session.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rehearse_ai.models import CandidateSession, InterviewAssignment, utcnow
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
