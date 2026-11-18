"""Automatic interview-plan preparation and optional natural-language revision.

The model proposes; Python validates. A generated plan is accepted only if it satisfies
``BlueprintContent`` and the checks below (question count for the duration, no duplicated
substance, résumé-grounded questions backed by a verbatim span of the résumé, normalised
time budgets). Résumé, job-description, instruction and revision text are untrusted data.
No transaction is held open across a model call, and a failed or stale revision never
changes the plan already in use.
"""

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rehearse_ai.accounts_db import (
    AssignmentAudit,
    BlueprintVersion,
    InterviewAssignment,
    InterviewBlueprint,
    InterviewPreparation,
)
from rehearse_ai.assigned import BlueprintContent, now
from rehearse_ai.budget import QUESTION_SHARE
from rehearse_ai.jobs import (
    ClaimedJob,
    JobCancelled,
    JobError,
    StaleJobError,
    verify_claim,
)
from rehearse_ai.prompts import load_prompt

MAX_REVISION_REQUEST = 1000
MIN_EVIDENCE_CHARS = 12
DUPLICATE_SIMILARITY = 0.7
GENERATIONS_PER_ATTEMPT = 2

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "question": {"type": "string"},
        "priority": {"type": "string", "enum": ["must", "should", "could"]},
        "source": {"type": "string", "enum": ["resume", "job", "both", "general"]},
        "evidence_quote": {"type": "string"},
        "basis": {"type": "string"},
        "follow_up_guidance": {"type": "string"},
        "max_probes": {"type": "integer"},
        "budget_seconds": {"type": "integer"},
    },
    "required": [
        "topic",
        "question",
        "priority",
        "source",
        "evidence_quote",
        "basis",
        "follow_up_guidance",
        "max_probes",
        "budget_seconds",
    ],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "purpose": {"type": "string"},
        "interviewer_style": {"type": "string"},
        "closing_instructions": {"type": "string"},
        "focus_areas": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": QUESTION_SCHEMA},
    },
    "required": [
        "title",
        "purpose",
        "interviewer_style",
        "closing_instructions",
        "focus_areas",
        "questions",
    ],
    "additionalProperties": False,
}


class PlanProviderError(Exception):
    """The model service failed or returned something unusable (no content in the message)."""


class PlanQualityError(Exception):
    """A generated plan broke a rule. The message is safe to send back to the model."""


class PlanGenerator(Protocol):
    async def generate(self, prompt_name: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class OpenAIPlanGenerator:
    """Structured-output call to the configured OpenAI text model."""

    def __init__(self, api_key: str, model: str, *, timeout: float = 90.0) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=1)
        self.model = model

    async def generate(self, prompt_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                temperature=0.4,
                max_completion_tokens=5000,
                messages=[
                    {"role": "system", "content": load_prompt(prompt_name)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "interview_plan",
                        "strict": True,
                        "schema": PLAN_SCHEMA,
                    },
                },
            )
            choice = response.choices[0]
            if choice.finish_reason != "stop" or not choice.message.content:
                raise PlanProviderError("incomplete_response")
            data = json.loads(choice.message.content)
        except PlanProviderError:
            raise
        except Exception as exc:  # network, auth, quota, JSON: never include provider text
            raise PlanProviderError(type(exc).__name__) from None
        if not isinstance(data, dict):
            raise PlanProviderError("not_an_object")
        return data


# --- validation ---------------------------------------------------------------------


def question_range(target_minutes: int) -> tuple[int, int]:
    low = max(3, round(target_minutes / 8))
    return low, min(12, max(low, round(target_minutes / 4.5)))


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


_TYPOGRAPHY = str.maketrans(
    {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00ad": "", "\u00a0": " "}
)


def _squash(text: str) -> str:
    """Compare content, not layout: a PDF may wrap 'event-sourced' as 'event-' / 'sourced'."""
    return re.sub(r"\s+", "", text.translate(_TYPOGRAPHY).casefold())


def _tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.casefold()) if len(word) > 2}


