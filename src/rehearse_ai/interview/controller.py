"""Provider-independent interview policy and speech delivery state."""

from dataclasses import dataclass, replace
from enum import Enum, auto

from rehearse_ai.interview.memory import SessionMemory

GREETING = "Hey, I'm RehearseAI, your AI interview coach. How's your day going?"
NAME_PROMPT = "What should I call you?"
READINESS_PROMPT = "Ready to jump in?"
READINESS_WAIT = "Just let me know when you're ready."
NO_PROJECT_SCOPE = (
    "This practice needs a technical example to build on. "
    "We can end here, or you can return when one comes to mind."
)

MAX_FOLLOW_UPS_PER_QUESTION = 1
MAX_CLARIFICATIONS_PER_QUESTION = 1
# An assigned prompt may be repeated this many times; after that the candidate is told so plainly
# instead of being silently moved on.
MAX_REPEATS_PER_PROMPT = 2
# Consecutive control replies (feedback, restart, process questions) before the interviewer simply
# returns to the pending prompt rather than answering another detour.
MAX_CONTROL_DETOURS = 3
MAX_OPENING_REPEATS = 1


class Stage(Enum):
    NEW = auto()
    DELIVERING = auto()
    AWAITING_GREETING = auto()
    AWAITING_OPENING_ACK = auto()
    AWAITING_NAME = auto()
    AWAITING_OPENING_RETURN = auto()
    AWAITING_DETOUR_RETURN = auto()
    AWAITING_CONTINUE_CHOICE = auto()
    AWAITING_QUESTION_START = auto()
    AWAITING_READINESS = auto()
    AWAITING_ANSWER = auto()
    AWAITING_PROPOSAL = auto()
    AWAITING_NO_PROJECT_EXAMPLE = auto()
    AWAITING_NO_PROJECT_CHOICE = auto()
    AWAITING_FEEDBACK = auto()
    AWAITING_CORRECTION_RETURN = auto()
    EARLY_CLOSING = auto()
    COMPLETE = auto()
    ENDED_EARLY = auto()


class ReplyKind(Enum):
    FOLLOW_UP = auto()
    NUDGE = auto()
    CLARIFICATION = auto()
    ADVANCE = auto()


class Readiness(Enum):
    READY = auto()
    DEFERRED = auto()
    UNCLEAR = auto()


class UtteranceKind(Enum):
    GREETING = auto()
    NAME_PROMPT = auto()
    NAME_ACK = auto()
    OPENING_COACH_REPLY = auto()
    DETOUR_ACK = auto()
    DETOUR_RESUME = auto()
    PROVISIONAL_FEEDBACK = auto()
    TOPIC_BOUNDARY = auto()
    OPENING_ACK = auto()
    READINESS = auto()
    OPENING_WAIT = auto()
    OPENING_CLARIFICATION = auto()
    QUESTION = auto()
    FOLLOW_UP = auto()
    NUDGE = auto()
    CLARIFICATION = auto()
    REPEAT = auto()
    NO_PROJECT_INVITE = auto()
    NO_PROJECT_SCOPE = auto()
    FEEDBACK = auto()
    CLOSING = auto()
    EARLY_FEEDBACK = auto()
    CORRECTION_ACK = auto()
    INFORMATION = auto()


@dataclass(frozen=True)
class Utterance:
    id: int
    kind: UtteranceKind
    text: str
    question_number: int | None


PROMPT_KINDS = frozenset(
    {
        UtteranceKind.QUESTION,
        UtteranceKind.FOLLOW_UP,
        UtteranceKind.NUDGE,
        UtteranceKind.CLARIFICATION,
    }
)


class InvalidTransition(ValueError):
    """An event is not valid in the current interview stage."""


class LimitReached(InvalidTransition):
    """The current question's follow-up or clarification budget is spent."""


