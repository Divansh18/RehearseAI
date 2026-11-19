"""Connect one interview controller to speech, inference, and session events."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rehearse_ai.interview.browser_events import candidate_event
from rehearse_ai.interview.budget import (
    ActiveClock,
    RemainingQuestion,
    pressure,
    question_seconds,
    skip_for_time,
)
from rehearse_ai.interview.controller import (
    InterviewController,
    InvalidTransition,
    LimitReached,
    Readiness,
    ReplyKind,
    Stage,
    Utterance,
    UtteranceKind,
)
from rehearse_ai.interview.controls import (
    Intent,
    interpret,
)
from rehearse_ai.interview.memory import FactField
from rehearse_ai.interview.session_log import SessionLog

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
_NAME_SPELLING = re.compile(
    r"^\s*(?:(?:actually|sorry)[, ]+)?"
    r"(?P<cue>my name is spelled|i spell my name(?: as)?|"
    r"i spell it(?: as)?|(?:it's|it is) spelled|spelled(?: as)?)\s+"
    r"(?P<letters>.+?)\s*$",
    re.I,
)
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


def _is_sentence_request(pattern: re.Pattern[str], text: str) -> bool:
    """Recognize a whole control clause even after a separate preamble."""
    return any(pattern.fullmatch(clause.strip()) for clause in re.split(r"(?<=[.!?])\s+", text))


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
        interview_plan: dict[str, Any],
        self_reported_name: str | None = None,
        clock: Callable[[], float] | None = None,
        opening: bool = True,
        opening_wait_secs: float = OPENING_REPLY_WAIT_SECS,
    ) -> None:
        self._plan = interview_plan
        self._now = clock or time.monotonic
        self._clock = ActiveClock(self._now)
        self._marks: dict[str, float] = {}
        self._output_started_at: dict[int, float] = {}
        self._closing_replays = 0
        self._finish_clarified = False
        self._opening = opening
        self._opening_wait_secs = opening_wait_secs
        self._opening_task: asyncio.Task[None] | None = None
        self._opening_seconds: int | None = None
        self._first_name_confirmed = self._first_name(self_reported_name)
        self._greeting = self._greeting_for(self_reported_name)
        self._target_seconds = int(interview_plan.get("target_minutes", 0)) * 60
        self._question_seconds = (
            question_seconds(interview_plan["questions"], self._target_seconds)
            if self._target_seconds
            else []
        )
        self._ended_for_time = False
        self._coverage_recorded = False
        plan_questions = interview_plan["questions"]
        self.controller = InterviewController(
            tuple(item["question"] for item in plan_questions),
            probe_limits=tuple(item["max_probes"] for item in plan_questions),
            required=tuple(item["required"] for item in plan_questions),
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
            discard_interrupted=True,
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
        return interpret(text).kind == "finish"

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
            # Speech counts as a reply only after a minimum amount of audio was heard.
            heard_enough = output_started and (
                pending.kind in _OPENING_KINDS or self._heard_enough(pending, heard_secs)
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

        await self._route_assigned_turn(answer)

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
        total = len(self._plan.get("questions", []))
        number = (
            self.controller.pending_prompt.question_number
            if self.controller.pending_prompt
            else None
        )
        minutes = self._plan.get("target_minutes")
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
        plan = self._plan
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
        if not self._target_seconds:
            return "normal"
        return pressure(
            self._clock.active_seconds(), self._target_seconds, self._remaining_for_budget()
        )

    def _apply_time_budget(self) -> str:
        """Drop unaffordable questions at a turn boundary and return the time pressure."""
        if not self._target_seconds:
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
        if self._coverage_recorded:
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
        questions = self._plan.get("questions", [])
        number = self.controller.current_question_number
        answers = self.controller.answers
        covered = self.controller.covered_source_turns
        skipped = set(self.controller.time_skipped_numbers)
        context: dict[str, Any] = {
            "title": self._plan.get("title", ""),
            "purpose": self._plan.get("purpose", ""),
            "interviewer_style": self._plan.get("interviewer_style", ""),
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
            "candidate_focus_areas": list(self._plan.get("candidate_focus_areas", []))[:8],
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

    async def _respond(self, generation: int) -> None:
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
            await self._queue_speech(
                self.controller.prepare_early_feedback(self._early_closing_text())
            )

    def _start_response(self) -> None:
        if self._media_paused or (
            self._stop_latched and self.controller.stage is not Stage.EARLY_CLOSING
        ):
            return
        self._generation += 1
        generation = self._generation
        self._response_task = asyncio.create_task(self._respond(generation))

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
            if pending.kind is UtteranceKind.GREETING:
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