def validate_generated_plan(
    raw: dict[str, Any], *, resume_text: str, target_minutes: int
) -> BlueprintContent:
    """Turn model output into a trusted plan or explain precisely what was wrong."""
    items = raw.get("questions")
    if not isinstance(items, list):
        raise PlanQualityError("questions must be a list")
    low, high = question_range(target_minutes)
    if not low <= len(items) <= high:
        raise PlanQualityError(
            f"Provide between {low} and {high} questions for {target_minutes} minutes; "
            f"{len(items)} were returned."
        )
    resume = _squash(resume_text)
    cleaned: list[dict[str, Any]] = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise PlanQualityError(f"question {number} is malformed")
        item = dict(item)
        source = item.get("source")
        quote = str(item.get("evidence_quote", "")).strip()
        if source in {"resume", "both"}:
            if len(_squash(quote)) < MIN_EVIDENCE_CHARS or _squash(quote) not in resume:
                raise PlanQualityError(
                    f"question {number} is marked as résumé-based but its evidence_quote is not "
                    "an exact span of the résumé. Copy a contiguous span verbatim, or change the "
                    "source to job or general and leave evidence_quote empty."
                )
        else:
            item["evidence_quote"] = ""
        priority = item.get("priority")
        item["required"] = priority == "must"
        item["max_probes"] = min(2, max(0, int(item.get("max_probes", 1) or 0)))
        cleaned.append(item)
    if not any(item["required"] for item in cleaned):
        raise PlanQualityError("At least one question must have priority must.")
    topics = [_normal(item.get("topic", "")) for item in cleaned]
    if len(set(topics)) != len(topics):
        raise PlanQualityError("Two questions share the same topic; make each distinct.")
    bags = [_tokens(item.get("question", "")) for item in cleaned]
    for first in range(len(bags)):
        for second in range(first + 1, len(bags)):
            union = bags[first] | bags[second]
            if union and len(bags[first] & bags[second]) / len(union) >= DUPLICATE_SIMILARITY:
                raise PlanQualityError(
                    f"Questions {first + 1} and {second + 1} ask essentially the same thing."
                )
    # Normalise time budgets to the share of the target reserved for questions.
    target_seconds = target_minutes * 60 * QUESTION_SHARE
    weights = [
        float(item["budget_seconds"])
        if isinstance(item.get("budget_seconds"), int) and item["budget_seconds"] > 0
        else target_seconds / len(cleaned)
        for item in cleaned
    ]
    scale = target_seconds / sum(weights)
    for item, weight in zip(cleaned, weights, strict=True):
        item["budget_seconds"] = int(min(900, max(30, round(weight * scale))))
    try:
        return BlueprintContent.model_validate(
            {
                "title": raw.get("title"),
                "purpose": raw.get("purpose"),
                "questions": cleaned,
                "focus_areas": [
                    str(area).strip()
                    for area in (raw.get("focus_areas") or [])
                    if str(area).strip()
                ][:8],
                "target_minutes": target_minutes,
                "interviewer_style": raw.get("interviewer_style"),
                "closing_instructions": raw.get("closing_instructions"),
            }
        )
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}))
        raise PlanQualityError(
            f"These fields broke the length or format limits: {fields}"
        ) from None


@dataclass(frozen=True)
class PlanInputs:
    resume_text: str
    job_description: str
    instructions: str
    target_minutes: int


async def generate_plan(
    generator: PlanGenerator,
    prompt_name: str,
    inputs: PlanInputs,
    *,
    current_plan: dict[str, Any] | None = None,
    revision_request: str | None = None,
) -> BlueprintContent:
    low, high = question_range(inputs.target_minutes)
    payload: dict[str, Any] = {
        "target_minutes": inputs.target_minutes,
        "question_count_range": [low, high],
        "job_description": inputs.job_description,
        "resume": inputs.resume_text,
        "private_interviewer_instructions": inputs.instructions,
    }
    if current_plan is not None:
        payload["current_plan"] = current_plan
        payload["current_question_count"] = len(current_plan.get("questions", []))
        payload["revision_request"] = revision_request or ""
    for _ in range(GENERATIONS_PER_ATTEMPT):
        try:
            raw = await generator.generate(prompt_name, payload)
        except PlanProviderError as exc:
            raise JobError("model_error") from exc
        try:
            return validate_generated_plan(
                raw, resume_text=inputs.resume_text, target_minutes=inputs.target_minutes
            )
        except PlanQualityError as exc:
            payload["previous_validation_error"] = str(exc)
    raise JobError("plan_invalid")


# --- job handlers -------------------------------------------------------------------


async def _next_version(db: AsyncSession, blueprint_id: uuid.UUID) -> int:
    latest = await db.scalar(
        select(func.max(BlueprintVersion.version)).where(
            BlueprintVersion.blueprint_id == blueprint_id
        )
    )
    return (latest or 0) + 1