class InterviewController:
    """One session's question order, reply budgets, and delivery accounting.

    IDs are scoped to this controller. A delivery acknowledgment means the
    transport reports playback completed, not that a human heard every word.
    """

    def __init__(
        self,
        questions: tuple[str, ...],
        *,
        probe_limits: tuple[int, ...] | None = None,
        required: tuple[bool, ...] | None = None,
    ) -> None:
        self._questions = questions
        if not self._questions or len(self._questions) > 12:
            raise ValueError("Interview needs 1–12 questions")
        self._probe_limits = probe_limits or (MAX_FOLLOW_UPS_PER_QUESTION,) * len(self._questions)
        self._required = required or (True,) * len(self._questions)
        if len(self._probe_limits) != len(self._questions) or len(self._required) != len(
            self._questions
        ):
            raise ValueError("Question metadata length mismatch")
        self._stage = Stage.NEW
        self._question_index = -1
        self._next_utterance_id = 1
        self._pending: Utterance | None = None
        self._interrupted: Utterance | None = None
        self._answers: list[list[str]] = [[] for _ in self._questions]
        self._covered_source_turns: list[int | None] = [None] * len(self._questions)
        self._opening_text: str | None = None
        self._pending_prompt: Utterance | None = None
        self._prompt_delivered = False
        self._repeats = [0] * len(self._questions)
        self._control_detours = 0
        self._gaps: set[int] = set()
        self._time_skipped: set[int] = set()
        self._delivered_questions: set[int] = set()
        self._closing_remarks: list[str] = []
        self._follow_ups = [0] * len(self._questions)
        self._clarifications = [0] * len(self._questions)
        self._greeting_response = ""
        self.memory = SessionMemory()
        self._name_declined = False
        self._name_detour = False
        self._detour_resume_prompt: Utterance | None = None
        self._return_after_detour = False
        self._opening_return_to_name = False
        self._opening_coach_replies = 0
        self._opening_repeats = {Stage.AWAITING_GREETING: 0, Stage.AWAITING_READINESS: 0}
        self._readiness_nudges = 0
        self._correction_return_stage: Stage | None = None
        self._correction_resume_utterance: Utterance | None = None

    @property
    def stage(self) -> Stage:
        return self._stage

    @property
    def current_question_number(self) -> int | None:
        return self._question_index + 1 if self._question_index >= 0 else None

    @property
    def current_question(self) -> str | None:
        return self._questions[self._question_index] if self._question_index >= 0 else None

    @property
    def remaining_clarifications(self) -> int:
        if self._question_index < 0:
            return MAX_CLARIFICATIONS_PER_QUESTION
        return MAX_CLARIFICATIONS_PER_QUESTION - self._clarifications[self._question_index]

    @property
    def pending(self) -> Utterance | None:
        return self._pending

    @property
    def interrupted_utterance(self) -> Utterance | None:
        return self._interrupted

    @property
    def answers(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(turns) for turns in self._answers)

    @property
    def covered_source_turns(self) -> tuple[int | None, ...]:
        return tuple(self._covered_source_turns)

    def credit_covered_question(
        self, question_number: int, evidence_quote: str, *, source_turn: int
    ) -> bool:
        """Credit later authored coverage only when the quote occurs in an actual current answer."""
        self._require(Stage.AWAITING_PROPOSAL)
        target = question_number - 1
        if (
            target <= self._question_index
            or target >= len(self._questions)
            or self._covered_source_turns[target] is not None
            or source_turn < 1
            or len(evidence_quote.strip()) < 8
        ):
            return False
        answer = (
            self._answers[self._question_index][-1] if self._answers[self._question_index] else ""
        ).strip()
        normalized_answer = " ".join(answer.casefold().split())
        normalized_quote = " ".join(evidence_quote.casefold().split())
        if not normalized_quote or normalized_quote not in normalized_answer:
            return False
        self._answers[target].append(answer)
        self._covered_source_turns[target] = source_turn
        return True

    def _next_open_index(self, start: int) -> int:
        """First question at or after ``start`` that is neither covered nor skipped for time."""
        index = start
        while index < len(self._questions) and (
            self._covered_source_turns[index] is not None or index in self._time_skipped
        ):
            index += 1
        return index

    def mark_time_skipped(self, indices: "list[int] | set[int]") -> None:
        """Skip later questions because the soft time budget cannot afford them.

        Only questions after the current one that have not been delivered or covered can be
        skipped; the question in progress is never removed.
        """
        for index in indices:
            if (
                index > self._question_index
                and 0 <= index < len(self._questions)
                and self._covered_source_turns[index] is None
                and index not in self._delivered_questions
            ):
                self._time_skipped.add(index)

    @property
    def time_skipped_numbers(self) -> tuple[int, ...]:
        return tuple(sorted(index + 1 for index in self._time_skipped))

    def coverage_snapshot(self) -> dict[str, list[int] | int]:
        """Honest, number-only account of what was and was not reached."""
        numbers = range(1, len(self._questions) + 1)
        delivered = sorted(index + 1 for index in self._delivered_questions)
        covered = [n for n in numbers if self._covered_source_turns[n - 1] is not None]
        skipped = list(self.time_skipped_numbers)
        unreached = [
            n for n in numbers if n in skipped or (n not in delivered and n not in covered)
        ]
        return {
            "planned": len(self._questions),
            "asked": delivered,
            "answered": [n for n in numbers if self._answers[n - 1]],
            "covered_from_prior_turns": covered,
            "skipped_for_time": skipped,
            "no_knowledge_or_declined": sorted(i + 1 for i in self._gaps),
            "required_not_reached": [n for n in unreached if self._required[n - 1]],
            "optional_not_reached": [n for n in unreached if not self._required[n - 1]],
        }

    @property
    def greeting_response(self) -> str:
        return self._greeting_response

    @property
    def preferred_name(self) -> str | None:
        fact = self.memory.get("candidate_name")
        return fact.value if fact else None

    def remember_name(
        self,
        name: str,
        *,
        source_turn: int | None = None,
        supplied_wording: str | None = None,
        confirmed: bool = False,
        correction: bool = False,
        superseded_wording: str | None = None,
    ) -> None:
        if not name or len(name) > 40 or not all(part.isalpha() for part in name.split()):
            raise ValueError("Preferred name must be short plain text")
        self.memory.supply(
            "candidate_name",
            name,
            source_turn=source_turn,
            supplied_wording=supplied_wording or name,
            confirmed=confirmed,
            correction=correction,
            superseded_wording=superseded_wording,
        )
        self._name_declined = False

    def acknowledge_correction(
        self,
        text: str,
        *,
        discard_interrupted: bool = False,
        refund_probe: bool = False,
    ) -> Utterance:
        if self._stage in {Stage.COMPLETE, Stage.ENDED_EARLY, Stage.EARLY_CLOSING}:
            raise InvalidTransition("The interview has ended")
        interrupted = self._interrupted
        if (
            discard_interrupted
            and interrupted is not None
            and interrupted.kind
            in {
                UtteranceKind.FOLLOW_UP,
                UtteranceKind.NUDGE,
                UtteranceKind.CLARIFICATION,
            }
        ):
            if refund_probe and self._question_index >= 0:
                self._follow_ups[self._question_index] = max(
                    0, self._follow_ups[self._question_index] - 1
                )
            self._correction_return_stage = Stage.AWAITING_PROPOSAL
            self._correction_resume_utterance = None
        else:
            self._correction_return_stage = self._stage
            self._correction_resume_utterance = interrupted
        self._interrupted = None
        return self._prepare(UtteranceKind.CORRECTION_ACK, text, None)

    def resume_after_correction(self) -> Utterance | None:
        self._require(Stage.AWAITING_CORRECTION_RETURN)
        previous = self._correction_return_stage
        self._correction_return_stage = None
        if self._correction_resume_utterance is not None:
            self._interrupted = self._correction_resume_utterance
            self._correction_resume_utterance = None
            self._stage = Stage.DELIVERING
            return self.retry_interrupted()
        self._stage = previous or Stage.AWAITING_ANSWER
        return None

    def accept_interrupted_correction_ack(self) -> None:
        self._require(Stage.DELIVERING)
        if self._interrupted is None or self._interrupted.kind is not UtteranceKind.CORRECTION_ACK:
            raise InvalidTransition("No correction acknowledgement was interrupted")
        self._interrupted = None
        self._stage = Stage.AWAITING_CORRECTION_RETURN

    @property
    def remaining_follow_ups(self) -> int:
        if self._question_index < 0:
            return MAX_FOLLOW_UPS_PER_QUESTION
        return self._probe_limits[self._question_index] - self._follow_ups[self._question_index]

    def start(self, opening: str | None = None) -> Utterance:
        """Begin an interview. An assigned interview may open with a short greeting first."""
        self._require(Stage.NEW)
        if opening:
            self._opening_text = opening
            return self._prepare(UtteranceKind.GREETING, opening, None)
        return self._prepare(UtteranceKind.QUESTION, self._questions[0], 0)

    def submit_greeting(self, text: str) -> Utterance | None:
        self._require(Stage.AWAITING_GREETING)
        response = text.strip()
        if not response:
            return self.request_opening_repeat()
        self._greeting_response = response
        self._stage = Stage.AWAITING_OPENING_ACK
        return None

    def prepare_opening_transition(self, text: str) -> Utterance:
        """Assigned opening: acknowledge briefly, then move on. Delivery leads to question one.

        Opening speech never becomes an answer: question one is not current until it has
        been delivered, so nothing said here can be stored against it or spend its probes.
        """
        if self._stage not in {Stage.AWAITING_GREETING, Stage.AWAITING_OPENING_ACK}:
            raise InvalidTransition("There is no assigned opening to complete")
        cleaned = text.strip()
        if not cleaned or len(cleaned) > 300:
            raise ValueError("Opening transition must be short and nonempty")
        return self._prepare(UtteranceKind.OPENING_ACK, cleaned, None)

    def finish_after_interrupted_closing(self) -> Stage:
        """The candidate spoke over a closing; do not replay it, just end the interview."""
        self._require(Stage.DELIVERING)
        interrupted = self._interrupted
        if interrupted is None or interrupted.kind not in {
            UtteranceKind.CLOSING,
            UtteranceKind.EARLY_FEEDBACK,
        }:
            raise InvalidTransition("No closing was interrupted")
        self._interrupted = None
        self._pending = None
        self._stage = (
            Stage.ENDED_EARLY
            if interrupted.kind is UtteranceKind.EARLY_FEEDBACK
            else Stage.COMPLETE
        )
        return self._stage

    def add_greeting_continuation(self, text: str) -> None:
        self._require(Stage.AWAITING_OPENING_ACK)
        if continuation := text.strip():
            self._greeting_response = f"{self._greeting_response} {continuation}"

    def submit_name(self, name: str | None, readiness: Readiness) -> Utterance:
        self._require(Stage.AWAITING_NAME)
        if name:
            self.remember_name(name)
            acknowledgement = f"Nice to meet you, {name}."
        else:
            self._name_declined = True
            acknowledgement = "No problem."
        if self._name_detour:
            self._name_detour = False
            return self._prepare(
                UtteranceKind.DETOUR_ACK,
                f"{acknowledgement} Would you like to continue or finish?",
                self._question_index,
            )
        if readiness is Readiness.READY:
            return self._prepare(UtteranceKind.NAME_ACK, acknowledgement, None)
        if readiness is Readiness.DEFERRED:
            self._readiness_nudges = 1
            return self._prepare(
                UtteranceKind.OPENING_WAIT, f"{acknowledgement} {READINESS_WAIT}", None
            )
        return self._prepare(UtteranceKind.READINESS, f"{acknowledgement} {READINESS_PROMPT}", None)

    def resume_opening_question(self) -> Utterance:
        self._require(Stage.AWAITING_OPENING_RETURN)
        if self._opening_return_to_name and self._opening_coach_replies < 2:
            return self._prepare(UtteranceKind.NAME_PROMPT, NAME_PROMPT, None)
        return self._prepare(UtteranceKind.READINESS, READINESS_PROMPT, None)

    def acknowledge_spelled_name(self, name: str) -> Utterance:
        self._require(Stage.AWAITING_READINESS)
        return self._prepare(
            UtteranceKind.READINESS,
            f"Thanks, I'll call you {name}. {READINESS_PROMPT}",
            None,
        )

    def restore_answers(self, answers: tuple[str, ...]) -> None:
        self._require(Stage.AWAITING_PROPOSAL)
        self._answers[self._question_index] = list(answers)

    def resume_detour(self) -> Utterance | None:
        self._require(Stage.AWAITING_DETOUR_RETURN)
        self._name_detour = False
        self._return_after_detour = False
        prompt = self._detour_resume_prompt
        self._detour_resume_prompt = None
        if prompt and prompt.kind in {
            UtteranceKind.FOLLOW_UP,
            UtteranceKind.NUDGE,
            UtteranceKind.CLARIFICATION,
            UtteranceKind.REPEAT,
            UtteranceKind.DETOUR_RESUME,
        }:
            return self._prepare(UtteranceKind.DETOUR_RESUME, prompt.text, self._question_index)
        if self._answers[self._question_index]:
            self._stage = Stage.AWAITING_PROPOSAL
            return None
        return self._prepare(
            UtteranceKind.DETOUR_RESUME, self._questions[self._question_index], self._question_index
        )

    def begin_question_one(self) -> Utterance:
        self._require(Stage.AWAITING_QUESTION_START)
        return self._prepare(UtteranceKind.QUESTION, self._questions[0], 0)

    def request_opening_repeat(self) -> Utterance:
        phase = (
            Stage.AWAITING_GREETING if self._stage is Stage.AWAITING_OPENING_ACK else self._stage
        )
        if phase not in self._opening_repeats:
            raise InvalidTransition("There is no opening prompt to repeat")
        if self._opening_repeats[phase] >= MAX_OPENING_REPEATS:
            raise LimitReached("Opening repeat limit reached")
        self._opening_repeats[phase] += 1
        if phase is Stage.AWAITING_GREETING:
            self._greeting_response = ""
            return self._prepare(UtteranceKind.GREETING, self._opening_text or GREETING, None)
        return self._prepare(UtteranceKind.READINESS, READINESS_PROMPT, None)

    def submit_answer(self, text: str) -> Utterance | None:
        """Record a candidate turn; an empty turn requests one bounded repeat."""
        self._require(Stage.AWAITING_ANSWER)
        answer = text.strip()
        if not answer:
            return self.request_repeat()
        self._answers[self._question_index].append(answer)
        self._stage = Stage.AWAITING_PROPOSAL
        return None

    def request_repeat(self, active_prompt: Utterance | None = None) -> Utterance:
        """Repeat the active prompt, spending only the clarification allowance."""
        if self._stage not in {Stage.AWAITING_ANSWER, Stage.AWAITING_PROPOSAL}:
            raise InvalidTransition("There is no project prompt to repeat")
        index = self._question_index
        if self._clarifications[index] >= MAX_CLARIFICATIONS_PER_QUESTION:
            raise LimitReached("Clarification limit reached for this question")
        self._clarifications[index] += 1
        text = (
            active_prompt.text
            if active_prompt and active_prompt.question_number == index + 1
            else self._questions[index]
        )
        return self._prepare(UtteranceKind.REPEAT, text, index)

    def propose(self, kind: ReplyKind, text: str | None = None) -> Utterance | None:
        """Accept a bounded LLM proposal; Python selects the next question."""
        self._require(Stage.AWAITING_PROPOSAL)
        if not isinstance(kind, ReplyKind):
            raise ValueError("Unknown reply kind")
        index = self._question_index
        if kind is ReplyKind.ADVANCE:
            if text is not None:
                raise ValueError("ADVANCE cannot supply speech text")
            next_index = self._next_open_index(index + 1)
            if next_index == len(self._questions):
                self._stage = Stage.AWAITING_FEEDBACK
                return None
            return self._prepare(UtteranceKind.QUESTION, self._questions[next_index], next_index)

        if kind not in {ReplyKind.FOLLOW_UP, ReplyKind.NUDGE, ReplyKind.CLARIFICATION}:
            raise ValueError("Unsupported project proposal")

        if not isinstance(text, str) or not text.strip():
            raise ValueError("Spoken proposal text must be nonempty")
        if self._follow_ups[index] >= self._probe_limits[index]:
            raise LimitReached("Extra project prompt limit reached for this question")
        self._follow_ups[index] += 1
        utterance_kind = {
            ReplyKind.FOLLOW_UP: UtteranceKind.FOLLOW_UP,
            ReplyKind.NUDGE: UtteranceKind.NUDGE,
            ReplyKind.CLARIFICATION: UtteranceKind.CLARIFICATION,
        }[kind]
        return self._prepare(utterance_kind, text.strip(), index)

    def prepare_closing(self, text: str) -> Utterance:
        self._require(Stage.AWAITING_FEEDBACK)
        if not text.strip() or len(text) > 500:
            raise ValueError("Assigned closing must be short and nonempty")
        return self._prepare(UtteranceKind.CLOSING, text.strip(), None)

    @property
    def pending_prompt(self) -> Utterance | None:
        """The main question or probe that controls and detours refer to (delivered or not)."""
        return self._pending_prompt

    @property
    def pending_prompt_delivered(self) -> bool:
        return self._prompt_delivered

    def _settle_interrupted(self) -> None:
        """A control turn supersedes whatever was cut off; the pending prompt is what returns."""
        self._interrupted = None
        if self._stage is Stage.DELIVERING and self._pending is None:
            self._stage = Stage.AWAITING_ANSWER

    def repeat_pending(self) -> Utterance | None:
        """Say the pending prompt again. ``None`` means the repeat allowance is used up.

        A prompt that was never delivered is simply re-asked, since it was not heard. A repeat
        spends only the repeat allowance: never a probe or a clarification.
        """
        prompt = self._pending_prompt
        if (
            prompt is None
            or prompt.question_number is None
            or self._stage not in {Stage.AWAITING_ANSWER, Stage.AWAITING_PROPOSAL, Stage.DELIVERING}
        ):
            raise InvalidTransition("There is no pending prompt to repeat")
        index = prompt.question_number - 1
        self._settle_interrupted()
        if not self._prompt_delivered:
            return self._prepare(prompt.kind, prompt.text, index)
        if self._repeats[index] >= MAX_REPEATS_PER_PROMPT:
            return None
        self._repeats[index] += 1
        return self._prepare(UtteranceKind.REPEAT, prompt.text, index)

    def note_control_detour(self) -> bool:
        """Count a control reply. True when the interviewer should return to the prompt instead."""
        self._control_detours += 1
        return self._control_detours > MAX_CONTROL_DETOURS

    def prepare_control_reply(self, text: str) -> Utterance:
        """A short reply to a control or detour; the pending prompt is untouched and returns.

        If the pending prompt was never delivered it is re-asked in the same utterance so that it
        cannot be lost and the next answer is attributed to the right question.
        """
        prompt = self._pending_prompt
        if prompt is None or prompt.question_number is None:
            raise InvalidTransition("No assigned interview question is pending")
        self._settle_interrupted()
        if not self._prompt_delivered:
            combined = self._prepare(
                prompt.kind, f"{text.strip()} {prompt.text}", prompt.question_number - 1
            )
            self._pending_prompt = replace(combined, text=prompt.text)
            return combined
        return self._prepare(UtteranceKind.INFORMATION, text.strip(), self._question_index)

    def record_gap_answer(self, text: str) -> None:
        """Store "I don't know", "I haven't used it" or a refusal as the actual answer.

        The words are the candidate's answer for the question in play, kept as spoken, and the
        question is marked so coverage can say it was answered by not knowing or declining.
        """
        answer = text.strip()
        if self._interrupted is not None:
            self.accept_barge_in_answer(answer)
        elif self._stage is Stage.AWAITING_ANSWER:
            self.submit_answer(answer)
        elif self._stage is Stage.AWAITING_PROPOSAL:
            self.add_answer_continuation(answer)
        else:
            raise InvalidTransition("No question is in play")
        self._gaps.add(self._question_index)

    def end_early(self) -> bool:
        """Stop all question progression, including an undelivered question."""
        if self._stage in {Stage.COMPLETE, Stage.ENDED_EARLY, Stage.EARLY_CLOSING}:
            return False
        self._pending = None
        self._interrupted = None
        self._stage = Stage.EARLY_CLOSING
        return True

    def prepare_early_feedback(self, text: str) -> Utterance:
        self._require(Stage.EARLY_CLOSING)
        lines = text.splitlines()
        if not 1 <= len(lines) <= 2 or any(not line.strip() for line in lines):
            raise ValueError("Early closing must have 1 or 2 nonempty lines")
        return self._prepare(UtteranceKind.EARLY_FEEDBACK, text, None)

    def skip_current(self) -> Utterance | None:
        """Advance on an explicit skip without recording a fabricated answer."""
        self._require(Stage.AWAITING_ANSWER)
        if self._required[self._question_index] and not self._answers[self._question_index]:
            raise InvalidTransition("Required question must be addressed")
        next_index = self._next_open_index(self._question_index + 1)
        if next_index >= len(self._questions):
            self._stage = Stage.AWAITING_FEEDBACK
            return None
        return self._prepare(UtteranceKind.QUESTION, self._questions[next_index], next_index)

    def skip_interrupted(self) -> Utterance | None:
        """Apply an explicit skip during interrupted question speech."""
        self._require(Stage.DELIVERING)
        interrupted = self._interrupted
        if interrupted is None or interrupted.question_number is None:
            raise InvalidTransition("There is no interrupted question to skip")
        if interrupted.kind is UtteranceKind.QUESTION:
            self._question_index = interrupted.question_number - 1
        self._interrupted = None
        self._stage = Stage.AWAITING_ANSWER
        return self.skip_current()

    def resolve_unheard_question(self, text: str) -> Utterance:
        """Route barge-in before audio to the previously delivered question."""
        self._require(Stage.DELIVERING)
        interrupted = self._interrupted
        if interrupted is None or interrupted.kind is not UtteranceKind.QUESTION:
            raise InvalidTransition("There is no interrupted main question")
        self._interrupted = None
        if self._question_index < 0:
            return self._prepare(UtteranceKind.QUESTION, interrupted.text, 0)
        answer = text.strip()
        if answer:
            previous = self._answers[self._question_index]
            if previous:
                previous[-1] = f"{previous[-1]} {answer}"
            else:
                previous.append(answer)
        return self._prepare(
            UtteranceKind.QUESTION, interrupted.text, interrupted.question_number - 1
        )

    def resolve_unheard_follow_up(self, text: str) -> Utterance:
        """Preserve the earlier answer; an unheard extra prompt cannot consume a reply."""
        self._require(Stage.DELIVERING)
        interrupted = self._interrupted
        if interrupted is None or interrupted.kind not in {
            UtteranceKind.FOLLOW_UP,
            UtteranceKind.NUDGE,
            UtteranceKind.CLARIFICATION,
            UtteranceKind.REPEAT,
        }:
            raise InvalidTransition("There is no interrupted extra prompt")
        if text.strip() and self._answers[self._question_index]:
            answers = self._answers[self._question_index]
            answers[-1] = f"{answers[-1]} {text.strip()}"
        return self.retry_interrupted()

    def delivery_finished(self, utterance_id: int) -> bool:
        """Apply only the current pending delivery; ignore duplicate or stale IDs."""
        utterance = self._pending
        if utterance is None or utterance.id != utterance_id:
            return False
        self._pending = None
        prompt = self._pending_prompt
        if prompt is not None and utterance.id == prompt.id:
            self._prompt_delivered = True
        elif (
            utterance.kind is UtteranceKind.REPEAT
            and prompt is not None
            and utterance.question_number == prompt.question_number
        ):
            # A repeat that completes also settles which question the candidate is answering.
            self._question_index = utterance.question_number - 1
            if prompt.kind is UtteranceKind.QUESTION:
                self._delivered_questions.add(self._question_index)
        if utterance.kind is UtteranceKind.GREETING:
            self._stage = Stage.AWAITING_GREETING
        elif utterance.kind in {UtteranceKind.OPENING_ACK, UtteranceKind.NAME_ACK}:
            self._stage = Stage.AWAITING_QUESTION_START
        elif utterance.kind is UtteranceKind.NAME_PROMPT:
            self._stage = Stage.AWAITING_NAME
        elif utterance.kind is UtteranceKind.OPENING_COACH_REPLY:
            self._stage = Stage.AWAITING_OPENING_RETURN
        elif utterance.kind in {
            UtteranceKind.DETOUR_ACK,
            UtteranceKind.PROVISIONAL_FEEDBACK,
            UtteranceKind.TOPIC_BOUNDARY,
        }:
            self._stage = (
                Stage.AWAITING_DETOUR_RETURN
                if self._name_detour or self._return_after_detour
                else Stage.AWAITING_CONTINUE_CHOICE
            )
        elif utterance.kind is UtteranceKind.NO_PROJECT_INVITE:
            self._stage = Stage.AWAITING_NO_PROJECT_EXAMPLE
        elif utterance.kind is UtteranceKind.NO_PROJECT_SCOPE:
            self._stage = Stage.AWAITING_NO_PROJECT_CHOICE
        elif utterance.kind in {
            UtteranceKind.READINESS,
            UtteranceKind.OPENING_WAIT,
            UtteranceKind.OPENING_CLARIFICATION,
        }:
            self._stage = Stage.AWAITING_READINESS
        elif utterance.kind in {UtteranceKind.QUESTION, UtteranceKind.DETOUR_RESUME}:
            if utterance.question_number is None:
                raise RuntimeError("Question utterance has no question number")
            self._question_index = utterance.question_number - 1
            if utterance.kind is UtteranceKind.QUESTION:
                self._delivered_questions.add(self._question_index)
            self._stage = Stage.AWAITING_ANSWER
        elif utterance.kind in {UtteranceKind.FEEDBACK, UtteranceKind.CLOSING}:
            self._stage = Stage.COMPLETE
        elif utterance.kind is UtteranceKind.EARLY_FEEDBACK:
            self._stage = Stage.ENDED_EARLY
        elif utterance.kind is UtteranceKind.CORRECTION_ACK:
            self._stage = Stage.AWAITING_CORRECTION_RETURN
        else:
            self._stage = Stage.AWAITING_ANSWER
        return True

    def interrupt(self, utterance_id: int) -> bool:
        """Keep interrupted speech recoverable and invalidate its old ID."""
        utterance = self._pending
        if utterance is None or utterance.id != utterance_id:
            return False
        self._pending = None
        self._interrupted = utterance
        return True

    def retry_interrupted(self) -> Utterance:
        """Requeue interrupted text with a fresh ID after barge-in has settled."""
        self._require(Stage.DELIVERING)
        utterance = self._interrupted
        if utterance is None:
            raise InvalidTransition("There is no interrupted utterance to retry")
        self._interrupted = None
        return self._prepare(utterance.kind, utterance.text, self._index_for(utterance))

    def accept_barge_in_answer(self, text: str) -> bool:
        """Use a substantive answer to resolve interrupted speech without replaying it.

        An answer to a partly heard main question explicitly commits that
        question. Empty transcription leaves the utterance recoverable.
        """
        self._require(Stage.DELIVERING)
        interrupted = self._interrupted
        if interrupted is None:
            raise InvalidTransition("There is no interrupted utterance")
        answer = text.strip()
        if not answer:
            return False
        if interrupted.kind is UtteranceKind.GREETING:
            self._greeting_response = answer
            self._stage = Stage.AWAITING_OPENING_ACK
        elif interrupted.kind is UtteranceKind.OPENING_ACK:
            self._stage = Stage.AWAITING_QUESTION_START
        elif interrupted.kind is UtteranceKind.NAME_PROMPT:
            self._stage = Stage.AWAITING_NAME
        elif interrupted.kind is UtteranceKind.OPENING_COACH_REPLY:
            self._stage = Stage.AWAITING_OPENING_RETURN
        elif interrupted.kind is UtteranceKind.NAME_ACK:
            self._stage = Stage.AWAITING_QUESTION_START
        elif interrupted.kind in {
            UtteranceKind.DETOUR_ACK,
            UtteranceKind.PROVISIONAL_FEEDBACK,
            UtteranceKind.TOPIC_BOUNDARY,
        }:
            self._stage = (
                Stage.AWAITING_DETOUR_RETURN
                if self._name_detour or self._return_after_detour
                else Stage.AWAITING_CONTINUE_CHOICE
            )
        elif interrupted.kind is UtteranceKind.NO_PROJECT_INVITE:
            self._stage = Stage.AWAITING_NO_PROJECT_EXAMPLE
        elif interrupted.kind is UtteranceKind.NO_PROJECT_SCOPE:
            self._stage = Stage.AWAITING_NO_PROJECT_CHOICE
        elif interrupted.kind in {
            UtteranceKind.READINESS,
            UtteranceKind.OPENING_WAIT,
            UtteranceKind.OPENING_CLARIFICATION,
        }:
            self._stage = Stage.AWAITING_READINESS
        elif interrupted.kind is UtteranceKind.CLOSING:
            self._stage = Stage.AWAITING_FEEDBACK
        elif interrupted.kind is UtteranceKind.FEEDBACK:
            self._closing_remarks.append(answer)
            self._stage = Stage.AWAITING_FEEDBACK
        else:
            if interrupted.kind in {UtteranceKind.QUESTION, UtteranceKind.DETOUR_RESUME}:
                if interrupted.question_number is None:
                    raise RuntimeError("Question utterance has no question number")
                self._question_index = interrupted.question_number - 1
            self._answers[self._question_index].append(answer)
            self._stage = Stage.AWAITING_PROPOSAL
        self._interrupted = None
        return True

    def add_answer_continuation(self, text: str) -> None:
        """Extend the latest answer when speech resumes during LLM planning."""
        self._require(Stage.AWAITING_PROPOSAL)
        continuation = text.strip()
        if continuation:
            latest = self._answers[self._question_index]
            latest[-1] = f"{latest[-1]} {continuation}"

    def _index_for(self, utterance: Utterance) -> int | None:
        return utterance.question_number - 1 if utterance.question_number is not None else None

    def _prepare(self, kind: UtteranceKind, text: str, index: int | None) -> Utterance:
        utterance = Utterance(
            id=self._next_utterance_id,
            kind=kind,
            text=text,
            question_number=index + 1 if index is not None else None,
        )
        self._next_utterance_id += 1
        self._pending = utterance
        self._stage = Stage.DELIVERING
        if kind in PROMPT_KINDS and index is not None:
            # A new main question or probe becomes the prompt that controls refer to.
            self._pending_prompt = utterance
            self._prompt_delivered = False
            self._control_detours = 0
        return utterance

    def _require(self, stage: Stage) -> None:
        if self._stage is not stage:
            raise InvalidTransition(f"Expected {stage.name}; current stage is {self._stage.name}")
