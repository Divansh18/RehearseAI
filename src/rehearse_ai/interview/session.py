"""Connect one interview controller to speech, inference, and session events."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
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
    FEEDBACK_DECLINED,
    FINISH_CLARIFICATION,
    OFF_TOPIC_REPLY,
    OUTCOME_UNKNOWN,
    REPEAT_EXHAUSTED,
    RESTART_UNAVAILABLE,
    Intent,
    interpret,
)
from rehearse_ai.interview.memory import FactField
from rehearse_ai.interview.phrases import (
    AFFIRM,
    FLOOR_CONTROL,
    NAME_TOKEN,
    REPEAT,
    SKIP,
    CorrectionRequest,
    classify_greeting_readiness,
    fallback_opening_ack,
    is_sentence_request,
    letters_as_name,
    parse_correction,
    spoken_name_spelling,
)
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.interview.speech_output import SpeechOutput
from rehearse_ai.interview.timeline import ResponseTimeline

# The opening is one exchange: greeting, the candidate's reply, a short acknowledgement and a
# transition. If the candidate says nothing it moves on after this wait. The whole opening is
# counted in active time, and its length is reported with the interview's coverage summary.
# Interruption: audio that played for less than this was not meaningfully heard. This is measured
# on the server at the transport output; it approximates, and is not, what the browser played.
HEARD_MIN_SECS = 1.5
HEARD_MIN_FRACTION = 0.5
WORDS_PER_SECOND = 2.6
OPENING_REPLY_WAIT_SECS = 12.0
OPENING_ACK_TIMEOUT_SECS = 4.0
_OPENING_KINDS = {UtteranceKind.GREETING, UtteranceKind.OPENING_ACK}
Infer = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]]
Speak = Callable[[Utterance], Awaitable[None]]
Publish = Callable[[dict[str, Any]], None]
CancelAudio = Callable[[], Awaitable[None]]


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
        self._timeline = ResponseTimeline(self._now)
        self._speech = SpeechOutput(
            pending=lambda: self.controller.pending,
            closed=lambda: self._closed,
            paused=lambda: self._media_paused,
            record=self._record,
            mark=self._timeline.mark,
        )
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
        self._pending_correction: CorrectionRequest | None = None

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
            self._speech.forget_utterance(pending.id)
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
        return token if token and len(token) <= 30 and NAME_TOKEN.fullmatch(token) else ""

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
        if classify_greeting_readiness(answer) is Readiness.READY:
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
            self._timeline.mark("decision_start")
            result = await asyncio.wait_for(
                self._infer("opening_ack", {"greeting_response": reply}), OPENING_ACK_TIMEOUT_SECS
            )
            self._timeline.mark("decision_done")
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
            ack = fallback_opening_ack(reply)
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

    def _record_response_latency(self, utterance: Utterance) -> None:
        details = self._timeline.latency_details()
        if details is not None:
            self._record(
                "response_latency",
                speaker=None,
                utterance_id=utterance.id,
                utterance_kind=utterance.kind.name.lower(),
                **details,
            )

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
        spelling = spoken_name_spelling(answer, prompted=prompted)
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
                self.controller.submit_name(name, classify_greeting_readiness(remainder))
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

    def _correction_field(self, request: CorrectionRequest) -> FactField | None:
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
        request = parse_correction(answer)
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
            replacement = letters_as_name(replacement, allow_word=True) or replacement
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
            self._timeline.mark("vad_stop")
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
            self._timeline.mark("last_stt")
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
        self._timeline.mark("prepared", first=True)
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
        self._timeline.clear()
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
            self._speech.forget_utterance(pending.id)
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
        self._timeline.mark("turn_complete")
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
            if not answer or is_sentence_request(REPEAT, answer):
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
            if AFFIRM.fullmatch(answer.strip()):
                await self._end_early(answer)
                return
        intent = interpret(answer)
        if intent.is_control and await self._route_assigned_control(intent, answer, interrupted):
            return
        if is_sentence_request(SKIP, answer):
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
                if FLOOR_CONTROL.fullmatch(answer.strip()):
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
            self._timeline.mark("decision_start")
            result = await self._infer("assigned_decision", self._assigned_payload())
            self._timeline.mark("decision_done")
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
        self._speech.context_assigned(utterance_id, context_id)

    def tts_context_utterance(self, context_id: str | None) -> int | None:
        return self._speech.utterance_for(context_id)

    def tts_started(self, utterance_id: int, context_id: str | None) -> None:
        self._speech.started(utterance_id, context_id)

    def provider_tts_requested(self, context_id: str, text_length: int) -> None:
        self._speech.provider_requested(context_id, text_length)

    def provider_tts_audio_chunk(self, context_id: str, byte_count: int) -> None:
        self._speech.provider_audio_chunk(context_id, byte_count)

    def provider_tts_error(self, context_id: str, error: str, status: int | None) -> None:
        self._speech.provider_error(context_id, error, status)

    def provider_tts_cancelled(self, context_id: str) -> None:
        self._speech.provider_cancelled(context_id)

    def provider_tts_finished(self, context_id: str) -> None:
        self._speech.provider_finished(context_id)

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
            self._speech.utterance_for(context_id)
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
            **self._speech.context_summary(context_id),
        )

    def tts_audio_chunk(self, context_id: str | None, byte_count: int) -> bool:
        return self._speech.audio_chunk(context_id, byte_count)

    def tts_first_audio(self, context_id: str | None) -> None:
        self._speech.first_audio(context_id)

    def tts_finished(self, context_id: str | None) -> bool:
        return self._speech.finished(context_id)

    def bot_started(self) -> None:
        if self._media_paused:
            return
        pending = self.controller.pending
        if pending is None or self._closed or self._speaking_id == pending.id:
            return
        if not self._speech.has_first_audio(pending.id):
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
        self._timeline.mark("output_started")
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
            or not self._speech.generation_completed(pending.id)
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