class PrepareHandler:
    """Create version 1 of the plan from the stored résumé, job description and instructions."""

    timeout_secs = 360.0

    def __init__(
        self,
        db_sessions: async_sessionmaker[AsyncSession],
        generator: Callable[[], PlanGenerator],
    ) -> None:
        self.db_sessions = db_sessions
        self.generator = generator

    async def run(self, job: ClaimedJob) -> None:
        async with self.db_sessions.begin() as db:
            await verify_claim(db, job)
            assignment = await db.get(InterviewAssignment, job.assignment_id, with_for_update=True)
            if assignment is None or assignment.status != "assigned":
                raise JobCancelled("assignment_unavailable")
            if assignment.blueprint_version_id is not None:
                raise JobCancelled("already_prepared")
            prep = await db.scalar(
                select(InterviewPreparation).where(
                    InterviewPreparation.assignment_id == job.assignment_id
                )
            )
            if prep is None:
                raise JobError("missing_inputs", retryable=False)
            inputs = PlanInputs(
                prep.resume_text,
                prep.job_description_text,
                prep.interviewer_instructions,
                prep.target_minutes,
            )
            assignment.preparation_status = "preparing"
            assignment.preparation_error = None
            assignment.updated_at = now()
        # No transaction is open during the model call.
        plan = await generate_plan(self.generator(), "plan_prepare", inputs)
        async with self.db_sessions.begin() as db:
            await verify_claim(db, job)
            assignment = await db.get(InterviewAssignment, job.assignment_id, with_for_update=True)
            if assignment is None or assignment.status != "assigned":
                raise JobCancelled("assignment_started_or_cancelled")
            if assignment.blueprint_version_id is not None:
                raise JobCancelled("already_prepared")
            prep = await db.scalar(
                select(InterviewPreparation).where(
                    InterviewPreparation.assignment_id == job.assignment_id
                )
            )
            instant = now()
            content = plan.model_dump(mode="json")
            blueprint = InterviewBlueprint(
                id=uuid.uuid4(),
                created_by=job.created_by,
                draft=content,
                created_at=instant,
                updated_at=instant,
            )
            db.add(blueprint)
            await db.flush()
            version = BlueprintVersion(
                id=uuid.uuid4(),
                blueprint_id=blueprint.id,
                version=1,
                content=content,
                published_at=instant,
            )
            db.add(version)
            await db.flush()
            prep.blueprint_id = blueprint.id
            assignment.blueprint_version_id = version.id
            assignment.preparation_status = "ready"
            assignment.preparation_error = None
            assignment.updated_at = instant
            db.add(
                AssignmentAudit(
                    assignment_id=assignment.id,
                    actor_id=job.created_by,
                    action="plan_ready",
                    occurred_at=instant,
                    detail={"version": 1},
                )
            )

    async def exhausted(self, job: ClaimedJob, code: str) -> None:
        async with self.db_sessions.begin() as db:
            assignment = await db.get(InterviewAssignment, job.assignment_id, with_for_update=True)
            if (
                assignment is not None
                and assignment.status == "assigned"
                and assignment.blueprint_version_id is None
            ):
                assignment.preparation_status = "failed"
                assignment.preparation_error = code
                assignment.updated_at = now()


class ReviseHandler:
    """Apply an admin's natural-language change as a new version, never editing in place."""

    timeout_secs = 360.0

    def __init__(
        self,
        db_sessions: async_sessionmaker[AsyncSession],
        generator: Callable[[], PlanGenerator],
    ) -> None:
        self.db_sessions = db_sessions
        self.generator = generator

    async def run(self, job: ClaimedJob) -> None:
        request = str(job.payload.get("request", ""))[:MAX_REVISION_REQUEST]
        base_id = job.payload.get("base_version_id")
        async with self.db_sessions.begin() as db:
            await verify_claim(db, job)
            assignment = await db.get(InterviewAssignment, job.assignment_id)
            if (
                assignment is None
                or assignment.status != "assigned"
                or str(assignment.blueprint_version_id) != base_id
            ):
                raise JobCancelled("plan_changed")
            version = await db.get(BlueprintVersion, assignment.blueprint_version_id)
            prep = await db.scalar(
                select(InterviewPreparation).where(
                    InterviewPreparation.assignment_id == job.assignment_id
                )
            )
            if prep is None or version is None:
                raise JobError("missing_inputs", retryable=False)
            inputs = PlanInputs(
                prep.resume_text,
                prep.job_description_text,
                prep.interviewer_instructions,
                prep.target_minutes,
            )
            current = dict(version.content)
            blueprint_id = version.blueprint_id
        plan = await generate_plan(
            self.generator(),
            "plan_revise",
            inputs,
            current_plan=current,
            revision_request=request,
        )
        async with self.db_sessions.begin() as db:
            await verify_claim(db, job)
            assignment = await db.get(InterviewAssignment, job.assignment_id, with_for_update=True)
            # Reject the result if the interview started, was cancelled or the plan moved on.
            if (
                assignment is None
                or assignment.status != "assigned"
                or str(assignment.blueprint_version_id) != base_id
            ):
                raise JobCancelled("stale_revision")
            instant = now()
            number = await _next_version(db, blueprint_id)
            version = BlueprintVersion(
                id=uuid.uuid4(),
                blueprint_id=blueprint_id,
                version=number,
                content=plan.model_dump(mode="json"),
                published_at=instant,
            )
            db.add(version)
            await db.flush()
            assignment.blueprint_version_id = version.id
            assignment.updated_at = instant
            db.add(
                AssignmentAudit(
                    assignment_id=assignment.id,
                    actor_id=job.created_by,
                    action="plan_revised",
                    occurred_at=instant,
                    detail={"version": number},
                )
            )

    async def exhausted(self, job: ClaimedJob, code: str) -> None:
        # The previous ready plan stays in force; the failed job row records why.
        return None


__all__ = [
    "JobCancelled",
    "OpenAIPlanGenerator",
    "PlanGenerator",
    "PlanInputs",
    "PlanProviderError",
    "PlanQualityError",
    "PrepareHandler",
    "ReviseHandler",
    "StaleJobError",
    "generate_plan",
    "question_range",
    "validate_generated_plan",
]
