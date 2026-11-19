"""The interview plan: the questions and execution guidance an attempt follows.

A plan is prepared automatically from the résumé and job description (see
``plans.preparation``), stored as an immutable version, and frozen onto each attempt when it
starts. Optional fields exist so plans frozen before they were added remain valid. Fields marked
private to administrators never reach the live interview model or any candidate-facing API.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Share of the target duration reserved for questions; the rest is opening and closing.
QUESTION_SHARE = 0.85


class PlanQuestion(BaseModel):
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


class InterviewPlanContent(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    purpose: str = Field(min_length=3, max_length=500)
    questions: list[PlanQuestion] = Field(min_length=1, max_length=12)
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
