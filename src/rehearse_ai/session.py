"""Connect one interview controller to speech, inference, and session events."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rehearse_ai.browser_events import candidate_event
from rehearse_ai.budget import (
    ActiveClock,
    RemainingQuestion,
    pressure,
    question_seconds,
    skip_for_time,
)
from rehearse_ai.controls import (
    END_CLAUSE as _END_CLAUSE,
)
from rehearse_ai.controls import (
    WITHDRAW_CLAUSE as _WITHDRAW_CLAUSE,
)
from rehearse_ai.controls import (
    Intent,
    interpret,
)
from rehearse_ai.interview import (
    QUESTIONS,
    InterviewController,
    InvalidTransition,
    LimitReached,
    Readiness,
    ReplyKind,
    Stage,
    Utterance,
    UtteranceKind,
)
from rehearse_ai.memory import FACT_FIELDS, FactField
from rehearse_ai.session_log import SessionLog

# The opening is one exchange: greeting, the candidate's reply, a short acknowledgement and a
# transition. If the candidate says nothing it moves on after this wait. The whole opening is
# counted in active time, and its length is reported with the interview's coverage summary.
# Interruption: audio that played for less than this was not meaningfully heard. This is measured
# on the server at the transport output; it approximates, and is not, what the browser played.
HEARD_MIN_SECS = 1.5
HEARD_MIN_FRACTION = 0.5
WORDS_PER_SECOND = 2.6
# Short floor-control phrases ("go on", "yes please cut") are not answers.
_FLOOR_CONTROL = re.compile(
    r"^(?:(?:yes|yeah|yep|okay|ok|sure|sorry|please|thanks|thank you|go on|go ahead|continue|"
    r"carry on|hold on|wait|one second|just a second|never mind|cut|cut in|cut it)[,.!?\s]*){1,5}$",
    re.I,
)
# Spoken text for assigned-interview controls. Fixed on purpose: nothing here comes from a model, so
# none of it can invent a contact, a policy or a promise about the outcome.
FEEDBACK_DECLINED = (
    "I can't provide interview feedback. "
    "Whenever you're ready, you can answer, or ask me to repeat the question."
)
RESTART_UNAVAILABLE = (
    "I can't restart the interview or go back to an earlier question in this session. "
    "I can repeat the current question, or we can end the interview."
)
FINISH_CLARIFICATION = (
    "Would you like to end the interview, or are you finished with this question?"
)
REPEAT_EXHAUSTED = (
    "I've already repeated that. You can answer as best you can, tell me you don't know, "
    "or ask to end the interview."
)
OUTCOME_UNKNOWN = (
    "I don't have any information about hiring decisions, contacts or next steps. "
    "I can repeat the question, or we can end the interview."
)
OFF_TOPIC_REPLY = (
    "Let's stay with the interview. You can answer the current question or ask me to repeat it."
)
# A plain "yes" is only taken as agreement when it answers the end-of-interview clarification.
_AFFIRM = re.compile(
    r"(?:yes|yeah|yep|sure|please do|yes please|end it|that's right|correct)[\s.!,]*", re.I
)
OPENING_REPLY_WAIT_SECS = 12.0
OPENING_ACK_TIMEOUT_SECS = 4.0
_OPENING_KINDS = {UtteranceKind.GREETING, UtteranceKind.OPENING_ACK}
_NAME_TOKEN = re.compile(r"[^\W\d_][^\W\d_'’.\-]*(?:['’.\-][^\W\d_]+)*")

Infer = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]]
Speak = Callable[[Utterance], Awaitable[None]]
Publish = Callable[[dict[str, Any]], None]
CancelAudio = Callable[[], Awaitable[None]]

_REPEAT = re.compile(
    r"^(?:(?:sorry|pardon|excuse me),?\s+)?(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?"
    r"(?:repeat(?: that| the (?:question|last question))?"
    r"|say (?:that|the question) again)(?:\s+one more time)?\??[.!]?$",
    re.IGNORECASE,
)
_SKIP = re.compile(r"^(?:please\s+)?(?:skip(?: this question)?|next question)\??[.!]?$", re.I)
_CORRECTION = re.compile(
    r"^\s*(?:(?:actually|sorry)\s*[, ]\s*)?"
    r"(?P<cue>it's|it is|i said|i meant|i used|my name is)\s+"
    r"(?P<replacement>.+?)\s*,?\s+not\s+(?P<superseded>[^.!?,]+)"
    r"(?P<remainder>[.!?,].*)?$",
    re.I,
)


@dataclass(frozen=True)
class _CorrectionRequest:
    cue: str
    replacement: str
    superseded: str
    remainder: str


def _parse_correction(text: str) -> _CorrectionRequest | None:
    clauses = re.split(r"(?<=[.!?])\s+", text.replace("’", "'").strip())
    for index, clause in enumerate(clauses):
        match = _CORRECTION.fullmatch(clause)
        if match:
            remainder = [*clauses[:index]]
            remainder.extend(
                part for part in [(match["remainder"] or "").lstrip(".!?, ").strip()] if part
            )
            remainder.extend(clauses[index + 1 :])
            return _CorrectionRequest(
                match["cue"].lower(),
                match["replacement"].strip(" ,"),
                match["superseded"].strip(),
                " ".join(remainder).strip(),
            )
    return None


_DONE_CLAUSE = re.compile(r"(?:i'm|i am|we're|we are)\s+(?:done|finished)", re.I)
_DEFER_READINESS = re.compile(
    r"\b(?:not (?:ready|yet|now)|do not (?:start|begin)|do not want to"
    r"|give me (?:a |one )?(?:moment|minute|second)|need (?:a |one )?(?:moment|minute|second)"
    r"|hold on|wait|maybe later|not quite|later)\b"
)
_AFFIRM_READINESS = re.compile(
    r"\b(?:i am ready(?: to (?:start|begin|go))?|i am good to go"
    r"|let us (?:get|go) into it|let us (?:start|begin|go|do it)"
    r"|we can (?:start|begin)|go ahead|ready to (?:start|begin|go))"
    r"(?: now| please)?$"
)
_BARE_AFFIRM_READINESS = re.compile(
    r"(?:yes|yeah|yep|sure|okay|ok|absolutely|of course|sounds good|ready"
    r"|start|begin|let us go|let us do it)(?: now| please)?"
)
_FINAL_START = re.compile(r"\b(?:start|begin)(?: now| please)?$")
_NAME_INTRO = re.compile(
    r"\b(i(?:'m| am)|my name is|name is|call me)\s*,?\s+([^\W\d_]+)(?:\s+([^\W\d_]+))?",
    re.I,
)
_NAME_SPELLING = re.compile(
    r"^\s*(?:(?:actually|sorry)[, ]+)?"
    r"(?P<cue>my name is spelled|i spell my name(?: as)?|"
    r"i spell it(?: as)?|(?:it's|it is) spelled|spelled(?: as)?)\s+"
    r"(?P<letters>.+?)\s*$",
    re.I,
)
_NAME_DECLINE = re.compile(
    r"\b(?:rather not|prefer not|do not want to|don't want to|no need to)\b.*\bname\b",
    re.I,
)
_NAME_DETOUR = re.compile(
    r"\bask\w*\b.{0,35}\bmy name\b"
    r"|\bwhat\b.{0,20}\bcall me\b",
    re.I,
)
_NAME_KNOWLEDGE = re.compile(r"\b(?:know|forget\w*|remember\w*)\b.{0,35}\bmy name\b", re.I)
_NON_NAMES = {
    "a",
    "an",
    "bit",
    "can",
    "doing",
    "fine",
    "good",
    "great",
    "happy",
    "here",
    "hello",
    "hey",
    "hi",
    "okay",
    "ok",
    "ready",
    "really",
    "sorry",
    "tired",
    "well",
    "nervous",
    "no",
    "nah",
    "not",
    "so",
    "very",
    "yes",
    "yeah",
    "yep",
    "start",
    "begin",
    "sure",
    "spelled",
    "you",
}


def _letters_as_name(text: str, *, allow_word: bool = False) -> str | None:
    parts = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    if len(parts) >= 2 and all(len(part) == 1 for part in parts):
        name = "".join(parts)
        return name[0].upper() + name[1:].lower() if len(name) <= 40 else None
    if allow_word and len(parts) == 1 and len(parts[0]) <= 40:
        word = parts[0]
        return word[0].upper() + word[1:]
    return None


def _spoken_name_spelling(text: str, *, prompted: bool) -> tuple[str, str] | None:
    # Deepgram may punctuate individually spoken letters as separate sentences.
    joined = re.sub(r"\b([^\W\d_])\.\s+(?=[^\W\d_]\.)", r"\1 ", text)
    clauses = re.split(r"(?<=[.!?])\s+", joined.strip())
    for index, clause in enumerate(clauses):
        match = _NAME_SPELLING.fullmatch(clause.strip(" .!?"))
        if match:
            cue = match["cue"].lower()
            if not prompted and "name" not in cue:
                continue
            name = _letters_as_name(match["letters"], allow_word=True)
        elif prompted:
            name = _letters_as_name(clause.strip(" .!?"))
        else:
            name = None
        if name and name.casefold() not in _NON_NAMES:
            remainder = " ".join([*clauses[:index], *clauses[index + 1 :]]).strip()
            return name, remainder
    return None


def _preferred_name(text: str, *, prompted: bool) -> str | None:
    """Accept explicit self-identification, or a short answer to a name question."""
    normalized = text.replace("’", "'").strip()
    if spelling := _spoken_name_spelling(normalized, prompted=prompted):
        return spelling[0]
    match = _NAME_INTRO.search(normalized)
    if match:
        prefix, first, second = match.groups()
        explicit_name = prefix.lower() in {"my name is", "name is", "call me"}
        if first.lower() not in _NON_NAMES and (explicit_name or first[0].isupper()):
            parts = [first]
            if second and second.lower() not in _NON_NAMES and second[0].isupper():
                parts.append(second)
            return " ".join(parts)
    if prompted:
        candidate = normalized.strip(" .!?")
        parts = candidate.split()
        if (
            not normalized.endswith("?")
            and 1 <= len(parts) <= 2
            and all(part.isalpha() and part.lower() not in _NON_NAMES for part in parts)
            and (len(parts) == 1 or all(part[0].isupper() for part in parts))
        ):
            return " ".join(part[0].upper() + part[1:] for part in parts)
    return None


def _asks_about_name(text: str) -> bool:
    addressed_to_coach = re.search(r"\b(?:you|your)\b", text, re.I)
    return bool(
        addressed_to_coach
        and (_NAME_DETOUR.search(text) or ("?" in text and _NAME_KNOWLEDGE.search(text)))
    )


def _asks_for_question_scope(text: str) -> bool:
    """Recognize a short request for the subject of the active question."""
    normalized = re.sub(r"[^a-z ]", " ", text.lower())
    normalized = " ".join(normalized.split())
    return bool(
        re.search(r"\b(?:on|about) what\b|\bwhat do you mean\b", normalized)
        and len(normalized.split()) <= 9
    )


def _declines_detail(text: str) -> bool:
    """Accept a clear inability or refusal without demanding another example."""
    normalized = " ".join(text.lower().replace("’", "'").split()).strip(" .!?")
    normalized = re.sub(r"^(?:no|sorry)[.!?, ]+", "", normalized)
    return bool(
        re.fullmatch(
            r"(?:i |we )?(?:don't|do not) know(?: (?:that|how|any|of one))?"
            r"|(?:i |we )?(?:can't|cannot) (?:remember|recall|say)"
            r"|(?:i |we )?(?:don't|do not) (?:want to|wanna) (?:answer|clarify|share|say)"
            r"|(?:i |we )?(?:don't|do not) have (?:a |any )?(?:metric|example|number|data)"
            r"|(?:i'd|i would|we'd|we would) rather not(?: answer| say)?",
            normalized,
        )
    )


def _looks_unfinished(text: str) -> bool:
    """Conservatively retain a turn ending in an unfinished clause."""
    normalized = text.lower().strip().rstrip(".,!? ")
    return bool(
        re.search(r"\b(?:the idea|will be|because|that|and|but|so|to|of|with)$", normalized)
    )


def _is_sentence_request(pattern: re.Pattern[str], text: str) -> bool:
    """Recognize a whole control clause even after a separate preamble."""
    return any(pattern.fullmatch(clause.strip()) for clause in re.split(r"(?<=[.!?])\s+", text))


def _may_need_opening_interpretation(text: str) -> bool:
    return "?" in text or "your name" in text.lower() or "call you" in text.lower()


def _asks_coach_identity(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:what(?:'s| is) your name|who are you|"
            r"what (?:should|can|do) (?:i|we) call you|"
            r"(?:can|could|would) you (?:tell|give) me your name)\b",
            text.replace("’", "'"),
            re.I,
        )
    )


def _classify_readiness(text: str) -> Readiness:
    normalized = text.lower().replace("’", "'")
    for old, new in {"i'm": "i am", "let's": "let us", "don't": "do not"}.items():
        normalized = normalized.replace(old, new)
    words = re.findall(r"[a-z]+", normalized)
    words = [word for index, word in enumerate(words) if index == 0 or word != words[index - 1]]
    normalized = " ".join(words)
    if normalized in {"no", "nah", "nope"}:
        return Readiness.DEFERRED
    if _DEFER_READINESS.search(normalized):
        return Readiness.DEFERRED
    if (
        _AFFIRM_READINESS.search(normalized)
        or _BARE_AFFIRM_READINESS.fullmatch(normalized)
        or _FINAL_START.search(normalized)
    ):
        return Readiness.READY
    return Readiness.UNCLEAR


def _classify_greeting_readiness(text: str) -> Readiness:
    classification = _classify_readiness(text)
    if classification is not Readiness.UNCLEAR:
        return classification
    clauses = [_classify_readiness(clause) for clause in re.split(r"[.!?]+", text)]
    if Readiness.DEFERRED in clauses:
        return Readiness.DEFERRED
    if Readiness.READY in clauses:
        return Readiness.READY
    return Readiness.UNCLEAR


def _fallback_opening_ack(text: str) -> str:
    if re.search(r"\bhow (?:are|r|about) you\b|\bhow's it going\b", text, re.I):
        return "Doing well, thanks."
    return "Thanks for telling me."


def _presence_check(text: str) -> bool:
    return bool(re.match(r"^\s*(?:hello|hi|hey|are you there|can you hear me)\b", text, re.I))


def _explicit_continuation_choice(text: str) -> str | None:
    """Resolve clear replies to an offered choice without model inference."""
    words = " ".join(re.findall(r"[a-z]+", text.lower().replace("’", "'")))
    if re.fullmatch(r"continue what|continue with what", words):
        return "explain"
    if re.fullmatch(
        r"(?:yes |yeah |please )?(?:continue|keep going|go on)"
        r"(?: (?:with |the |my )*(?:interview|project|practice))?",
        words,
    ):
        return "continue"
    if re.fullmatch(r"(?:no |please )?(?:finish|end|stop)(?: here| now)?", words):
        return "finish"
    return None


def _broad_discouragement(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:not|never)\s+proud\b|\bproud\s+of\s+(?:nothing|no\s+thing)\b"
            r"|\b(?:hopeless|worthless)\b",
            text,
            re.I,
        )
    )


def _is_stop_request(text: str) -> bool:
    """Match a complete, affirmative request clause, including STT sentence splits."""
    normalized = re.sub(r"\bwanna\b", "want to", text.replace("’", "'"), flags=re.I)
    for sentence in re.split(r"[.!?]+", normalized):
        for clause in re.split(r"\b(?:but|because)\b", sentence, flags=re.I):
            clause = " ".join(re.sub(r"[,;:]", " ", clause).split())
            if (
                _END_CLAUSE.fullmatch(clause)
                or _DONE_CLAUSE.fullmatch(clause)
                or _WITHDRAW_CLAUSE.fullmatch(clause)
            ):
                return True
    return False


class InterviewSession:
    def __init__(
        self,
        log: SessionLog,
        infer: Infer,
        speak: Speak,
        publish: Publish,
        *,
        turn_pause_secs: float = 1.1,
        cancel_audio: CancelAudio | None = None,
        build_id: str | None = None,
        interview_plan: dict[str, Any] | None = None,
        self_reported_name: str | None = None,
        clock: Callable[[], float] | None = None,
        opening: bool = True,
        opening_wait_secs: float = OPENING_REPLY_WAIT_SECS,
    ) -> None:
        self._assigned_plan = interview_plan
        self._now = clock or time.monotonic
        self._clock = ActiveClock(self._now)
        self._marks: dict[str, float] = {}
        self._output_started_at: dict[int, float] = {}
        self._closing_replays = 0
        self._finish_clarified = False
        self._opening = bool(opening and interview_plan)
        self._opening_wait_secs = opening_wait_secs
        self._opening_task: asyncio.Task[None] | None = None
        self._opening_seconds: int | None = None
        self._first_name_confirmed = self._first_name(self_reported_name)
        self._greeting = self._greeting_for(self_reported_name)
        self._target_seconds = (
            int(interview_plan.get("target_minutes", 0)) * 60 if interview_plan else 0
        )
        self._question_seconds = (
            question_seconds(interview_plan["questions"], self._target_seconds)
            if interview_plan and self._target_seconds
            else []
        )
        self._ended_for_time = False
        self._coverage_recorded = False
        plan_questions = interview_plan["questions"] if interview_plan else None
        self.controller = InterviewController(
            tuple(item["question"] for item in plan_questions) if plan_questions else None,
            probe_limits=tuple(item["max_probes"] for item in plan_questions)
            if plan_questions
            else None,
            required=tuple(item["required"] for item in plan_questions) if plan_questions else None,
        )
        if self_reported_name:
            try:
                self.controller.remember_name(
                    self_reported_name, supplied_wording=self_reported_name, confirmed=True
                )
            except ValueError:
                # A real name such as "Anne-Marie O'Neil" is not "short plain text" to the memory
                # rules. It must not abort the interview; it is simply not stored as a fact.
                pass
        self.log = log
        self._infer = infer
        self._speak = speak
        self._publish = publish
        self._cancel_audio = cancel_audio
        self._response_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._speaking_id: int | None = None
        self._active_prompt_id: int | None = None
        self._last_delivered_prompt: Utterance | None = None
        self._last_project_prompt: Utterance | None = None
        self._output_started_ids: set[int] = set()
        self._tts_completed_ids: set[int] = set()
        self._tts_contexts: dict[str, int] = {}
        self._tts_context_history: dict[str, int] = {}
        self._tts_context_started_at: dict[str, float] = {}
        self._tts_audio_stats: dict[str, list[int]] = {}
        self._provider_tts_stats: dict[str, list[float | int]] = {}
        self._first_audio_ids: set[int] = set()
        self._interrupted_output_id: int | None = None
        self._cancelled_output_stop_debt = 0
        self._last_turn_end: float | None = None
        self._turn_started_at: float | None = None
        self._turn_number = 0
        self._answer_number = 0
        self._turn_pause_secs = turn_pause_secs
        self._build_id = build_id
        self._stop_latched = False
        self._closed = False
        self._media_paused = False
        self._answer_snapshot: tuple[str, ...] | None = None
        self._latest_candidate_turn = ""
        self._opening_interpret_text: str | None = None
        self._no_project_turn: str | None = None
        self._choice_turn: str | None = None
        self._continued_prompt_id: int | None = None
        self._end_reason = "user_request"
        self._interrupted_candidate_turn: str | None = None
        self._project_prompt_for_detour: Utterance | None = None
        self._pending_correction: _CorrectionRequest | None = None

    @property
    def response_task(self) -> asyncio.Task[None] | None:
        return self._response_task

    @property
    def media_paused(self) -> bool:
        return self._media_paused

    async def media_pause(self, reason: str) -> None:
        if (
            self._closed
            or self._media_paused
            or self.controller.stage
            in {Stage.NEW, Stage.COMPLETE, Stage.ENDED_EARLY, Stage.EARLY_CLOSING}
        ):
            return
        self._media_paused = True
        self._cancel_opening_timer()
        self._clock.pause()
        self._generation += 1
        task = self._response_task
        if task and not task.done():
            task.cancel()
        self._response_task = None
        pending = self.controller.pending
        if pending is not None and self.controller.interrupt(pending.id):
            if pending.id in self._output_started_ids:
                self._cancelled_output_stop_debt += 1
            self._speaking_id = None
            self._tts_contexts = {
                context: identifier
                for context, identifier in self._tts_contexts.items()
                if identifier != pending.id
            }
            self._record(
                "agent_interrupted",
                speaker="agent",
                utterance_id=pending.id,
                text=pending.text,
                interrupted=True,
                output_started=pending.id in self._output_started_ids,
                cancellation_reason="media_lost",
            )
            self._record(
                "agent_cancellation",
                speaker=None,
                utterance_id=pending.id,
                cancellation_reason="media_lost",
            )
        self._record("media_paused", speaker=None, reason=reason)
        if self._cancel_audio:
            await self._cancel_audio()

    async def media_resume(self) -> None:
        if self._closed or not self._media_paused:
            return
        self._media_paused = False
        self._clock.resume()
        self._record("media_resumed", speaker=None)
        if self.controller.stage is Stage.AWAITING_GREETING:
            self._arm_opening_timer()
        if self.controller.interrupted_utterance is not None:
            await self._queue_speech(self.controller.retry_interrupted())
        elif self.controller.stage in {
            Stage.AWAITING_PROPOSAL,
            Stage.AWAITING_OPENING_ACK,
            Stage.AWAITING_FEEDBACK,
        }:
            self._start_response()

    @staticmethod
    def _first_name(name: str | None) -> str:
        """The first name to address, from the confirmed name. Plain data: never put in a prompt."""
        token = name.split()[0] if name and name.split() else ""
        return token if token and len(token) <= 30 and _NAME_TOKEN.fullmatch(token) else ""

    def _greeting_for(self, self_reported_name: str | None) -> str:
        first = self._first_name(self_reported_name)
        return f"Hi {first}, how's your day going?" if first else "Hi, how's your day going?"

    def _address_name(self) -> str:
        """Who to address in closings: a spoken correction wins over the confirmed name."""
        return self._first_name(self.controller.preferred_name) or self._first_name_confirmed

    def _closing_text(self) -> str:
        name = self._address_name()
        return (
            f"That concludes the interview. Thank you for your time{', ' + name if name else ''}."
        )

    def _early_closing_text(self) -> str:
        name = self._address_name()
        return (
            f"Thank you for your time{', ' + name if name else ''}. We'll end the interview here."
        )

    def _cancel_opening_timer(self) -> None:
        task, self._opening_task = self._opening_task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _arm_opening_timer(self) -> None:
        self._cancel_opening_timer()
        if self._opening_wait_secs > 0 and not self._closed:
            self._opening_task = asyncio.create_task(self._opening_timeout())

    async def _opening_timeout(self) -> None:
        try:
            await asyncio.sleep(self._opening_wait_secs)
        except asyncio.CancelledError:
            return
        if (
            self._closed
            or self._media_paused
            or self._stop_latched
            or self.controller.stage is not Stage.AWAITING_GREETING
        ):
            return
        self._record("opening_reply_timeout", speaker=None)
        await self._queue_speech(
            self.controller.prepare_opening_transition("Whenever you're ready, let's get started.")
        )

    async def _route_assigned_opening(self, answer: str, interrupted: Utterance | None) -> None:
        """Candidate speech during the opening. It can never become an interview answer."""
        controller = self.controller
        if interrupted is not None and interrupted.kind is UtteranceKind.OPENING_ACK:
            # They spoke over the acknowledgement: nothing is lost by moving straight on.
            if answer and controller.accept_barge_in_answer(answer):
                self._interrupted_output_id = None
                await self._queue_speech(controller.begin_question_one())
            else:
                await self._queue_speech(controller.retry_interrupted())
            return
        if interrupted is not None and interrupted.kind is UtteranceKind.GREETING:
            if not answer:
                await self._queue_speech(controller.retry_interrupted())
                return
            # Talking over the greeting is a reply to it, however early it began.
            controller.accept_barge_in_answer(answer)
            self._interrupted_output_id = None
        elif controller.stage is Stage.AWAITING_OPENING_ACK:
            if answer:
                controller.add_greeting_continuation(answer)
        elif controller.stage is Stage.AWAITING_GREETING:
            if not answer:
                self._arm_opening_timer()
                return
            if interpret(answer).kind == "repeat":
                try:
                    await self._queue_speech(controller.request_opening_repeat())
                    return
                except LimitReached:
                    self._record("opening_repeat_limit", speaker=None, opening_phase="greeting")
            controller.submit_greeting(answer)
        else:
            return
        if _classify_greeting_readiness(answer) is Readiness.READY:
            await self._queue_speech(
                controller.prepare_opening_transition("Great, let's get started.")
            )
            return
        self._start_response()

    async def _respond_opening_ack(self, generation: int) -> None:
        """One short, natural acknowledgement of the greeting reply, then the transition."""
        reply = self.controller.greeting_response
        self._record(
            "inference_started",
            speaker=None,
            generation=generation,
            purpose="opening_ack",
            llm_first_token_ms=None,
        )
        ack = ""
        try:
            self._mark("decision_start")
            result = await asyncio.wait_for(
                self._infer("opening_ack", {"greeting_response": reply}), OPENING_ACK_TIMEOUT_SECS
            )
            self._mark("decision_done")
            ack = str(result.get("text", "")).strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record(
                "response_error",
                speaker=None,
                generation=generation,
                error_type=type(exc).__name__,
                failure_stage="opening_inference",
            )
        if (
            not ack
            or len(ack) > 160
            or "?" in ack
            or re.search(
                r"\b(?:strength|weakness|score|feedback|improve|coach|great job)\b", ack, re.I
            )
        ):
            ack = _fallback_opening_ack(reply)
        if generation != self._generation or self._closed or self._media_paused:
            self._record("inference_stale_discarded", speaker=None, generation=generation)
            return
        self._record(
            "inference_completed",
            speaker=None,
            generation=generation,
            purpose="opening_ack",
            validated=True,
        )
        await self._queue_speech(
            self.controller.prepare_opening_transition(f"{ack} Let's get started.")
        )

    def _mark(self, name: str, *, first: bool = False) -> None:
        if not first or name not in self._marks:
            self._marks[name] = self._now()

    def _record_response_latency(self, utterance: Utterance) -> None:
        """Server-side breakdown of one response, from the end of the candidate's speech.

        Every value is a difference between two server monotonic readings. ``output_started`` is
        Pipecat's transport output, not browser playback; ``vad_stop`` is a Silero estimate of
        the end of speech, not the last audible phoneme. Stages that were not observed are None.
        """
        marks, turn_end = self._marks, self._marks.get("turn_complete")
        if turn_end is None:
            return

        def gap(later: str, earlier: str) -> int | None:
            if later in marks and earlier in marks:
                return round((marks[later] - marks[earlier]) * 1000)
            return None

        self._record(
            "response_latency",
            speaker=None,
            utterance_id=utterance.id,
            utterance_kind=utterance.kind.name.lower(),
            stt_final_after_speech_end_ms=gap("last_stt", "vad_stop"),
            speech_end_to_turn_complete_ms=gap("turn_complete", "vad_stop"),
            last_stt_to_turn_complete_ms=gap("turn_complete", "last_stt"),
            turn_complete_to_model_start_ms=gap("decision_start", "turn_complete"),
            model_ms=gap("decision_done", "decision_start"),
            model_done_to_prepared_ms=gap("prepared", "decision_done"),
            prepared_to_tts_first_audio_ms=gap("first_audio", "prepared"),
            tts_first_audio_to_output_started_ms=gap("output_started", "first_audio"),
            turn_complete_to_output_started_ms=gap("output_started", "turn_complete"),
            speech_end_to_output_started_ms=gap("output_started", "vad_stop"),
            basis=(
                "server monotonic clock; output_started is server transport output, not browser "
                "playback; vad_stop is an estimate of the end of speech"
            ),
        )
        for name in ("turn_complete", "decision_start", "decision_done", "prepared", "first_audio"):
            marks.pop(name, None)

    def _heard_enough(self, utterance: Utterance, heard_secs: float) -> bool:
        """Did the candidate plausibly hear enough of this to respond to it?"""
        expected = max(0.5, len(utterance.text.split()) / WORDS_PER_SECOND)
        return heard_secs >= min(HEARD_MIN_SECS, HEARD_MIN_FRACTION * expected)

    async def _finish_interrupted_closing(self, interrupted: Utterance) -> None:
        """Speech over a closing: replay it once if barely heard, otherwise end without resuming."""
        heard = self._interrupted_output_id == interrupted.id
        if not heard and self._closing_replays < 1:
            self._closing_replays += 1
            self._record("closing_replayed_after_early_interruption", speaker=None)
            await self._queue_speech(self.controller.retry_interrupted())
            return
        stage = self.controller.finish_after_interrupted_closing()
        self._record("closing_interrupted_not_resumed", speaker=None, heard=heard)
        if stage is Stage.ENDED_EARLY:
            self._record_coverage("ended_early")
            self._record("interview_ended_early", speaker=None)
        else:
            self._record_coverage("time_budget" if self._ended_for_time else "completed")
            self._record("interview_complete", speaker=None)

    def _record(self, event: str, **details: Any) -> None:
        entry = self.log.record(event, **details)
        # The log is internal evidence. Only allowlisted, minimal payloads reach the browser.
        visible = candidate_event(entry)
        if visible is not None:
            self._publish({"type": "rehearseai", "event": visible})
        if self.log.persistence_failed:
            self.log.persistence_failed = False
            self._publish({"type": "rehearseai", "event": {"event": "persistence_error"}})

    def _remember_spoken_name(self, name: str, wording: str) -> None:
        previous = self.controller.memory.get("candidate_name")
        self.controller.remember_name(
            name,
            source_turn=self._turn_number or None,
            supplied_wording=wording,
            confirmed=False,
        )
        current = self.controller.memory.get("candidate_name")
        if current is not previous:
            self._record(
                "memory_updated",
                speaker=None,
                field="candidate_name",
                value=current.value,
                confirmed=current.confirmed,
                source_turn=current.source_turn,
                update_source="spoken_name_extraction",
            )

    def _remember_spelled_name(self, name: str, wording: str) -> None:
        previous = self.controller.memory.get("candidate_name")
        changed = previous is not None and previous.value.casefold() != name.casefold()
        self.controller.remember_name(
            name,
            source_turn=self._turn_number or None,
            supplied_wording=wording,
            confirmed=True,
            correction=changed,
            superseded_wording=previous.value if changed else None,
        )
        if changed:
            self._record(
                "correction_applied",
                speaker=None,
                field="candidate_name",
                superseded_wording=previous.value,
                supplied_wording=wording,
                source_turn=self._turn_number,
                update_source="spoken_spelling",
            )
        self._record(
            "memory_updated",
            speaker=None,
            field="candidate_name",
            value=self.controller.preferred_name,
            confirmed=True,
            source_turn=self._turn_number,
            update_source="spoken_spelling",
        )

    async def _route_spoken_name_spelling(self, answer: str) -> str | None:
        prompted = self.controller.stage is Stage.AWAITING_NAME or (
            self.controller.interrupted_utterance is not None
            and self.controller.interrupted_utterance.kind is UtteranceKind.NAME_PROMPT
        )
        spelling = _spoken_name_spelling(answer, prompted=prompted)
        if spelling is None:
            return None
        name, remainder = spelling
        self._remember_spelled_name(name, answer)
        if prompted and self.controller.stage is Stage.DELIVERING:
            self.controller.accept_barge_in_answer(answer)
            self._interrupted_output_id = None
        if self.controller.stage is Stage.AWAITING_GREETING:
            return None  # Preserve the full greeting as the candidate's original turn.
        if self.controller.stage is Stage.AWAITING_NAME:
            await self._queue_speech(
                self.controller.submit_name(name, _classify_greeting_readiness(remainder))
            )
            return ""
        if remainder:
            return remainder
        if self.controller.stage is Stage.AWAITING_READINESS:
            await self._queue_speech(self.controller.acknowledge_spelled_name(name))
            return ""
        await self._queue_speech(self._acknowledge_correction(f"Thanks, I'll call you {name}."))
        return ""

    def _acknowledge_correction(self, text: str) -> Utterance:
        interrupted = self.controller.interrupted_utterance
        acknowledgement = self.controller.acknowledge_correction(
            text,
            discard_interrupted=self._assigned_plan is not None,
            refund_probe=(
                interrupted is not None and self._interrupted_output_id != interrupted.id
            ),
        )
        self._interrupted_output_id = None
        return acknowledgement

    def _correction_field(self, request: _CorrectionRequest) -> FactField | None:
        old = request.superseded
        matching = [
            field_name
            for field_name, fact in self.controller.memory.facts.items()
            if re.search(rf"(?<!\w){re.escape(old)}(?!\w)", fact.value, re.I)
        ]
        if request.cue == "i used":
            return "technologies"
        if request.cue == "my name is" or "candidate_name" in matching:
            return "candidate_name"
        if len(matching) == 1:
            return matching[0]
        if request.cue in {"i said", "i meant"} and not matching:
            return "technical_terms"
        if request.cue in {"it's", "it is"} and self.controller.stage in {
            Stage.AWAITING_GREETING,
            Stage.AWAITING_NAME,
            Stage.AWAITING_READINESS,
            Stage.AWAITING_OPENING_ACK,
        }:
            return "candidate_name"
        return None

    async def _route_correction(self, answer: str) -> str | None:
        request = _parse_correction(answer)
        field_name: FactField | None = None
        if self._pending_correction is not None:
            pending = self._pending_correction
            self._pending_correction = None
            if request is None:
                label = re.sub(r"[^a-z ]", " ", answer.lower())
                if "name" in label:
                    field_name = "candidate_name"
                elif "technology" in label or "tool" in label:
                    field_name = "technologies"
                elif "term" in label:
                    field_name = "technical_terms"
                request = pending if field_name else None
            elif request == pending:
                self._record(
                    "correction_target_unresolved", speaker=None, source_turn=self._turn_number
                )
                await self._queue_speech(
                    self._acknowledge_correction(
                        "I couldn't place that correction. You can tell me the detail again."
                    )
                )
                return ""
        if request is None:
            return None
        field_name = field_name or self._correction_field(request)
        if field_name is None:
            self._pending_correction = request
            self._record("correction_needs_target", speaker=None, source_turn=self._turn_number)
            await self._queue_speech(
                self._acknowledge_correction(
                    "Which detail should I correct—your name or a project term?"
                )
            )
            return ""
        previous = self.controller.memory.get(field_name)
        replacement = request.replacement
        if field_name == "candidate_name":
            replacement = re.sub(r"^spelled\s+", "", replacement, flags=re.I)
            replacement = _letters_as_name(replacement, allow_word=True) or replacement
        if previous and field_name != "candidate_name":
            replacement = re.sub(
                rf"(?<!\w){re.escape(request.superseded)}(?!\w)",
                lambda _match: request.replacement,
                previous.value,
                flags=re.I,
            )
        try:
            if field_name == "candidate_name":
                self.controller.remember_name(
                    replacement,
                    source_turn=self._turn_number,
                    supplied_wording=answer,
                    confirmed=True,
                    correction=True,
                    superseded_wording=request.superseded,
                )
            else:
                self.controller.memory.supply(
                    field_name,
                    replacement,
                    source_turn=self._turn_number,
                    supplied_wording=answer,
                    confirmed=True,
                    correction=True,
                    superseded_wording=request.superseded,
                )
        except ValueError:
            self._record(
                "correction_rejected",
                speaker=None,
                field=field_name,
                source_turn=self._turn_number,
                reason="invalid_value",
            )
            return ""
        self._record(
            "correction_applied",
            speaker=None,
            field=field_name,
            superseded_wording=request.superseded,
            supplied_wording=request.replacement,
            source_turn=self._turn_number,
        )
        self._record(
            "memory_updated",
            speaker=None,
            field=field_name,
            value=replacement,
            confirmed=True,
            source_turn=self._turn_number,
            update_source="spoken_correction",
        )
        for related_field, related_fact in list(self.controller.memory.facts.items()):
            if related_field in {field_name, "candidate_name", "project_name"}:
                continue
            corrected_value = re.sub(
                rf"(?<!\w){re.escape(request.superseded)}(?!\w)",
                lambda _match: request.replacement,
                related_fact.value,
                flags=re.I,
            )
            if corrected_value == related_fact.value:
                continue
            self.controller.memory.supply(
                related_field,
                corrected_value,
                source_turn=self._turn_number,
                supplied_wording=answer,
                confirmed=True,
                correction=True,
                superseded_wording=request.superseded,
            )
            self._record(
                "memory_updated",
                speaker=None,
                field=related_field,
                value=corrected_value,
                confirmed=True,
                source_turn=self._turn_number,
                update_source="spoken_correction",
            )
        if request.remainder:
            return request.remainder
        acknowledgement = (
            f"Thanks, I'll call you {replacement}."
            if field_name == "candidate_name"
            else "Thanks, I've corrected that."
        )
        await self._queue_speech(self._acknowledge_correction(acknowledgement))
        return ""

    def turn_signal(self, signal: str, **details: Any) -> None:
        if signal == "vad_stopped":
            self._marks["vad_stop"] = self._now()
        if not self._closed:
            self._record(signal, speaker="candidate", turn_number=self._turn_number, **details)

    def transcript_segment(
        self,
        text: str,
        *,
        frame_timestamp: str,
        finalized: bool,
        frame_id: int,
        correlation: str = "active_turn",
        boundary_frame_id: int | None = None,
        **provider_details: Any,
    ) -> None:
        if not self._closed:
            self._marks["last_stt"] = self._now()
            self._record(
                "stt_final_segment",
                speaker="candidate",
                text=text,
                # As in earlier logs, a pre-start segment is numbered with the next turn.
                turn_number=self._turn_number + int(correlation == "pre_start_segment"),
                turn_correlation=correlation,
                boundary_frame_id=boundary_frame_id,
                frame_timestamp=frame_timestamp,
                finalized=finalized,
                frame_id=frame_id,
                **provider_details,
            )

    async def late_transcript(self, text: str, *, boundary_frame_id: int) -> None:
        """A transcript created before a boundary reached us after the turn was already routed.

        It is the candidate's speech from that turn, so it is processed now as a continuation
        rather than waiting for an unrelated later boundary.
        """
        if self._closed or self._media_paused:
            return
        self._record(
            "late_transcript_continuation", speaker=None, boundary_frame_id=boundary_frame_id
        )
        await self.user_started()
        await self.user_stopped(text, segment_count=1, boundary_frame_id=boundary_frame_id)

    def _is_finish_request(self, text: str) -> bool:
        """A clear request to end. Assigned interviews use the shared control classifier, where
        ending outranks feedback or anything else in the same turn; "I'm done" alone is ambiguous
        and is clarified instead."""
        if self._assigned_plan is not None:
            return interpret(text).kind == "finish"
        return _is_stop_request(text)

    async def detect_stop_from_segments(self, text: str) -> bool:
        if (
            self._closed
            or self._media_paused
            or self._stop_latched
            or self.controller.stage
            in {
                Stage.COMPLETE,
                Stage.ENDED_EARLY,
                Stage.EARLY_CLOSING,
            }
        ):
            return False
        if not self._is_finish_request(text):
            return False
        await self._end_early(text, defer_closing=True)
        return True

    async def start(self) -> None:
        if self._closed or self._media_paused or self.controller.stage is not Stage.NEW:
            return
        self._clock.start()
        self._record("session_started", speaker=None, build_id=self._build_id)
        await self._queue_speech(self.controller.start(self._greeting if self._opening else None))

    async def _queue_speech(self, utterance: Utterance) -> None:
        if (
            self._closed
            or self._media_paused
            or self.controller.pending != utterance
            or (self._stop_latched and utterance.kind is not UtteranceKind.EARLY_FEEDBACK)
        ):
            return
        if (
            utterance.kind is UtteranceKind.QUESTION
            and self._opening_seconds is None
            and self._opening
        ):
            # The opening ends when question one is first queued; it counts as active time.
            self._opening_seconds = round(self._clock.active_seconds())
        self._active_prompt_id = utterance.id
        self._mark("prepared", first=True)
        self._record(
            "agent_prepared",
            speaker="agent",
            utterance_id=utterance.id,
            text=utterance.text,
            interrupted=False,
            question_number=utterance.question_number,
            utterance_kind=utterance.kind.name.lower(),
            generation=self._generation,
            spoken=False,
            browser_playback_onset_ms=None,
        )
        self._record(
            "tts_requested", speaker=None, utterance_id=utterance.id, generation=self._generation
        )
        try:
            await self._speak(utterance)
        except Exception as exc:
            self._record(
                "response_error",
                speaker=None,
                error_type=type(exc).__name__,
                failure_stage="speech_queue",
                utterance_id=utterance.id,
            )

    async def user_started(self) -> None:
        if (
            self._closed
            or self._media_paused
            or self.controller.stage in {Stage.COMPLETE, Stage.ENDED_EARLY}
        ):
            return
        self._cancel_opening_timer()
        self._marks.clear()
        self._generation += 1
        task = self._response_task
        if task and not task.done():
            self._record(
                "inference_cancel_requested", speaker=None, generation=self._generation - 1
            )
            task.cancel()
        self._response_task = None
        pending = self.controller.pending
        if pending is not None and self.controller.interrupt(pending.id):
            output_started = pending.id in self._output_started_ids
            started_at = self._output_started_at.get(pending.id)
            heard_secs = (self._now() - started_at) if (output_started and started_at) else 0.0
            # The assigned interview needs a minimum amount of audio before speech counts as a
            # reply to it. Historical practice sessions keep "audio started means heard".
            heard_enough = output_started and (
                self._assigned_plan is None
                or pending.kind in _OPENING_KINDS
                or self._heard_enough(pending, heard_secs)
            )
            # "Heard" drives how the reply is routed; the stop debt tracks real audio output.
            self._interrupted_output_id = pending.id if heard_enough else None
            if output_started:
                self._cancelled_output_stop_debt += 1
            self._speaking_id = None
            self._tts_contexts = {
                context: identifier
                for context, identifier in self._tts_contexts.items()
                if identifier != pending.id
            }
            self._record(
                "agent_interrupted",
                speaker="agent",
                utterance_id=pending.id,
                text=pending.text,
                interrupted=True,
                output_started=output_started,
                heard_ms=round(heard_secs * 1000),
                heard_enough=heard_enough,
                cancellation_reason="candidate_speech_onset",
                generation=self._generation - 1,
            )
            self._record(
                "agent_cancellation",
                speaker=None,
                utterance_id=pending.id,
                generation=self._generation - 1,
            )
        self._turn_number += 1
        self._turn_started_at = time.perf_counter()
        self._record(
            "candidate_started",
            speaker="candidate",
            turn_number=self._turn_number,
            question_number=self.controller.current_question_number,
        )

    async def user_stopped(
        self,
        text: str,
        *,
        segment_count: int | None = None,
        boundary_frame_id: int | None = None,
    ) -> None:
        if (
            self._closed
            or self._media_paused
            or self.controller.stage in {Stage.COMPLETE, Stage.ENDED_EARLY}
        ):
            return
        answer = text.strip()
        self._last_turn_end = time.perf_counter()
        self._mark("turn_complete")
        continuing = self.controller.stage in {
            Stage.AWAITING_OPENING_ACK,
            Stage.AWAITING_PROPOSAL,
            Stage.AWAITING_FEEDBACK,
        }
        if not continuing:
            self._answer_number += 1
        duration = (
            round((self._last_turn_end - self._turn_started_at) * 1000, 1)
            if self._turn_started_at is not None
            else None
        )
        self._record(
            "candidate_final",
            speaker="candidate",
            text=answer,
            turn_number=self._turn_number,
            answer_number=self._answer_number,
            question_number=self.controller.current_question_number,
            prompt_utterance_id=self._active_prompt_id,
            segment_count=segment_count,
            turn_duration_ms=duration,
            boundary="Pipecat UserStoppedSpeakingFrame",
            boundary_frame_id=boundary_frame_id,
            configured_pause_secs=self._turn_pause_secs,
            speech_end_estimate="Pipecat VAD stop before speech-timeout boundary",
        )
        self._record(
            "candidate_turn_completed",
            speaker="candidate",
            turn_number=self._turn_number,
            boundary_frame_id=boundary_frame_id,
            boundary_method="Pipecat SpeechTimeoutUserTurnStopStrategy",
            configured_pause_secs=self._turn_pause_secs,
        )
        self._turn_started_at = None
        if not self._stop_latched and self._is_finish_request(answer):
            await self._end_early(answer)
            return

        if (
            self.controller.interrupted_utterance is not None
            and self.controller.interrupted_utterance.kind is UtteranceKind.CORRECTION_ACK
        ):
            if not answer or _is_sentence_request(_REPEAT, answer):
                await self._queue_speech(self.controller.retry_interrupted())
                return
            self.controller.accept_interrupted_correction_ack()
            resumed = self.controller.resume_after_correction()
            if resumed is not None:
                await self._queue_speech(resumed)
                return

        if not self._stop_latched:
            spelled = await self._route_spoken_name_spelling(answer)
            if spelled == "":
                return
            if spelled is not None:
                answer = spelled
            corrected = await self._route_correction(answer)
            if corrected == "":
                return
            if corrected is not None:
                answer = corrected
        self._latest_candidate_turn = answer

        if self._assigned_plan is not None:
            await self._route_assigned_turn(answer)
            return

        if self.controller.stage is Stage.AWAITING_CONTINUE_CHOICE:
            self._choice_turn = answer
            self._start_response()
            return
        if self._stop_latched:
            if self.controller.stage is Stage.EARLY_CLOSING:
                if self._end_reason == "recovery_limit":
                    await self._queue_speech(
                        self.controller.prepare_early_feedback(
                            "We'll pause the interview here. Thanks for sharing what you could."
                        )
                    )
                elif any(self.controller.answers):
                    self._start_response()
                else:
                    await self._queue_speech(
                        self.controller.prepare_early_feedback(
                            "We'll end here. Thanks for practicing."
                        )
                    )
            elif self.controller.interrupted_utterance is not None:
                await self._queue_speech(self.controller.retry_interrupted())
            return
        if (
            self._continued_prompt_id is not None
            and _explicit_continuation_choice(answer) == "continue"
        ):
            interrupted = self.controller.interrupted_utterance
            if interrupted is not None and interrupted.id == self._continued_prompt_id:
                retry = self.controller.retry_interrupted()
                self._continued_prompt_id = retry.id
                self._record("duplicate_continuation_ignored", speaker="candidate")
                await self._queue_speech(retry)
                return
            if (
                self.controller.stage is Stage.AWAITING_ANSWER
                and self._last_project_prompt is not None
                and self._last_project_prompt.id == self._continued_prompt_id
            ):
                self._record("duplicate_continuation_ignored", speaker="candidate")
                return
        self._continued_prompt_id = None
        if _explicit_continuation_choice(answer) == "continue":
            interrupted = self.controller.interrupted_utterance
            if interrupted is not None and interrupted.kind in {
                UtteranceKind.QUESTION,
                UtteranceKind.FOLLOW_UP,
                UtteranceKind.NUDGE,
                UtteranceKind.CLARIFICATION,
                UtteranceKind.DETOUR_RESUME,
            }:
                self._record("nonanswer_continuation", speaker="candidate")
                await self._queue_speech(self.controller.retry_interrupted())
                return
            if self.controller.stage is Stage.AWAITING_ANSWER and self._last_project_prompt:
                self._record("nonanswer_continuation", speaker="candidate")
                await self._queue_speech(
                    self.controller.restate_project_prompt(self._last_project_prompt)
                )
                return
        if await self._route_control_request(answer):
            return

        if await self._route_opening(answer):
            return

        if (
            self.controller.current_question_number == 4
            and self.controller.stage in {Stage.AWAITING_ANSWER, Stage.AWAITING_PROPOSAL}
            and _asks_for_question_scope(answer)
        ):
            try:
                explanation = self.controller.explain_current_question(
                    "In the project you described—what would you change if you built it again?"
                )
            except LimitReached:
                self._record("clarification_limit", speaker=None)
                return
            self._record("question_scope_requested", speaker="candidate", text=answer)
            await self._queue_speech(explanation)
            return

        interrupted = self.controller.interrupted_utterance
        if interrupted is not None and interrupted.kind in {
            UtteranceKind.DETOUR_ACK,
            UtteranceKind.PROVISIONAL_FEEDBACK,
            UtteranceKind.TOPIC_BOUNDARY,
        }:
            if self._interrupted_output_id != interrupted.id:
                await self._queue_speech(self.controller.retry_interrupted())
                return
            self.controller.accept_barge_in_answer(answer)
            self._interrupted_output_id = None
            if self.controller.stage is Stage.AWAITING_CONTINUE_CHOICE:
                self._choice_turn = answer
                self._start_response()
            elif self.controller.stage is Stage.AWAITING_DETOUR_RETURN:
                utterance = self.controller.resume_detour()
                if utterance:
                    await self._queue_speech(utterance)
                else:
                    self._latest_candidate_turn = self.controller.answers[
                        self.controller.current_question_number - 1
                    ][-1]
                    self._start_response()
            return

        if (
            interrupted is not None
            and interrupted.kind
            in {
                UtteranceKind.QUESTION,
                UtteranceKind.FOLLOW_UP,
                UtteranceKind.NUDGE,
                UtteranceKind.CLARIFICATION,
                UtteranceKind.REPEAT,
                UtteranceKind.DETOUR_RESUME,
            }
            and answer
        ):
            self._interrupted_candidate_turn = answer
            self._start_response()
            return

        if (
            answer
            and _asks_about_name(answer)
            and self.controller.stage in {Stage.AWAITING_ANSWER, Stage.AWAITING_PROPOSAL}
        ):
            self._record("candidate_detour", speaker="candidate", kind="name", text=answer)
            resume_prompt = (
                self._last_project_prompt
                if self.controller.stage is Stage.AWAITING_ANSWER
                else None
            )
            await self._queue_speech(self.controller.start_name_detour(resume_prompt))
            return

        if self.controller.stage is Stage.AWAITING_DETOUR_RETURN:
            utterance = self.controller.resume_detour()
            if utterance:
                await self._queue_speech(utterance)
            else:
                self._latest_candidate_turn = self.controller.answers[
                    self.controller.current_question_number - 1
                ][-1]
                self._start_response()
            return

        if (
            _is_sentence_request(_SKIP, answer)
            and self.controller.interrupted_utterance is not None
        ):
            try:
                utterance = self.controller.skip_interrupted()
            except ValueError:
                return
            self._record("candidate_skipped_question", speaker="candidate")
            if utterance is not None:
                await self._queue_speech(utterance)
            else:
                self._start_response()
            return

        interrupted = self.controller.interrupted_utterance
        if interrupted is not None:
            if interrupted.kind.name == "EARLY_FEEDBACK":
                await self._queue_speech(self.controller.retry_interrupted())
                return
            if not answer or _is_sentence_request(_REPEAT, answer):
                await self._queue_speech(self.controller.retry_interrupted())
                return
            if self._interrupted_output_id != interrupted.id:
                self._record(
                    "unheard_prompt_recovery",
                    speaker=None,
                    utterance_id=interrupted.id,
                    candidate_text=answer,
                )
                if interrupted.kind.name == "QUESTION":
                    retry = self.controller.resolve_unheard_question(answer)
                    await self._queue_speech(retry)
                    return
                elif interrupted.kind.name in {"FOLLOW_UP", "NUDGE", "CLARIFICATION", "REPEAT"}:
                    await self._queue_speech(self.controller.resolve_unheard_follow_up(answer))
                    return
                else:
                    await self._queue_speech(self.controller.retry_interrupted())
                    return
            else:
                self.controller.accept_barge_in_answer(answer)
                self._record("barge_in_answer_accepted", speaker="candidate", text=answer)
            self._interrupted_output_id = None
        elif self.controller.stage is Stage.AWAITING_ANSWER:
            self._project_prompt_for_detour = self._last_project_prompt
            try:
                if _is_sentence_request(_SKIP, answer):
                    utterance = self.controller.skip_current()
                    self._record("candidate_skipped_question", speaker="candidate")
                elif _is_sentence_request(_REPEAT, answer):
                    utterance = self.controller.request_repeat(self._last_project_prompt)
                else:
                    self._answer_snapshot = self.controller.answers[
                        self.controller.current_question_number - 1
                    ]
                    self._latest_candidate_turn = answer
                    utterance = self.controller.submit_answer(answer)
            except LimitReached:
                self._record("clarification_limit", speaker=None)
                return
            if utterance is not None:
                await self._queue_speech(utterance)
                return
            if _is_sentence_request(_SKIP, answer):
                self._start_response()
                return
        elif self.controller.stage is Stage.AWAITING_PROPOSAL:
            self._project_prompt_for_detour = None
            if not answer:
                return
            self._answer_snapshot = self.controller.answers[
                self.controller.current_question_number - 1
            ]
            self._latest_candidate_turn = answer
            self.controller.add_answer_continuation(answer)
        elif self.controller.stage is Stage.AWAITING_FEEDBACK:
            if not answer:
                return
            self.controller.add_closing_remarks(answer)
        elif self.controller.stage in {
            Stage.AWAITING_NO_PROJECT_EXAMPLE,
            Stage.AWAITING_NO_PROJECT_CHOICE,
        }:
            if not answer:
                self._record("no_project_waiting", speaker=None)
                return
            self._no_project_turn = answer
            self._latest_candidate_turn = answer
        else:
            return

        self._start_response()

    async def _route_control_request(self, answer: str) -> bool:
        """Apply explicit controls before opening gates or answer collection."""
        if _is_sentence_request(_REPEAT, answer):
            interrupted = self.controller.interrupted_utterance
            if interrupted is not None:
                self._record(
                    "candidate_repeat_requested", speaker="candidate", prompt_id=interrupted.id
                )
                await self._queue_speech(self.controller.retry_interrupted())
                return True
            if self.controller.stage in {Stage.AWAITING_ANSWER, Stage.AWAITING_PROPOSAL}:
                try:
                    utterance = self.controller.request_repeat(self._last_project_prompt)
                except LimitReached:
                    self._record("clarification_limit", speaker=None)
                    return True
                self._record(
                    "candidate_repeat_requested", speaker="candidate", prompt_id=utterance.id
                )
                await self._queue_speech(utterance)
                return True
            if self.controller.stage in {
                Stage.AWAITING_NO_PROJECT_EXAMPLE,
                Stage.AWAITING_NO_PROJECT_CHOICE,
            }:
                try:
                    utterance = self.controller.repeat_no_project_prompt()
                except LimitReached:
                    self._record("no_project_repeat_limit", speaker=None)
                    return True
                self._record(
                    "candidate_repeat_requested", speaker="candidate", prompt_id=utterance.id
                )
                await self._queue_speech(utterance)
                return True
        if _is_sentence_request(_SKIP, answer):
            interrupted = self.controller.interrupted_utterance
            try:
                if interrupted is not None and interrupted.question_number is not None:
                    utterance = self.controller.skip_interrupted()
                elif self.controller.stage is Stage.AWAITING_ANSWER:
                    utterance = self.controller.skip_current()
                else:
                    return False
            except InvalidTransition:
                if self.controller.current_question_number != 1:
                    raise
                self._record("project_example_required", speaker=None, request="skip")
                await self._queue_speech(self.controller.offer_project_example())
                return True
            self._record("candidate_skipped_question", speaker="candidate")
            if utterance is None:
                self._start_response()
            else:
                await self._queue_speech(utterance)
            return True
        return False

    async def _route_assigned_turn(self, answer: str) -> None:
        """Route one completed turn against the assigned, delivery-aware plan."""
        closing = self.controller.interrupted_utterance
        if closing is not None and closing.kind in {
            UtteranceKind.CLOSING,
            UtteranceKind.EARLY_FEEDBACK,
        }:
            await self._finish_interrupted_closing(closing)
            return
        if self._stop_latched:
            if self.controller.stage is Stage.EARLY_CLOSING:
                await self._queue_speech(
                    self.controller.prepare_early_feedback(self._early_closing_text())
                )
            return
        interrupted = self.controller.interrupted_utterance
        if self.controller.stage in {Stage.AWAITING_GREETING, Stage.AWAITING_OPENING_ACK} or (
            interrupted is not None and interrupted.kind in _OPENING_KINDS
        ):
            await self._route_assigned_opening(answer, interrupted)
            return
        if self._finish_clarified:
            # The last thing said was "end the interview, or finished with this question?"
            self._finish_clarified = False
            if _AFFIRM.fullmatch(answer.strip()):
                await self._end_early(answer)
                return
        intent = interpret(answer)
        if intent.is_control and await self._route_assigned_control(intent, answer, interrupted):
            return
        if _is_sentence_request(_SKIP, answer):
            self._apply_time_budget()
            try:
                next_prompt = (
                    self.controller.skip_interrupted()
                    if interrupted
                    else self.controller.skip_current()
                )
            except InvalidTransition:
                self._record("required_question_skipped", speaker="candidate")
                await self._control_reply(
                    "This question is part of the interview. "
                    "If you prefer not to answer, you can say so."
                )
                return
            self._record("candidate_skipped_question", speaker="candidate")
            if next_prompt:
                await self._queue_speech(next_prompt)
            else:
                self._start_response()
            return
        if interrupted:
            if not answer:
                await self._queue_speech(self.controller.retry_interrupted())
                return
            if self._interrupted_output_id != interrupted.id:
                if _FLOOR_CONTROL.fullmatch(answer.strip()):
                    # "Go on" / "yes please cut" over a prompt that was barely heard: replay it.
                    # These words are neither an answer nor a reason to spend a probe.
                    self._record("floor_control_replay", speaker=None, utterance_id=interrupted.id)
                    await self._queue_speech(self.controller.retry_interrupted())
                    return
                if interrupted.kind is UtteranceKind.QUESTION:
                    await self._queue_speech(self.controller.resolve_unheard_question(answer))
                elif interrupted.kind in {
                    UtteranceKind.FOLLOW_UP,
                    UtteranceKind.NUDGE,
                    UtteranceKind.CLARIFICATION,
                    UtteranceKind.REPEAT,
                }:
                    await self._queue_speech(self.controller.resolve_unheard_follow_up(answer))
                else:
                    await self._queue_speech(self.controller.retry_interrupted())
                self._record("unheard_prompt_recovery", speaker=None, utterance_id=interrupted.id)
                return
            self.controller.accept_barge_in_answer(answer)
            self._interrupted_output_id = None
            self._record("barge_in_answer_accepted", speaker="candidate", text=answer)
            self._start_response()
            return
        if self.controller.stage is Stage.AWAITING_ANSWER:
            if not answer:
                try:
                    await self._queue_speech(self.controller.submit_answer(""))
                except LimitReached:
                    self._record("clarification_limit", speaker=None)
                return
            self._latest_candidate_turn = answer
            self._answer_snapshot = self.controller.answers[
                self.controller.current_question_number - 1
            ]
            self.controller.submit_answer(answer)
            self._start_response()
        elif self.controller.stage is Stage.AWAITING_PROPOSAL:
            if answer:
                self._answer_snapshot = self.controller.answers[
                    self.controller.current_question_number - 1
                ]
                self.controller.add_answer_continuation(answer)
                self._latest_candidate_turn = answer
                self._start_response()

    # --- Assigned-interview controls ---------------------------------------------------

    async def _control_reply(self, text: str) -> None:
        """A short reply to a control or detour. The pending prompt is untouched and returns.

        After several replies in a row with no answer, the interviewer stops answering detours and
        returns to the prompt itself, so the conversation cannot loop on side requests.
        """
        if self.controller.note_control_detour():
            self._record("control_detour_limit", speaker=None)
            await self._repeat_pending(count_detour=False)
            return
        await self._queue_speech(self.controller.prepare_control_reply(text))

    async def _repeat_pending(self, *, count_detour: bool = True) -> None:
        """Repeat the authoritative pending prompt; say so plainly when the allowance is used."""
        utterance = self.controller.repeat_pending()
        if utterance is None:
            self._record("repeat_limit_reached", speaker=None)
            await self._queue_speech(self.controller.prepare_control_reply(REPEAT_EXHAUSTED))
            return
        self._record("repeat_requested", speaker="candidate", utterance_id=utterance.id)
        await self._queue_speech(utterance)

    def _progress_reply(self) -> str:
        plan = self._assigned_plan or {}
        total = len(plan.get("questions", []))
        number = (
            self.controller.pending_prompt.question_number
            if self.controller.pending_prompt
            else None
        )
        minutes = plan.get("target_minutes")
        facts = f"There are {total} questions in this interview"
        if number:
            facts += f", and this is number {number}"
        if minutes:
            facts += f". About {minutes} minutes are planned"
        return f"{facts}. I can repeat the question whenever you like."

    async def _route_assigned_control(
        self, intent: Intent, answer: str, interrupted: Utterance | None
    ) -> bool:
        """Handle a clear control. Returns False when the turn should be treated as speech.

        A control is never an answer: it does not spend a probe, mark coverage or advance. The one
        exception is "I don't know" and its relatives, which *are* the candidate's answer.
        """
        controller = self.controller
        prompt = controller.pending_prompt
        if prompt is None:
            return False
        heard = controller.pending_prompt_delivered or (
            interrupted is not None
            and interrupted.id == prompt.id
            and self._interrupted_output_id == interrupted.id
        )
        kind = intent.kind
        self._record(
            "control_recognised", speaker="candidate", control=kind, gap=intent.gap or None
        )
        if kind == "finish":
            await self._end_early(answer)
        elif kind == "finish_unclear":
            self._finish_clarified = True
            await self._control_reply(FINISH_CLARIFICATION)
        elif kind in {"restart", "back"}:
            self._record("restart_declined", speaker=None, control=kind)
            await self._control_reply(RESTART_UNAVAILABLE)
        elif kind == "repeat" or (kind == "not_understood") or (kind == "gap" and not heard):
            # An unheard prompt cannot be answered or declined; and "I don't understand" is a
            # request to hear it again, not an admission that the candidate does not know.
            await self._repeat_pending()
        elif kind == "feedback":
            self._record("feedback_request_declined", speaker="candidate")
            await self._control_reply(FEEDBACK_DECLINED)
        elif kind == "outcome":
            await self._control_reply(OUTCOME_UNKNOWN)
        elif kind == "progress":
            await self._control_reply(self._progress_reply())
        elif kind == "gap":
            await self._record_gap_and_advance(intent, answer)
        else:
            return False
        return True

    async def _record_gap_and_advance(self, intent: Intent, answer: str) -> None:
        """ "I don't know", "I haven't used it" and refusals are answers: keep them, move on.

        The whole turn is stored as spoken, so a mixed answer ("I haven't used Kafka, but I used
        Airflow...") keeps its useful part and still says nothing about Kafka. Nothing asks again
        for the same detail.
        """
        self.controller.record_gap_answer(answer)
        self._interrupted_output_id = None
        self._latest_candidate_turn = answer
        self._record(
            "candidate_gap_recorded",
            speaker="candidate",
            gap=intent.gap,
            subject=intent.subject or None,
            mixed=len(intent.residual.split()) >= 3,
        )
        self._apply_time_budget()
        following = self.controller.propose(ReplyKind.ADVANCE)
        if following:
            await self._queue_speech(following)
        else:
            self._start_response()

    # --- Assigned-interview context, budget and response -------------------------------

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _remaining_for_budget(self) -> list[RemainingQuestion]:
        plan = self._assigned_plan
        if not plan:
            return []
        current = (self.controller.current_question_number or 0) - 1
        covered = self.controller.covered_source_turns
        skipped = set(self.controller.time_skipped_numbers)
        return [
            RemainingQuestion(
                index, bool(item.get("required", True)), self._question_seconds[index]
            )
            for index, item in enumerate(plan["questions"])
            if index > current and covered[index] is None and (index + 1) not in skipped
        ]

    def _time_pressure(self) -> str:
        if not self._assigned_plan or not self._target_seconds:
            return "normal"
        return pressure(
            self._clock.active_seconds(), self._target_seconds, self._remaining_for_budget()
        )

    def _apply_time_budget(self) -> str:
        """Drop unaffordable questions at a turn boundary and return the time pressure."""
        if not self._assigned_plan or not self._target_seconds:
            return "normal"
        active = self._clock.active_seconds()
        remaining = self._remaining_for_budget()
        state = pressure(active, self._target_seconds, remaining)
        dropped = skip_for_time(active, self._target_seconds, remaining)
        if dropped:
            self.controller.mark_time_skipped(dropped)
            self._record(
                "questions_skipped_for_time",
                speaker=None,
                question_numbers=[index + 1 for index in dropped],
                time_pressure=state,
                active_seconds=round(active),
                target_seconds=self._target_seconds,
            )
        if state == "exhausted":
            self._ended_for_time = True
        return state

    def _record_coverage(self, end_reason: str) -> None:
        """Persist, once, an honest number-only account of what was reached."""
        if self._coverage_recorded or not self._assigned_plan:
            return
        self._coverage_recorded = True
        self._record(
            "coverage_summary",
            speaker=None,
            summary={
                **self.controller.coverage_snapshot(),
                "end_reason": end_reason,
                "active_seconds": round(self._clock.active_seconds()),
                "paused_seconds": round(self._clock.paused_seconds),
                "target_seconds": self._target_seconds,
                "ended_for_time": self._ended_for_time,
                "opening_seconds": self._opening_seconds,
            },
        )

    def _assigned_payload(self) -> dict[str, Any]:
        """The bounded context a single assigned decision needs.

        Deliberately excluded: the full plan, future question text and guidance,
        grounding evidence and anything the admin supplied as private instructions.
        Future questions appear only as short topic labels so earlier coverage can be
        recognised. Candidate speech and memory remain untrusted data.
        """
        plan = self._assigned_plan or {}
        questions = plan.get("questions", [])
        number = self.controller.current_question_number
        answers = self.controller.answers
        covered = self.controller.covered_source_turns
        skipped = set(self.controller.time_skipped_numbers)
        context: dict[str, Any] = {
            "title": plan.get("title", ""),
            "purpose": plan.get("purpose", ""),
            "interviewer_style": plan.get("interviewer_style", ""),
        }
        current = None
        if number:
            item = questions[number - 1]
            current = {
                "question_number": number,
                "of": len(questions),
                "topic": item["topic"],
                "question": item["question"],
                "follow_up_guidance": item.get("follow_up_guidance", ""),
                "required": bool(item.get("required", True)),
            }
        latest = self._latest_candidate_turn
        before = list(self._answer_snapshot or ())
        return {
            "phase": "interview",
            "interview_context": context,
            "candidate_focus_areas": list(plan.get("candidate_focus_areas", []))[:8],
            "current_question": current,
            "remaining_topics": [
                {"question_number": n, "topic": item["topic"]}
                for n, item in enumerate(questions, 1)
                if number is not None and n > number and covered[n - 1] is None and n not in skipped
            ],
            "prior_answers": [
                {
                    "question_number": n,
                    "topic": questions[n - 1]["topic"],
                    "answer": self._clip(" ".join(answers[n - 1]), 500),
                }
                for n in range(1, len(questions) + 1)
                if n != number and answers[n - 1]
            ],
            "current_question_answer_before_latest_turn": self._clip(" ".join(before), 1200),
            "latest_candidate_turn": self._clip(latest, 1200),
            "session_memory": self.controller.memory.for_model(),
            "correction_overlays": self.controller.memory.corrections_for_model(),
            "last_delivered_prompt": (
                self._last_delivered_prompt.text if self._last_delivered_prompt else None
            ),
            "interrupted_prompt": (
                self.controller.interrupted_utterance.text
                if self.controller.interrupted_utterance
                else None
            ),
            "remaining_follow_ups": self.controller.remaining_follow_ups,
            "time_pressure": self._time_pressure(),
            "ended_for_time": self._ended_for_time,
        }

    async def _respond_assigned(self, generation: int) -> None:
        if self._closed or self._media_paused or generation != self._generation:
            return
        if self.controller.stage is Stage.AWAITING_OPENING_ACK:
            await self._respond_opening_ack(generation)
            return
        if self.controller.stage is Stage.AWAITING_FEEDBACK:
            # This stage is the terminal closing gate. The closing is fixed text: brief, neutral and
            # free of promises, so no model can add feedback, an outcome or a contact.
            self._record("closing_prepared", speaker=None, generation=generation, fixed_text=True)
            if generation == self._generation and not self._closed:
                await self._queue_speech(self.controller.prepare_closing(self._closing_text()))
            return
        if self.controller.stage is not Stage.AWAITING_PROPOSAL:
            return
        if not self._latest_candidate_turn:
            return
        state = self._apply_time_budget()
        if state == "exhausted":
            # The candidate's turn has finished; nothing is cut off. Move to the closing.
            self._record(
                "inference_skipped_budget_exhausted",
                speaker=None,
                generation=generation,
                active_seconds=round(self._clock.active_seconds()),
                target_seconds=self._target_seconds,
            )
            next_prompt = self.controller.propose(ReplyKind.ADVANCE)
            if next_prompt:
                await self._queue_speech(next_prompt)
            else:
                self._start_response()
            return
        self._record(
            "inference_started",
            speaker=None,
            generation=generation,
            purpose="assigned_decision",
            llm_first_token_ms=None,
        )
        try:
            self._mark("decision_start")
            result = await self._infer("assigned_decision", self._assigned_payload())
            self._mark("decision_done")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record(
                "response_error",
                speaker=None,
                generation=generation,
                error_type=type(exc).__name__,
                failure_stage="assigned_decision",
            )
            return
        if generation != self._generation or self._closed or self._media_paused:
            self._record("inference_stale_discarded", speaker=None, generation=generation)
            return
        action = str(result.get("action", "")).lower()
        speech = result.get("text", "")
        # Internal record only. It is not marked validated until validation below succeeds.
        self._record(
            "decision_proposed",
            speaker=None,
            generation=generation,
            action=action,
            text=speech,
            validated=False,
        )

        def accept() -> None:
            self._record(
                "inference_completed",
                speaker=None,
                generation=generation,
                purpose="assigned_decision",
                validated=True,
                action=action,
            )

        if action in {"feedback_request", "off_topic", "coach_identity"}:
            if (
                self.controller.stage is Stage.AWAITING_PROPOSAL
                and self._answer_snapshot is not None
            ):
                self.controller.restore_answers(self._answer_snapshot)
            reply = {
                "feedback_request": FEEDBACK_DECLINED,
                "off_topic": OFF_TOPIC_REPLY,
                "coach_identity": "You can call me RehearseAI.",
            }[action]
            accept()
            await self._control_reply(reply)
            return
        if action == "wait":
            accept()
            self._record("awaiting_continuation", speaker=None, generation=generation)
            return
        if action in {"follow_up", "nudge"}:
            if state != "normal" or self.controller.remaining_follow_ups <= 0:
                self._record(
                    "decision_rejected",
                    speaker=None,
                    generation=generation,
                    reason="probe_budget" if state != "normal" else "probe_limit",
                    time_pressure=state,
                )
            elif (
                isinstance(speech, str)
                and speech.strip()
                and len(speech) <= 300
                and speech.count("?") <= 1
            ):
                accept()
                kind = ReplyKind.FOLLOW_UP if action == "follow_up" else ReplyKind.NUDGE
                await self._queue_speech(self.controller.propose(kind, speech))
                return
            else:
                self._record(
                    "decision_rejected",
                    speaker=None,
                    generation=generation,
                    reason="invalid_spoken_text",
                )
        elif action != "advance":
            self._record(
                "decision_rejected",
                speaker=None,
                generation=generation,
                reason="unknown_action",
            )
        if action == "advance":
            coverage = result.get("covered_future_questions", [])
            if isinstance(coverage, list):
                for item in coverage:
                    if not isinstance(item, dict):
                        continue
                    number = item.get("question_number")
                    quote = item.get("evidence_quote")
                    if (
                        type(number) is int
                        and isinstance(quote, str)
                        and self.controller.credit_covered_question(
                            number, quote, source_turn=self._turn_number
                        )
                    ):
                        self._record(
                            "question_covered_from_prior_turn",
                            speaker=None,
                            question_number=number,
                            source_turn=self._turn_number,
                            evidence_quote=quote,
                        )
            self._apply_time_budget()
            accept()
        else:
            # Python advances because the proposal was rejected; the model's text is unused.
            self._record(
                "decision_fallback_advance", speaker=None, generation=generation, validated=False
            )
        next_prompt = self.controller.propose(ReplyKind.ADVANCE)
        if next_prompt:
            await self._queue_speech(next_prompt)
        else:
            self._start_response()

    async def _route_opening(self, answer: str, *, allow_interpretation: bool = True) -> bool:
        interrupted = self.controller.interrupted_utterance
        opening_kinds = {
            UtteranceKind.GREETING,
            UtteranceKind.OPENING_ACK,
            UtteranceKind.NAME_PROMPT,
            UtteranceKind.NAME_ACK,
            UtteranceKind.OPENING_COACH_REPLY,
            UtteranceKind.READINESS,
            UtteranceKind.OPENING_WAIT,
            UtteranceKind.OPENING_CLARIFICATION,
        }
        if interrupted is not None and interrupted.kind in opening_kinds:
            if not answer or _is_sentence_request(_REPEAT, answer):
                await self._queue_speech(self.controller.retry_interrupted())
                return True
            if (
                interrupted.kind
                not in {
                    UtteranceKind.OPENING_WAIT,
                    UtteranceKind.OPENING_CLARIFICATION,
                }
                and not (
                    interrupted.kind is UtteranceKind.NAME_PROMPT
                    and (
                        _preferred_name(answer, prompted=True)
                        or _may_need_opening_interpretation(answer)
                    )
                )
                and not (
                    interrupted.kind is UtteranceKind.READINESS and _asks_coach_identity(answer)
                )
                and self._interrupted_output_id != interrupted.id
            ):
                self._record(
                    "unheard_prompt_recovery",
                    speaker=None,
                    utterance_id=interrupted.id,
                    candidate_text=answer,
                )
                await self._queue_speech(self.controller.retry_interrupted())
                return True
            self.controller.accept_barge_in_answer(answer)
            self._interrupted_output_id = None
            self._record("barge_in_answer_accepted", speaker="candidate", text=answer)

        if self.controller.stage is Stage.AWAITING_GREETING:
            try:
                utterance = (
                    self.controller.request_opening_repeat()
                    if not answer or _is_sentence_request(_REPEAT, answer)
                    else self.controller.submit_greeting(answer)
                )
            except LimitReached:
                self._record("opening_repeat_limit", speaker=None, opening_phase="greeting")
                return True
            if utterance is not None:
                await self._queue_speech(utterance)
            else:
                self._start_response()
            return True

        if self.controller.stage is Stage.AWAITING_QUESTION_START:
            await self._queue_speech(self.controller.begin_question_one())
            return True

        if self.controller.stage is Stage.AWAITING_OPENING_RETURN:
            utterance = (
                self.controller.start_after_opening_question()
                if _classify_readiness(answer) is Readiness.READY
                else self.controller.resume_opening_question()
            )
            await self._queue_speech(utterance)
            return True

        if (
            answer
            and self.controller.stage in {Stage.AWAITING_NAME, Stage.AWAITING_READINESS}
            and _asks_coach_identity(answer)
        ):
            name = _preferred_name(answer, prompted=self.controller.stage is Stage.AWAITING_NAME)
            if name:
                self._remember_spoken_name(name, answer)
            self._record(
                "candidate_detour",
                speaker="candidate",
                kind="coach_identity",
                preferred_name=name,
            )
            await self._queue_speech(
                self.controller.answer_opening_question("You can call me RehearseAI.", name)
            )
            return True

        if (
            allow_interpretation
            and answer
            and self.controller.stage in {Stage.AWAITING_NAME, Stage.AWAITING_READINESS}
            and _may_need_opening_interpretation(answer)
        ):
            self._opening_interpret_text = answer
            self._start_response()
            return True

        if self.controller.stage is Stage.AWAITING_NAME:
            if not answer or _is_sentence_request(_REPEAT, answer):
                await self._queue_speech(self.controller.retry_name_prompt())
                return True
            readiness = _classify_greeting_readiness(answer)
            declined = bool(_NAME_DECLINE.search(answer))
            name = None if declined else _preferred_name(answer, prompted=True)
            if not name and not declined and readiness is not Readiness.READY:
                self._record("name_unclear", speaker=None, source_turn=self._turn_number)
                await self._queue_speech(self.controller.clarify_name())
                return True
            if name:
                self._remember_spoken_name(name, answer)
            self._record(
                "name_response",
                speaker="candidate",
                preferred_name=name,
                declined=declined,
                readiness=readiness.name.lower(),
            )
            if readiness is Readiness.READY and name is None:
                utterance = self.controller.skip_name_for_start()
            else:
                utterance = self.controller.submit_name(name, readiness)
            await self._queue_speech(utterance)
            return True

        if self.controller.stage is Stage.AWAITING_OPENING_ACK:
            if _is_sentence_request(_REPEAT, answer):
                try:
                    await self._queue_speech(self.controller.request_opening_repeat())
                except LimitReached:
                    self._record("opening_repeat_limit", speaker=None, opening_phase="greeting")
                    self._start_response()
                return True
            if answer and interrupted is None:
                self.controller.add_greeting_continuation(answer)
            self._start_response()
            return True

        if self.controller.stage is Stage.AWAITING_READINESS:
            if not answer or _is_sentence_request(_REPEAT, answer):
                try:
                    await self._queue_speech(self.controller.request_opening_repeat())
                except LimitReached:
                    self._record("opening_repeat_limit", speaker=None, opening_phase="readiness")
                return True
            classification = _classify_readiness(answer)
            self._record(
                "readiness_response",
                speaker="candidate",
                classification=classification.name.lower(),
                ready=classification is Readiness.READY,
            )
            utterance = (
                self.controller.clarify_readiness()
                if classification is Readiness.UNCLEAR
                else self.controller.submit_readiness(classification is Readiness.READY)
            )
            if utterance is not None:
                await self._queue_speech(utterance)
            else:
                self._record(
                    "opening_waiting_for_readiness",
                    speaker=None,
                    classification=classification.name.lower(),
                    guidance="Say start when ready, not yet to wait, or end the interview to stop.",
                )
            return True

        return False

    async def _end_early(
        self, request: str, *, defer_closing: bool = False, reason: str = "user_request"
    ) -> None:
        if self._stop_latched:
            return
        self._stop_latched = True
        self._cancel_opening_timer()
        self._end_reason = reason
        self._generation += 1
        task = self._response_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._response_task = None
        pending = self.controller.pending
        if self.controller.end_early():
            self._speaking_id = None
            self._record(
                "early_end_requested",
                speaker="candidate" if reason == "user_request" else None,
                text=request,
                end_reason=reason,
                utterance_id=pending.id if pending else None,
                detection_source="final_stt_segments" if defer_closing else "completed_turn",
            )
            if pending:
                self._record(
                    "agent_cancellation",
                    speaker=None,
                    utterance_id=pending.id,
                    cancellation_reason="early_end",
                )
            # Pipecat already broadcasts interruption on UserStartedSpeakingFrame.
            # A second broadcast while processing an STT frame could reset the
            # queue before UserStoppedSpeakingFrame reaches this adapter.
            if self._cancel_audio and not defer_closing:
                await self._cancel_audio()
            if defer_closing:
                return
            if self._assigned_plan is not None:
                await self._queue_speech(
                    self.controller.prepare_early_feedback(self._early_closing_text())
                )
                return
            if reason == "recovery_limit":
                await self._queue_speech(
                    self.controller.prepare_early_feedback(
                        "We'll pause the interview here. Thanks for sharing what you could."
                    )
                )
            elif any(self.controller.answers):
                self._start_response()
            else:
                await self._queue_speech(
                    self.controller.prepare_early_feedback("We'll end here. Thanks for practicing.")
                )

    def _start_response(self) -> None:
        if self._media_paused or (
            self._stop_latched and self.controller.stage is not Stage.EARLY_CLOSING
        ):
            return
        self._generation += 1
        generation = self._generation
        self._response_task = asyncio.create_task(self._respond(generation))

    def _payload(self) -> dict[str, Any]:
        index = self.controller.current_question_number
        current_answers = list(self.controller.answers[index - 1]) if index else []
        answers_before_latest = [list(turns) for turns in self.controller.answers]
        if index and self._answer_snapshot is not None:
            answers_before_latest[index - 1] = list(self._answer_snapshot)
        phase = {
            Stage.AWAITING_NAME: "name",
            Stage.AWAITING_READINESS: "readiness",
            Stage.AWAITING_NO_PROJECT_EXAMPLE: "no_project_example",
            Stage.AWAITING_NO_PROJECT_CHOICE: "no_project_choice",
        }.get(self.controller.stage, "interview")
        return {
            "phase": phase,
            "current_question_number": self.controller.current_question_number,
            "current_question": self.controller.current_question,
            "greeting_response": self.controller.greeting_response,
            "preferred_name": self.controller.preferred_name,
            "session_memory": self.controller.memory.for_model(),
            "correction_overlays": self.controller.memory.corrections_for_model(),
            "project_established": self.controller.project_established,
            "answers_by_question": [list(turns) for turns in self.controller.answers],
            "covered_source_turns": list(self.controller.covered_source_turns),
            "answers_by_question_before_latest_turn": answers_before_latest,
            "current_question_answers": current_answers,
            "current_question_answer": " ".join(current_answers),
            "last_delivered_prompt": (
                self._last_delivered_prompt.text if self._last_delivered_prompt else None
            ),
            "last_delivered_prompt_id": (
                self._last_delivered_prompt.id if self._last_delivered_prompt else None
            ),
            "last_project_prompt": (
                self._last_project_prompt.text if self._last_project_prompt else None
            ),
            "interrupted_prompt": (
                self.controller.interrupted_utterance.text
                if self.controller.interrupted_utterance
                else None
            ),
            "closing_remarks": list(self.controller.closing_remarks),
            "max_extra_prompts_per_question": {
                "llm_project_probe_including_clarification": 1,
                "candidate_requested_repeat_or_explanation": 1,
            },
            "remaining_follow_ups": self.controller.remaining_follow_ups,
            "remaining_clarifications": self.controller.remaining_clarifications,
            "remaining_detours": self.controller.remaining_detours,
            "answers_before_latest_turn": list(self._answer_snapshot or ()),
            "pending_continuation_choice": self.controller.stage is Stage.AWAITING_CONTINUE_CHOICE,
            "early_end_reason": self._end_reason,
            "interrupted_output_started": self._interrupted_output_id is not None,
            "latest_candidate_turn": self._latest_candidate_turn,
        }

    async def _infer_decision(self, payload: dict[str, Any], generation: int) -> Mapping[str, Any]:
        result = await self._infer("decision", payload)
        action = str(result.get("action", "")).upper()
        text = result.get("text", "")
        identity_claim = isinstance(text, str) and "call me rehearseai" in text.lower()
        if (
            action == "DETOUR"
            and identity_claim
            and _asks_coach_identity(payload.get("latest_candidate_turn", ""))
        ):
            self._record(
                "decision_normalized",
                speaker=None,
                generation=generation,
                from_action="detour",
                to_action="coach_identity",
            )
            return {**result, "action": "coach_identity", "text": ""}
        if (
            action == "COACH_IDENTITY" or (action == "DETOUR" and identity_claim)
        ) and not _asks_coach_identity(payload.get("latest_candidate_turn", "")):
            self._record(
                "proposal_rejected",
                speaker=None,
                generation=generation,
                action=action.lower(),
                reason="coach_identity_without_question",
            )
            result = await self._infer(
                "decision",
                {
                    **payload,
                    "invalid_prior_decision": (
                        "The previous response answered a coach identity question "
                        "that was not asked. "
                        "Classify the candidate's actual request again."
                    ),
                },
            )
            revised_action = str(result.get("action", "")).upper()
            revised_text = result.get("text", "")
            if revised_action == "COACH_IDENTITY" or (
                revised_action == "DETOUR"
                and isinstance(revised_text, str)
                and "call me rehearseai" in revised_text.lower()
            ):
                self._record(
                    "proposal_rejected",
                    speaker=None,
                    generation=generation,
                    action=revised_action.lower(),
                    reason="repeated_identity_misclassification",
                )
                return {"action": "off_topic", "text": "", "project_fragment": ""}
        return result

    async def _queue_conversational_decision(
        self, result: Mapping[str, Any], *, interrupted: bool = False
    ) -> bool:
        action = str(result.get("action", "")).upper()
        if action not in {"FEEDBACK_REQUEST", "OFF_TOPIC", "COACH_IDENTITY", "DETOUR"}:
            return False
        text = result.get("text", "")
        if action == "COACH_IDENTITY" and not _asks_coach_identity(self._latest_candidate_turn):
            self._record(
                "proposal_rejected", speaker=None, reason="coach_identity_without_question"
            )
            return False
        if action == "FEEDBACK_REQUEST":
            kind = UtteranceKind.PROVISIONAL_FEEDBACK
            collected = [list(turns) for turns in self.controller.answers]
            number = self.controller.current_question_number
            if not interrupted and number and self._answer_snapshot is not None:
                collected[number - 1] = list(self._answer_snapshot)
            if not any(collected):
                speech = "I don't have enough project detail yet to give useful feedback."
            elif isinstance(text, str) and text.strip() and len(text) <= 300 and "?" not in text:
                speech = text.strip()
            else:
                speech = "I have only limited detail so far, so my feedback would be tentative."
            self._record(
                "provisional_feedback_prepared",
                speaker=None,
                answers_collected=any(collected),
            )
        elif action == "OFF_TOPIC":
            kind = UtteranceKind.TOPIC_BOUNDARY
            speech = "This session is for interview practice."
        elif action == "COACH_IDENTITY":
            kind = UtteranceKind.DETOUR_ACK
            speech = "You can call me RehearseAI."
        else:
            kind = UtteranceKind.DETOUR_ACK
            project_prompt = (
                self.controller.interrupted_utterance
                if interrupted
                else self._project_prompt_for_detour
            )
            if (
                self.controller.current_question_number == 1
                and self.controller.remaining_follow_ups == 0
                and project_prompt is not None
                and project_prompt.kind
                in {UtteranceKind.FOLLOW_UP, UtteranceKind.NUDGE, UtteranceKind.DETOUR_RESUME}
            ):
                # The candidate owns the project's meaning; a generic model
                # definition here could turn an ambiguous label into a fact.
                speech = "That can vary by project. I'd like to hear what yours did."
                self._record(
                    "model_detour_text_suppressed",
                    speaker=None,
                    reason="candidate_project_details_unverified",
                )
            elif not isinstance(text, str) or not text.strip() or len(text) > 180 or "?" in text:
                speech = "I can help with this interview practice."
            elif "call me rehearseai" in text.lower() and not _asks_coach_identity(
                self._latest_candidate_turn
            ):
                speech = "I can help with this interview practice."
            else:
                speech = text.strip()
        spoken = (
            f"{speech} Would you like to continue or finish?"
            if kind in {UtteranceKind.PROVISIONAL_FEEDBACK, UtteranceKind.TOPIC_BOUNDARY}
            else speech
        )
        if interrupted:
            utterance = self.controller.start_interrupted_detour(spoken, kind=kind)
        else:
            if self._answer_snapshot is not None:
                self.controller.restore_answers(self._answer_snapshot)
            fragment = result.get("project_fragment", "")
            if action == "DETOUR" and isinstance(fragment, str) and fragment.strip():
                if fragment.lower() in self._latest_candidate_turn.lower():
                    self.controller.add_detour_fragment(fragment)
                else:
                    self._record(
                        "proposal_rejected", speaker=None, reason="nonliteral_project_fragment"
                    )
            utterance = self.controller.start_general_detour(
                spoken, self._project_prompt_for_detour, kind=kind
            )
        self._answer_snapshot = None
        self._record("candidate_detour", speaker="candidate", kind=action.lower())
        await self._queue_speech(utterance)
        return True

    async def _apply_interview_decision(self, result: Mapping[str, Any], generation: int) -> None:
        self._remember_proposed_facts(result, generation)
        action = str(result.get("action", "")).upper()
        text = result.get("text", "")
        project_status = result.get("project_status")
        self._record(
            "decision_proposed",
            speaker=None,
            generation=generation,
            action=action.lower(),
            project_status=project_status,
            text=text if isinstance(text, str) else None,
            remaining_follow_ups=self.controller.remaining_follow_ups,
            remaining_clarifications=self.controller.remaining_clarifications,
            validated=True,
        )
        if await self._queue_conversational_decision(result):
            return
        if self.controller.remaining_follow_ups == 0:
            if action in {"FOLLOW_UP", "NUDGE", "CLARIFICATION"}:
                self._record(
                    "proposal_rejected",
                    speaker=None,
                    reason="project_probe_limit",
                    action=action.lower(),
                )
            if _looks_unfinished(self._latest_candidate_turn):
                action, text = "WAIT", ""
            elif action in {"FOLLOW_UP", "NUDGE", "CLARIFICATION", "WAIT"}:
                self._record(
                    "decision_overridden",
                    speaker=None,
                    reason="completed_answer_after_project_probe",
                    proposed_action=action.lower(),
                )
                action, text = "ADVANCE", ""
        if self.controller.current_question_number == 1:
            if project_status in {"technical_example", "no_example", "nontechnical", "unclear"}:
                self.controller.set_project_status(project_status)
            if project_status in {"no_example", "nontechnical"} or (
                project_status == "unclear"
                and action == "ADVANCE"
                and self.controller.project_established is not True
            ):
                answer = " ".join(self.controller.answers[0])
                await self._queue_speech(
                    self.controller.offer_project_example(
                        gentle=result.get("tone") == "gentle" and _broad_discouragement(answer)
                    )
                )
                return
        if action == "WAIT":
            self._record("awaiting_continuation", speaker=None, answer_number=self._answer_number)
            return
        try:
            kind = ReplyKind[action]
            utterance = self.controller.propose(
                kind, text if kind is not ReplyKind.ADVANCE else None
            )
        except (KeyError, ValueError) as exc:
            self._record(
                "proposal_rejected",
                speaker=None,
                reason=type(exc).__name__,
                action=action.lower(),
            )
            self._record("awaiting_continuation", speaker=None, answer_number=self._answer_number)
            return
        if utterance is not None:
            await self._queue_speech(utterance)
        else:
            self._start_response()

    def _remember_proposed_facts(self, result: Mapping[str, Any], generation: int) -> None:
        action = str(result.get("action", "")).upper()
        if action not in {"FOLLOW_UP", "NUDGE", "CLARIFICATION", "WAIT", "ADVANCE"}:
            return
        if result.get("project_status") in {"no_example", "nontechnical"}:
            return
        proposed = result.get("facts", [])
        if not isinstance(proposed, list):
            return
        source = self._latest_candidate_turn
        for item in proposed[:6]:
            if not isinstance(item, dict):
                continue
            field_name, wording = item.get("field"), item.get("wording")
            if field_name not in FACT_FIELDS or field_name == "candidate_name":
                continue
            if not isinstance(wording, str) or not wording.strip() or len(wording) > 240:
                continue
            if wording.casefold() not in source.casefold():
                self._record(
                    "memory_proposal_rejected",
                    speaker=None,
                    generation=generation,
                    field=field_name,
                    reason="not_in_latest_turn",
                )
                continue
            if any(
                correction.superseded_wording
                and re.search(
                    rf"(?<!\w){re.escape(correction.superseded_wording)}(?!\w)",
                    wording,
                    re.I,
                )
                for correction in self.controller.memory.corrections
            ):
                self._record(
                    "memory_proposal_rejected",
                    speaker=None,
                    generation=generation,
                    field=field_name,
                    reason="superseded_wording",
                )
                continue
            previous = self.controller.memory.get(field_name)
            fact = self.controller.memory.supply(
                field_name,
                wording,
                source_turn=self._turn_number,
                supplied_wording=wording,
                confirmed=False,
            )
            if fact is not previous:
                self._record(
                    "memory_updated",
                    speaker=None,
                    field=field_name,
                    value=fact.value,
                    confirmed=False,
                    source_turn=self._turn_number,
                    update_source="model_extraction",
                    generation=generation,
                )

    async def _respond(self, generation: int) -> None:
        if self._assigned_plan is not None:
            await self._respond_assigned(generation)
            return
        opening_turn: str | None = None
        inference_purpose: str | None = None
        inference_returned = False
        try:
            if (
                not self._stop_latched
                and self.controller.stage is Stage.AWAITING_CONTINUE_CHOICE
                and self._choice_turn is not None
            ):
                turn = self._choice_turn
                explicit = _explicit_continuation_choice(turn)
                if explicit is None:
                    self._record(
                        "inference_started",
                        speaker=None,
                        generation=generation,
                        purpose="continuation_choice",
                    )
                    inference_purpose = "continuation_choice"
                    result = await self._infer_decision(
                        {**self._payload(), "phase": "continuation_choice"}, generation
                    )
                    inference_returned = True
                    action = str(result.get("action", "")).upper()
                    if action not in {"CONTINUE", "OFF_TOPIC", "WAIT"}:
                        self._record(
                            "proposal_rejected",
                            speaker=None,
                            generation=generation,
                            action=action.lower(),
                            reason="choice_requires_explicit_finish",
                        )
                        action = "WAIT"
                else:
                    action = explicit.upper()
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                self._choice_turn = None
                self._record(
                    "continuation_choice_proposed",
                    speaker=None,
                    generation=generation,
                    action=action.lower(),
                    source="explicit" if explicit else "model",
                )
                if action == "CONTINUE":
                    utterance = self.controller.continue_interview()
                    if utterance is None:
                        answers = self.controller.answers[
                            self.controller.current_question_number - 1
                        ]
                        self._latest_candidate_turn = answers[-1]
                        self._start_response()
                    else:
                        self._continued_prompt_id = utterance.id
                        await self._queue_speech(utterance)
                elif action == "FINISH":
                    await self._end_early(turn)
                else:
                    utterance = self.controller.repeat_continuation_choice(
                        off_topic=action == "OFF_TOPIC",
                        presence=_presence_check(turn),
                        explain=action == "EXPLAIN",
                    )
                    if utterance is None:
                        self._record("continuation_choice_exhausted", speaker=None)
                        await self._end_early(turn, reason="recovery_limit")
                    else:
                        await self._queue_speech(utterance)
                return

            if (
                not self._stop_latched
                and self.controller.stage is Stage.DELIVERING
                and self._interrupted_candidate_turn is not None
                and self.controller.interrupted_utterance is not None
            ):
                turn = self._interrupted_candidate_turn
                interrupted = self.controller.interrupted_utterance
                payload = self._payload()
                payload["answers_by_question_before_latest_turn"] = [
                    list(turns) for turns in self.controller.answers
                ]
                if interrupted.question_number is not None:
                    number = interrupted.question_number
                    follow_ups, clarifications = self.controller.remaining_prompts_for(number)
                    payload.update(
                        current_question_number=number,
                        current_question=QUESTIONS[number - 1],
                        current_question_answers=list(self.controller.answers[number - 1]),
                        current_question_answer=" ".join(self.controller.answers[number - 1]),
                        answers_before_latest_turn=list(self.controller.answers[number - 1]),
                        remaining_follow_ups=follow_ups,
                        remaining_clarifications=clarifications,
                    )
                payload["phase"] = "interrupted_turn"
                self._record(
                    "inference_started",
                    speaker=None,
                    generation=generation,
                    purpose="interrupted_turn_decision",
                    utterance_id=interrupted.id,
                )
                inference_purpose = "interrupted_turn_decision"
                result = await self._infer_decision(payload, generation)
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                self._interrupted_candidate_turn = None
                action = str(result.get("action", "")).lower()
                if await self._queue_conversational_decision(result, interrupted=True):
                    self._record(
                        "decision_proposed",
                        speaker=None,
                        generation=generation,
                        action=action,
                        purpose="interrupted_turn_decision",
                        text=result.get("text"),
                        validated=True,
                    )
                    self._interrupted_output_id = None
                    return
                if self._interrupted_output_id == interrupted.id:
                    self._answer_snapshot = self.controller.answers[interrupted.question_number - 1]
                    self.controller.accept_barge_in_answer(turn)
                    self._record("barge_in_answer_accepted", speaker="candidate", text=turn)
                    self._interrupted_output_id = None
                    await self._apply_interview_decision(result, generation)
                    return
                self._record(
                    "unheard_prompt_recovery",
                    speaker=None,
                    utterance_id=interrupted.id,
                    candidate_text=turn,
                )
                if interrupted.kind is UtteranceKind.QUESTION:
                    utterance = self.controller.resolve_unheard_question(turn)
                elif interrupted.kind in {
                    UtteranceKind.FOLLOW_UP,
                    UtteranceKind.NUDGE,
                    UtteranceKind.CLARIFICATION,
                    UtteranceKind.REPEAT,
                }:
                    utterance = self.controller.resolve_unheard_follow_up(turn)
                else:
                    utterance = self.controller.retry_interrupted()
                await self._queue_speech(utterance)
                return

            if (
                not self._stop_latched
                and self.controller.stage in {Stage.AWAITING_NAME, Stage.AWAITING_READINESS}
                and self._opening_interpret_text is not None
            ):
                turn = self._opening_interpret_text
                opening_turn = turn
                self._record(
                    "inference_started",
                    speaker=None,
                    generation=generation,
                    purpose="opening_intent",
                )
                inference_purpose = "opening_intent"
                result = await self._infer(
                    "decision", {**self._payload(), "latest_candidate_turn": turn}
                )
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                self._opening_interpret_text = None
                action = str(result.get("action", "")).lower()
                self._record(
                    "opening_intent_proposed", speaker=None, generation=generation, action=action
                )
                if action == "detour":
                    reply = result.get("text")
                    if (
                        not isinstance(reply, str)
                        or not reply.strip()
                        or len(reply) > 120
                        or "?" in reply
                    ):
                        raise ValueError("Invalid coach-directed opening reply")
                    name = _preferred_name(
                        turn, prompted=self.controller.stage is Stage.AWAITING_NAME
                    )
                    if name:
                        self._remember_spoken_name(name, turn)
                    utterance = self.controller.answer_opening_question(reply, name)
                    self._record(
                        "candidate_detour",
                        speaker="candidate",
                        kind="opening_question",
                        preferred_name=name,
                    )
                    await self._queue_speech(utterance)
                    return
                await self._route_opening(turn, allow_interpretation=False)
                return

            if not self._stop_latched and self.controller.stage is Stage.AWAITING_OPENING_ACK:
                if name := _preferred_name(self.controller.greeting_response, prompted=False):
                    self._remember_spoken_name(name, self.controller.greeting_response)
                    self._record(
                        "preferred_name_received", speaker="candidate", preferred_name=name
                    )
                elif _NAME_DECLINE.search(self.controller.greeting_response):
                    self.controller.decline_name()
                greeting_readiness = _classify_greeting_readiness(self.controller.greeting_response)
                self._record(
                    "greeting_readiness_classified",
                    speaker=None,
                    classification=greeting_readiness.name.lower(),
                )
                self._record(
                    "inference_started", speaker=None, generation=generation, purpose="opening_ack"
                )
                inference_purpose = "opening_ack"
                result = await self._infer(
                    "opening_ack",
                    {**self._payload(), "greeting_readiness": greeting_readiness.name.lower()},
                )
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                acknowledgement = result.get("text")
                if not isinstance(acknowledgement, str):
                    raise ValueError("Opening response did not contain text")
                try:
                    utterance = self.controller.prepare_readiness(
                        acknowledgement,
                        greeting_readiness,
                        ask_name=not self.controller.preferred_name
                        and not self.controller.name_declined,
                    )
                except ValueError:
                    self._record("opening_ack_fallback", speaker=None, error_type="InvalidText")
                    utterance = self.controller.prepare_readiness(
                        _fallback_opening_ack(self.controller.greeting_response),
                        greeting_readiness,
                        ask_name=not self.controller.preferred_name
                        and not self.controller.name_declined,
                    )
                self._record(
                    "inference_completed",
                    speaker=None,
                    generation=generation,
                    purpose="opening_ack",
                    validated=True,
                )
                await self._queue_speech(utterance)
                return

            if not self._stop_latched and self.controller.stage in {
                Stage.AWAITING_NO_PROJECT_EXAMPLE,
                Stage.AWAITING_NO_PROJECT_CHOICE,
            }:
                phase = self.controller.stage
                turn = self._no_project_turn or ""
                self._record(
                    "inference_started",
                    speaker=None,
                    generation=generation,
                    purpose="no_project_decision",
                )
                inference_purpose = "no_project_decision"
                result = await self._infer_decision(self._payload(), generation)
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                status = result.get("project_status", "unclear")
                action = str(result.get("action", "")).upper()
                self._record(
                    "decision_proposed",
                    speaker=None,
                    generation=generation,
                    action=action.lower(),
                    project_status=status,
                    purpose="no_project_decision",
                    validated=True,
                )
                if status == "technical_example":
                    self._remember_proposed_facts(result, generation)
                    self._answer_snapshot = self.controller.answers[0]
                    self.controller.accept_project_example(turn)
                    self._record("project_example_accepted", speaker="candidate")
                    if action == "WAIT":
                        self._record("awaiting_continuation", speaker=None)
                        return
                    if action in ReplyKind.__members__ and action != "DETOUR":
                        kind = ReplyKind[action]
                        utterance = self.controller.propose(
                            kind, None if kind is ReplyKind.ADVANCE else result.get("text")
                        )
                        if utterance:
                            await self._queue_speech(utterance)
                        else:
                            self._start_response()
                        return
                    self._start_response()
                    return
                if phase is Stage.AWAITING_NO_PROJECT_EXAMPLE:
                    await self._queue_speech(self.controller.explain_no_project_scope())
                elif action in {"END", "FINISH"}:
                    await self._end_early(turn)
                else:
                    utterance = self.controller.reply_to_no_project_choice(
                        presence=_presence_check(turn)
                    )
                    if utterance is None:
                        self._record("no_project_choice_exhausted", speaker=None)
                        await self._end_early(turn, reason="recovery_limit")
                    else:
                        await self._queue_speech(utterance)
                return

            if not self._stop_latched and self.controller.stage is Stage.AWAITING_PROPOSAL:
                if (
                    self.controller.current_question_number > 1
                    or self.controller.project_established is True
                ) and _declines_detail(self._latest_candidate_turn):
                    self._record(
                        "candidate_declined_detail", speaker="candidate", generation=generation
                    )
                    utterance = self.controller.propose(ReplyKind.ADVANCE)
                    if utterance is not None:
                        await self._queue_speech(utterance)
                    else:
                        self._start_response()
                    return
                started_at = time.perf_counter()
                self._record(
                    "inference_started",
                    speaker=None,
                    generation=generation,
                    purpose="decision",
                    answer_number=self._answer_number,
                    llm_first_token_ms=None,
                    llm_first_token_basis="unavailable from structured run_inference",
                )
                inference_purpose = "decision"
                result = await self._infer_decision(self._payload(), generation)
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                self._record(
                    "inference_completed",
                    speaker=None,
                    generation=generation,
                    purpose="decision",
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
                )
                await self._apply_interview_decision(result, generation)
                return

            if not self._stop_latched and self.controller.stage is Stage.AWAITING_FEEDBACK:
                started_at = time.perf_counter()
                self._record(
                    "inference_started",
                    speaker=None,
                    generation=generation,
                    purpose="feedback",
                    answer_number=self._answer_number,
                    llm_first_token_ms=None,
                    llm_first_token_basis="unavailable from structured run_inference",
                )
                inference_purpose = "feedback"
                result = await self._infer("feedback", self._payload())
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                self._record(
                    "inference_completed",
                    speaker=None,
                    generation=generation,
                    purpose="feedback",
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
                    validated=True,
                )
                lines = result.get("lines")
                if (
                    not isinstance(lines, list)
                    or len(lines) != 2
                    or any(
                        not isinstance(line, str)
                        or not line.strip()
                        or len(line) > 160
                        or "\n" in line
                        or "?" in line
                        or not line.strip().endswith((".", "!"))
                        or re.search(r"[.!?]\s+\S", line.strip()[:-1])
                        or "end the session" in line.lower()
                        for line in lines
                    )
                ):
                    raise ValueError("Feedback response must contain two short sentences")
                name = self.controller.preferred_name
                address = f", {name}" if name else ""
                sign_off = (
                    f"That wraps up our practice interview{address}—thanks for sharing, "
                    "and come back anytime."
                )
                await self._queue_speech(
                    self.controller.prepare_feedback(
                        "\n".join([*(line.strip() for line in lines), sign_off])
                    )
                )

            if self.controller.stage is Stage.EARLY_CLOSING:
                self._record(
                    "inference_started",
                    speaker=None,
                    generation=generation,
                    purpose="early_feedback",
                    llm_first_token_ms=None,
                    llm_first_token_basis="unavailable from structured run_inference",
                )
                inference_purpose = "early_feedback"
                result = await self._infer("early_feedback", self._payload())
                inference_returned = True
                if generation != self._generation or self._closed:
                    self._record("inference_stale_discarded", speaker=None, generation=generation)
                    return
                lines = result.get("lines")
                if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
                    raise ValueError("Early feedback response did not contain lines")
                self._record(
                    "inference_completed",
                    speaker=None,
                    generation=generation,
                    purpose="early_feedback",
                    validated=True,
                )
                await self._queue_speech(self.controller.prepare_early_feedback("\n".join(lines)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (
                opening_turn is not None
                and self.controller.stage in {Stage.AWAITING_NAME, Stage.AWAITING_READINESS}
                and generation == self._generation
            ):
                self._opening_interpret_text = None
                self._record(
                    "response_error",
                    speaker=None,
                    error_type=type(exc).__name__,
                    failure_stage="opening_intent",
                    recoverable=True,
                )
                if "name" in opening_turn.lower() and "you" in opening_turn.lower():
                    name = _preferred_name(
                        opening_turn, prompted=self.controller.stage is Stage.AWAITING_NAME
                    )
                    if name:
                        self._remember_spoken_name(name, opening_turn)
                    await self._queue_speech(
                        self.controller.answer_opening_question("You can call me RehearseAI.", name)
                    )
                else:
                    await self._route_opening(opening_turn, allow_interpretation=False)
                return
            if (
                self.controller.stage is Stage.AWAITING_OPENING_ACK
                and generation == self._generation
            ):
                self._record("opening_ack_fallback", speaker=None, error_type=type(exc).__name__)
                self._record(
                    "response_error",
                    speaker=None,
                    error_type=type(exc).__name__,
                    failure_stage="opening_inference",
                )
                greeting_readiness = _classify_greeting_readiness(self.controller.greeting_response)
                await self._queue_speech(
                    self.controller.prepare_readiness(
                        _fallback_opening_ack(self.controller.greeting_response),
                        greeting_readiness,
                        ask_name=not self.controller.preferred_name
                        and not self.controller.name_declined,
                    )
                )
            else:
                self._record(
                    "response_error",
                    speaker=None,
                    error_type=type(exc).__name__,
                    failure_stage=(
                        "inference_request"
                        if not inference_returned
                        else "structured_response_validation"
                        if isinstance(exc, (ValueError, KeyError, TypeError))
                        else "application_state"
                    ),
                    inference_purpose=inference_purpose,
                )
                if self.controller.stage is Stage.EARLY_CLOSING and generation == self._generation:
                    await self._queue_speech(
                        self.controller.prepare_early_feedback(
                            "We'll stop here. Thanks for sharing what you have so far."
                        )
                    )

    def tts_context_assigned(self, utterance_id: int, context_id: str) -> None:
        pending = self.controller.pending
        if not self._closed and pending is not None and pending.id == utterance_id:
            self._tts_context_history[context_id] = utterance_id

    def tts_context_utterance(self, context_id: str | None) -> int | None:
        return self._tts_context_history.get(context_id or "")

    def tts_started(self, utterance_id: int, context_id: str | None) -> None:
        pending = self.controller.pending
        if pending is None or pending.id != utterance_id or context_id is None:
            return
        assigned = self._tts_context_history.get(context_id)
        if assigned is not None and assigned != utterance_id:
            return
        self._tts_contexts[context_id] = utterance_id
        self._tts_context_history[context_id] = utterance_id
        self._tts_context_started_at[context_id] = time.perf_counter()
        self._tts_audio_stats[context_id] = [0, 0]
        self._record(
            "tts_started", speaker=None, utterance_id=utterance_id, tts_context_id=context_id
        )

    def provider_tts_requested(self, context_id: str, text_length: int) -> None:
        if self._closed:
            return
        self._provider_tts_stats[context_id] = [time.perf_counter(), 0, 0]
        self._record(
            "provider_tts_requested",
            speaker=None,
            tts_context_id=context_id,
            utterance_id=self._tts_context_history.get(context_id),
            text_length=text_length,
        )

    def provider_tts_audio_chunk(self, context_id: str, byte_count: int) -> None:
        if self._closed or context_id not in self._provider_tts_stats:
            return
        stats = self._provider_tts_stats[context_id]
        stats[1] += 1
        stats[2] += byte_count
        if stats[1] == 1:
            self._record(
                "provider_first_audio",
                speaker=None,
                tts_context_id=context_id,
                utterance_id=self._tts_context_history.get(context_id),
                first_audio_ms=round((time.perf_counter() - stats[0]) * 1000, 1),
                audio_bytes=byte_count,
            )

    def provider_tts_error(self, context_id: str, error: str, status: int | None) -> None:
        if self._closed:
            return
        match = re.search(r"status:\s*(\d{3})", error)
        self._record(
            "provider_tts_error",
            speaker=None,
            tts_context_id=context_id,
            utterance_id=self._tts_context_history.get(context_id),
            error_type=error if error.isidentifier() else "ErrorFrame",
            provider_status=status or (int(match.group(1)) if match else None),
        )

    def provider_tts_cancelled(self, context_id: str) -> None:
        if not self._closed:
            self._record(
                "provider_tts_cancelled",
                speaker=None,
                tts_context_id=context_id,
                utterance_id=self._tts_context_history.get(context_id),
            )

    def provider_tts_finished(self, context_id: str) -> None:
        if self._closed or context_id not in self._provider_tts_stats:
            return
        started, chunks, byte_count = self._provider_tts_stats.pop(context_id)
        self._record(
            "provider_tts_finished",
            speaker=None,
            tts_context_id=context_id,
            utterance_id=self._tts_context_history.get(context_id),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            audio_chunks=chunks,
            audio_bytes=byte_count,
        )

    def pipeline_error(
        self,
        *,
        processor: str | None,
        category: str | None,
        error: str | None = None,
        exception_type: str | None = None,
    ) -> None:
        if self._closed:
            return
        match = re.fullmatch(r"TTS context ([0-9a-f-]+) completed with no audio", error or "")
        context_id = match.group(1) if match else None
        utterance_id = (
            self._tts_context_history.get(context_id)
            if context_id
            else self.controller.pending.id
            if self.controller.pending
            else None
        )
        if context_id and (
            self.controller.pending is None or self.controller.pending.id != utterance_id
        ):
            self._record(
                "stale_pipeline_error_ignored",
                speaker=None,
                processor=processor,
                tts_context_id=context_id,
                utterance_id=utterance_id,
            )
            return
        if match:
            code = "tts_context_no_audio"
        elif (
            processor in {"OpenAITTSService", "CartesiaTTSService"}
            and error
            and ("Error getting audio" in error or error.startswith("Error:"))
        ):
            code = "tts_provider_response"
        else:
            code = "pipeline_error"
        status_match = re.search(r"status:\s*(\d{3})", error or "")
        self._record(
            "response_error",
            speaker=None,
            failure_stage="pipeline",
            processor=processor,
            category=category,
            error_code=code,
            exception_type=exception_type,
            provider_status=(status_match.group(1) if status_match else None),
            tts_context_id=context_id,
            utterance_id=utterance_id,
            recoverable=bool(match),
            tts_audio_chunks=self._tts_audio_stats.get(context_id, [0, 0])[0],
            tts_audio_bytes=self._tts_audio_stats.get(context_id, [0, 0])[1],
            tts_context_elapsed_ms=(
                round((time.perf_counter() - self._tts_context_started_at[context_id]) * 1000, 1)
                if context_id in self._tts_context_started_at
                else None
            ),
        )

    def tts_audio_chunk(self, context_id: str | None, byte_count: int) -> bool:
        if self._closed or self._media_paused:
            return False
        pending = self.controller.pending
        if (
            not context_id
            or context_id not in self._tts_contexts
            or pending is None
            or pending.id != self._tts_contexts[context_id]
        ):
            self._record(
                "stale_tts_audio_discarded",
                speaker=None,
                tts_context_id=context_id,
                audio_bytes=byte_count,
            )
            return False
        stats = self._tts_audio_stats[context_id]
        stats[0] += 1
        stats[1] += byte_count
        self.tts_first_audio(context_id)
        return True

    def tts_first_audio(self, context_id: str | None) -> None:
        utterance_id = self._tts_contexts.get(context_id or "")
        pending = self.controller.pending
        if (
            utterance_id is not None
            and pending is not None
            and pending.id == utterance_id
            and utterance_id not in self._first_audio_ids
        ):
            self._first_audio_ids.add(utterance_id)
            self._mark("first_audio", first=True)
            self._record(
                "tts_first_audio_generated",
                speaker=None,
                utterance_id=utterance_id,
                tts_context_id=context_id,
                browser_playback_onset_ms=None,
            )

    def tts_finished(self, context_id: str | None) -> bool:
        utterance_id = self._tts_contexts.pop(context_id or "", None)
        if utterance_id is None:
            if not self._closed:
                self._record(
                    "stale_tts_stop_discarded",
                    speaker=None,
                    tts_context_id=context_id,
                    utterance_id=self._tts_context_history.get(context_id or ""),
                )
            return False
        self._tts_completed_ids.add(utterance_id)
        stats = self._tts_audio_stats.get(context_id or "", [0, 0])
        self._record(
            "tts_generation_finished",
            speaker=None,
            utterance_id=utterance_id,
            tts_context_id=context_id,
            audio_chunks=stats[0],
            audio_bytes=stats[1],
        )
        return True

    def bot_started(self) -> None:
        if self._media_paused:
            return
        pending = self.controller.pending
        if pending is None or self._closed or self._speaking_id == pending.id:
            return
        if pending.id not in self._first_audio_ids:
            return
        if self._cancelled_output_stop_debt:
            self._record(
                "cancelled_output_stop_debt_retired",
                speaker=None,
                count=self._cancelled_output_stop_debt,
                replacement_utterance_id=pending.id,
            )
            self._cancelled_output_stop_debt = 0
        self._speaking_id = pending.id
        self._output_started_ids.add(pending.id)
        self._output_started_at[pending.id] = self._now()
        self._mark("output_started")
        elapsed = (
            round((time.perf_counter() - self._last_turn_end) * 1000, 1)
            if self._last_turn_end is not None
            else None
        )
        self._record(
            "agent_output_started",
            speaker="agent",
            utterance_id=pending.id,
            latency_ms=elapsed,
            latency_basis=(
                "user turn stopped → server output started (approximate)"
                if elapsed is not None
                else None
            ),
            browser_playback_onset_ms=None,
        )
        self._record_response_latency(pending)

    def bot_stopped(self) -> None:
        if self._media_paused:
            return
        if self._cancelled_output_stop_debt:
            self._cancelled_output_stop_debt -= 1
            self._record("cancelled_output_stop_ignored", speaker=None)
            return
        pending = self.controller.pending
        if (
            pending is None
            or pending.id != self._speaking_id
            or self._closed
            or pending.id not in self._tts_completed_ids
        ):
            return
        self._speaking_id = None
        if self.controller.delivery_finished(pending.id):
            self._last_delivered_prompt = pending
            if pending.kind in {
                UtteranceKind.QUESTION,
                UtteranceKind.FOLLOW_UP,
                UtteranceKind.NUDGE,
                UtteranceKind.CLARIFICATION,
                UtteranceKind.REPEAT,
                UtteranceKind.DETOUR_RESUME,
            }:
                self._last_project_prompt = pending
            self._record(
                "agent_delivery_reported",
                speaker="agent",
                utterance_id=pending.id,
                text=pending.text,
                interrupted=False,
                delivery_basis="Pipecat BotStoppedSpeakingFrame at output transport",
                browser_playback_confirmed=False,
            )
            if pending.kind is UtteranceKind.GREETING and self._assigned_plan is not None:
                self._arm_opening_timer()
            if self.controller.stage is Stage.COMPLETE:
                self._record_coverage("time_budget" if self._ended_for_time else "completed")
                self._record("interview_complete", speaker=None)
            elif self.controller.stage is Stage.ENDED_EARLY:
                self._record_coverage("ended_early")
                self._record("interview_ended_early", speaker=None)
            elif self.controller.stage is Stage.AWAITING_QUESTION_START:
                next_question = self.controller.begin_question_one()
                asyncio.create_task(self._queue_speech(next_question))
            elif self.controller.stage is Stage.AWAITING_OPENING_RETURN:
                next_prompt = self.controller.resume_opening_question()
                asyncio.create_task(self._queue_speech(next_prompt))
            elif self.controller.stage is Stage.AWAITING_DETOUR_RETURN:
                next_utterance = self.controller.resume_detour()
                if next_utterance is None:
                    self._latest_candidate_turn = self.controller.answers[
                        self.controller.current_question_number - 1
                    ][-1]
                    self._start_response()
            elif self.controller.stage is Stage.AWAITING_CORRECTION_RETURN:
                next_utterance = self.controller.resume_after_correction()
                if next_utterance is not None:
                    asyncio.create_task(self._queue_speech(next_utterance))
                elif self.controller.stage in {
                    Stage.AWAITING_PROPOSAL,
                    Stage.AWAITING_OPENING_ACK,
                    Stage.AWAITING_FEEDBACK,
                }:
                    self._start_response()

    async def close(self, *, failed: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_opening_timer()
        self._generation += 1
        pending = self.controller.pending
        if pending is not None:
            self._record(
                "agent_cancellation",
                speaker=None,
                utterance_id=pending.id,
                cancellation_reason="session_disconnected",
            )
        task = self._response_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.controller.stage is Stage.COMPLETE:
            end_kind = "normal_completion"
        elif self.controller.stage is Stage.ENDED_EARLY:
            end_kind = "spoken_early_end"
        elif failed:
            end_kind = "pipeline_failed"
        else:
            end_kind = "disconnected_before_completion"
        self._record_coverage(end_kind)
        self._record(
            "session_ended",
            speaker=None,
            interview_stage=self.controller.stage.name.lower(),
            session_end_kind=end_kind,
        )
        self.log.close()
