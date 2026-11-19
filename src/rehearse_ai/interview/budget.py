"""Soft interview time budget driven by active, unpaused monotonic time.

The target duration is advisory. It is never a hard timer: speech and candidate turns
are not cut off. The budget only changes what Python permits *at turn boundaries*:
optional probing stops, optional questions are dropped, and when the budget is
exhausted the interview moves to its closing after the current turn completes.
Real interviews can therefore overrun by at most one candidate turn plus the closing.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rehearse_ai.plans.schema import QUESTION_SHARE

# Fraction of the target after which optional probing stops.
TIGHT_FRACTION = 0.8


class ActiveClock:
    """Monotonic clock that stops counting during explicit media pauses."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0

    def start(self) -> None:
        if self._started is None:
            self._started = self._clock()

    def pause(self) -> None:
        if self._started is not None and self._paused_at is None:
            self._paused_at = self._clock()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None

    @property
    def paused_seconds(self) -> float:
        extra = self._clock() - self._paused_at if self._paused_at is not None else 0.0
        return self._paused_total + extra

    def active_seconds(self) -> float:
        if self._started is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else self._clock()
        return max(0.0, end - self._started - self._paused_total)


@dataclass(frozen=True)
class RemainingQuestion:
    index: int  # zero-based position in the plan
    required: bool
    seconds: int


def question_seconds(questions: Sequence[dict], target_seconds: int) -> list[int]:
    """Per-question budgets: the planned value where present, else an even share."""
    even = max(30, int(target_seconds * QUESTION_SHARE / max(1, len(questions))))
    return [
        int(item["budget_seconds"])
        if isinstance(item.get("budget_seconds"), int) and item["budget_seconds"] > 0
        else even
        for item in questions
    ]


def pressure(active: float, target_seconds: int, remaining: Sequence[RemainingQuestion]) -> str:
    """``normal``, ``tight`` (no optional probing) or ``exhausted`` (close after this turn)."""
    if active >= target_seconds:
        return "exhausted"
    required_need = sum(item.seconds for item in remaining if item.required)
    if active >= TIGHT_FRACTION * target_seconds or target_seconds - active < required_need:
        return "tight"
    return "normal"


def skip_for_time(
    active: float, target_seconds: int, remaining: Sequence[RemainingQuestion]
) -> list[int]:
    """Indices to drop because the budget cannot afford them.

    Required questions are kept until the budget is exhausted. Optional questions are
    kept only if asking them still leaves room for every later required question.
    When the budget is exhausted everything remaining is dropped; the caller records
    the unreached required questions honestly.
    """
    if active >= target_seconds:
        return [item.index for item in remaining]
    time_left = target_seconds - active
    later_required = [0] * (len(remaining) + 1)
    for position in range(len(remaining) - 1, -1, -1):
        item = remaining[position]
        later_required[position] = later_required[position + 1] + (
            item.seconds if item.required else 0
        )
    skipped: list[int] = []
    for position, item in enumerate(remaining):
        if item.required:
            time_left -= item.seconds
        elif time_left - item.seconds >= later_required[position + 1]:
            time_left -= item.seconds
        else:
            skipped.append(item.index)
    return skipped
