"""Pipecat assembly; interview policy remains in the domain controller."""

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from rehearse_ai.config import Settings
from rehearse_ai.interview.controller import Stage, Utterance
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.prompts import load_prompt
from rehearse_ai.voice.interruption import ConfirmedSpeechStartStrategy
from rehearse_ai.voice.tts import UtteranceMarkerFrame, build_tts_service

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "follow_up",
                "nudge",
                "clarification",
                "wait",
                "advance",
                "detour",
                "feedback_request",
                "off_topic",
                "coach_identity",
                "continue",
                "finish",
                "end",
            ],
        },
        "text": {"type": "string"},
        "project_fragment": {"type": "string"},
        "project_status": {
            "type": "string",
            "enum": ["technical_example", "no_example", "nontechnical", "unclear"],
        },
        "tone": {"type": "string", "enum": ["neutral", "gentle"]},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": [
                            "project_name",
                            "project_purpose",
                            "contribution",
                            "technical_challenges",
                            "testing_evidence",
                            "technical_terms",
                            "technologies",
                        ],
                    },
                    "wording": {"type": "string"},
                },
                "required": ["field", "wording"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action", "text", "project_fragment", "project_status", "tone", "facts"],
    "additionalProperties": False,
}
FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["lines"],
    "additionalProperties": False,
}
OPENING_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
ASSIGNED_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "follow_up",
                "nudge",
                "wait",
                "advance",
                "feedback_request",
                "off_topic",
                "coach_identity",
            ],
        },
        "text": {"type": "string"},
        "covered_future_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_number": {"type": "integer"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["question_number", "evidence_quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action", "text", "covered_future_questions"],
    "additionalProperties": False,
}


# How long an empty turn boundary waits for the transcript that caused it. Pipecat only ends a
# turn once a transcript exists, but the boundary is a system frame and can overtake that
# transcript (a data frame) by a few milliseconds. Measured gaps were under 10 ms.
LATE_TRANSCRIPT_GRACE_SECS = 0.35


def stt_details(frame: Any) -> dict[str, Any]:
    """Provider-reported confidence and audio position of a transcript, when available.

    These explain whether a mistake was *heard* wrongly (low confidence) or only *delivered*
    late (audio position far behind arrival). Missing values are simply omitted.
    """
    details: dict[str, Any] = {}
    result = getattr(frame, "result", None)
    try:
        alternative = result.channel.alternatives[0]
        if isinstance(alternative.confidence, (int, float)):
            details["stt_confidence"] = round(float(alternative.confidence), 3)
        start, duration = getattr(result, "start", None), getattr(result, "duration", None)
        if isinstance(start, (int, float)) and isinstance(duration, (int, float)):
            details["stt_audio_start_s"] = round(float(start), 2)
            details["stt_audio_end_s"] = round(float(start) + float(duration), 2)
    except (AttributeError, IndexError, TypeError):
        pass
    return details


class InterviewInput(FrameProcessor):
    """Collect final STT segments within Pipecat's single user-turn boundary."""

    def __init__(self, late_grace_secs: float = LATE_TRANSCRIPT_GRACE_SECS) -> None:
        super().__init__()
        self.session: InterviewSession | None = None
        self._segments: list[str] = []
        self._turn_open = False
        self._late_grace_secs = late_grace_secs
        self._last_boundary_id = 0
        self._deferred_boundary: int | None = None
        self._deferred_task: asyncio.Task[None] | None = None
        self._media_paused = False
        self._discard_until_boundary = False
        self._resume_requested = False

    def _cancel_deferred(self) -> None:
        self._deferred_boundary = None
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
        self._deferred_task = None

    async def cleanup(self) -> None:
        self._cancel_deferred()
        await super().cleanup()

    async def _expire_deferred(self, boundary_id: int) -> None:
        """No transcript came for an empty boundary: report it as the empty turn it is."""
        try:
            await asyncio.sleep(self._late_grace_secs)
        except asyncio.CancelledError:
            return
        if self._deferred_boundary == boundary_id and self.session is not None:
            self._deferred_boundary = None
            self._deferred_task = None
            await self.session.user_stopped("", segment_count=0, boundary_frame_id=boundary_id)

    def set_media_paused(self, paused: bool) -> None:
        self._cancel_deferred()
        if paused:
            self._discard_until_boundary = self._turn_open or bool(self._segments)
            self._resume_requested = False
        self._media_paused = paused
        self._segments.clear()
        self._turn_open = False

    def request_resume(self) -> bool:
        if self._discard_until_boundary:
            self._resume_requested = True
            return False
        self.set_media_paused(False)
        return True

    async def speak(self, utterance: Utterance) -> None:
        if self.session is None or self.session.controller.pending != utterance:
            return
        await self.push_frame(UtteranceMarkerFrame(utterance_id=utterance.id))
        if self.session.controller.pending != utterance:
            return
        await self.push_frame(TTSSpeakFrame(text=utterance.text, append_to_context=False))

    async def cancel_audio(self) -> None:
        await self.broadcast_interruption()

    async def _handle_transcript(self, frame: TranscriptionFrame) -> None:
        """Place a final transcript by when it was *created*, not when it arrived.

        A transcript created before a turn boundary but delivered after it belongs to that
        turn. Treating it as the start of the next turn (as arrival order suggests) held it
        unprocessed until some later boundary and merged it into unrelated speech.
        """
        session = self.session
        assert session is not None  # noqa: S101 - guarded by the caller
        text = frame.text.strip()
        details = stt_details(frame)
        common = {
            "frame_timestamp": frame.timestamp,
            "finalized": frame.finalized,
            "frame_id": frame.id,
            **details,
        }
        deferred = self._deferred_boundary
        if deferred is not None and frame.id < deferred and not self._turn_open:
            self._cancel_deferred()
            session.transcript_segment(
                text, correlation="late_for_boundary", boundary_frame_id=deferred, **common
            )
            await session.user_stopped(text, segment_count=1, boundary_frame_id=deferred)
            return
        if not self._turn_open and frame.id < self._last_boundary_id:
            session.transcript_segment(
                text,
                correlation="late_after_boundary",
                boundary_frame_id=self._last_boundary_id,
                **common,
            )
            await session.late_transcript(text, boundary_frame_id=self._last_boundary_id)
            return
        self._segments.append(text)
        session.transcript_segment(
            text,
            correlation="active_turn" if self._turn_open else "pre_start_segment",
            **common,
        )
        if self._turn_open:
            await session.detect_stop_from_segments(" ".join(self._segments))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            self._media_paused
            and direction is FrameDirection.DOWNSTREAM
            and isinstance(
                frame,
                (
                    UserStartedSpeakingFrame,
                    UserStoppedSpeakingFrame,
                    VADUserStartedSpeakingFrame,
                    VADUserStoppedSpeakingFrame,
                    TranscriptionFrame,
                ),
            )
        ):
            if isinstance(frame, UserStoppedSpeakingFrame):
                self._discard_until_boundary = False
                if self._resume_requested and self.session is not None:
                    self.set_media_paused(False)
                    await self.session.media_resume()
            return
        if direction is FrameDirection.DOWNSTREAM and self.session is not None:
            if isinstance(frame, UserStartedSpeakingFrame):
                # New speech supersedes an empty boundary still waiting for its transcript; that
                # transcript, if it arrives, is handled as a late continuation.
                self._cancel_deferred()
                if not self._turn_open:
                    self._turn_open = True
                    await self.session.user_started()
                    if self._segments:
                        await self.session.detect_stop_from_segments(" ".join(self._segments))
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self.session.turn_signal("vad_started", frame_id=frame.id)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self.session.turn_signal(
                    "vad_stopped",
                    frame_id=frame.id,
                    stop_secs=frame.stop_secs,
                    speech_end_basis="Silero VAD stop; estimate, not last audible phoneme",
                )
            elif isinstance(frame, TranscriptionFrame):
                if frame.text.strip():
                    await self._handle_transcript(frame)
            elif isinstance(frame, UserStoppedSpeakingFrame):
                if self._turn_open or self._segments:
                    boundary = frame.id
                    self._last_boundary_id = boundary
                    self._turn_open = False
                    if self._segments:
                        text, count = " ".join(self._segments), len(self._segments)
                        self._segments.clear()
                        await self.session.user_stopped(
                            text, segment_count=count, boundary_frame_id=boundary
                        )
                    else:
                        # Pipecat ends a turn only once it holds a transcript, so one is in
                        # flight behind this boundary. Wait briefly for it.
                        self._deferred_boundary = boundary
                        self._deferred_task = asyncio.create_task(self._expire_deferred(boundary))
        await self.push_frame(frame, direction)


class DeliveryObserver(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.session: InterviewSession | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM and self.session is not None:
            if isinstance(frame, BotStartedSpeakingFrame):
                self.session.bot_started()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.session.bot_stopped()
        await self.push_frame(frame, direction)


class AudioObserver(FrameProcessor):
    """Correlate TTS context IDs with the utterance marker before output."""

    def __init__(self) -> None:
        super().__init__()
        self.session: InterviewSession | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM and self.session is not None:
            if isinstance(frame, UtteranceMarkerFrame):
                return
            if isinstance(frame, TTSStartedFrame):
                utterance_id = self.session.tts_context_utterance(frame.context_id)
                if utterance_id is not None:
                    self.session.tts_started(utterance_id, frame.context_id)
            elif isinstance(frame, TTSAudioRawFrame):
                if not self.session.tts_audio_chunk(frame.context_id, len(frame.audio)):
                    return
            elif isinstance(frame, TTSStoppedFrame):
                if not self.session.tts_finished(frame.context_id):
                    return
        await self.push_frame(frame, direction)


def inference_adapter(llm: OpenAILLMService):
    async def infer(kind: str, payload: dict[str, Any]) -> Mapping[str, Any]:
        schema = {
            "decision": DECISION_SCHEMA,
            "opening_ack": OPENING_SCHEMA,
            "feedback": FEEDBACK_SCHEMA,
            "early_feedback": FEEDBACK_SCHEMA,
            "assigned_decision": ASSIGNED_DECISION_SCHEMA,
        }[kind]
        response = await llm.run_inference(
            LLMContext(messages=[{"role": "user", "content": json.dumps(payload)}]),
            max_tokens=320 if kind == "decision" else 220,
            system_instruction=load_prompt(kind),
            response_schema=schema,
        )
        if response is None:
            raise ValueError("Empty LLM response")
        parsed = json.loads(response)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response was not an object")
        return parsed

    return infer


class LiveSession:
    def __init__(
        self,
        connection: Any,
        settings: Settings,
        *,
        build_id: str | None = None,
        assignment_id: str | None = None,
        session_id: str | None = None,
        event_sink: Callable[[dict[str, Any]], bool] | None = None,
        interview_plan: dict[str, Any],
        self_reported_name: str | None = None,
    ) -> None:
        self.connection = connection
        self.assignment_id = assignment_id
        self.log = SessionLog(settings.session_dir, session_id or str(uuid.uuid4()), event_sink)
        if assignment_id is not None:
            self.log.record("assignment_session_authenticated", assignment_id=assignment_id)
        self.log.record(
            "stt_configured",
            model=settings.stt_model,
            language="en",
            keyterm_count=len(settings.stt_keyterms),
            sample_rate_hz=16000,
            channels=1,
            encoding="linear16",
            interim_results=True,
            endpointing="provider_default",
            vad_stop_secs=settings.vad_stop_secs,
            turn_pause_secs=settings.turn_pause_secs,
            interrupt_confirm_secs=settings.interrupt_confirm_secs,
            interrupt_transcript_grace_secs=settings.interrupt_transcript_grace_secs,
        )
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        self._transport = transport
        llm = OpenAILLMService(
            api_key=settings.openai_api_key,
            settings=OpenAILLMService.Settings(model=settings.llm_model),
        )
        input_adapter = InterviewInput()
        audio_observer = AudioObserver()
        delivery = DeliveryObserver()
        self.session = InterviewSession(
            self.log,
            inference_adapter(llm),
            input_adapter.speak,
            connection.send_app_message,
            turn_pause_secs=settings.turn_pause_secs,
            cancel_audio=input_adapter.cancel_audio,
            build_id=build_id,
            interview_plan=interview_plan,
            self_reported_name=self_reported_name,
        )
        input_adapter.session = self.session
        start_strategy = ConfirmedSpeechStartStrategy(
            confirm_secs=settings.interrupt_confirm_secs,
            transcript_grace_secs=min(
                settings.interrupt_transcript_grace_secs,
                max(0.0, settings.turn_pause_secs - 0.1),
            ),
            observer=lambda name, **details: self.session.turn_signal(name, **details),
        )
        audio_observer.session = self.session
        delivery.session = self.session
        self.pipeline = Pipeline(
            [
                transport.input(),
                VADProcessor(
                    vad_analyzer=SileroVADAnalyzer(
                        params=VADParams(
                            confidence=settings.vad_confidence,
                            start_secs=settings.vad_start_secs,
                            stop_secs=settings.vad_stop_secs,
                        )
                    )
                ),
                DeepgramSTTService(
                    api_key=settings.deepgram_api_key,
                    settings=DeepgramSTTService.Settings(
                        model=settings.stt_model,
                        keyterm=list(settings.stt_keyterms) if settings.stt_keyterms else None,
                    ),
                ),
                UserTurnProcessor(
                    user_turn_stop_timeout=30.0,
                    user_turn_strategies=UserTurnStrategies(
                        start=[start_strategy],
                        stop=[
                            SpeechTimeoutUserTurnStopStrategy(
                                user_speech_timeout=settings.turn_pause_secs
                            )
                        ],
                    ),
                ),
                input_adapter,
                build_tts_service(settings, self.session),
                audio_observer,
                transport.output(),
                delivery,
            ]
        )
        self.worker = PipelineWorker(
            self.pipeline, params=PipelineParams(audio_in_sample_rate=16000)
        )
        self.runner = WorkerRunner(handle_sigint=False)

        @self.worker.event_handler("on_pipeline_error")
        async def pipeline_error(_worker: PipelineWorker, frame: Any) -> None:
            category = frame.category.value if frame.category is not None else None
            processor = (
                type(frame.processor).__name__.removeprefix("Observed")
                if frame.processor is not None
                else None
            )
            self.session.pipeline_error(
                processor=processor,
                category=category,
                error=frame.error,
                exception_type=(type(frame.exception).__name__ if frame.exception else None),
            )

        @transport.event_handler("on_client_disconnected")
        async def disconnected(_transport: SmallWebRTCTransport, _client: Any) -> None:
            await self.stop()

        @transport.event_handler("on_app_message")
        async def app_message(_transport: SmallWebRTCTransport, message: Any, _sender: str) -> None:
            if not isinstance(message, dict) or message.get("type") != "rehearseai_media":
                return
            if message.get("action") == "begin":
                await self.session.start()
            elif (
                message.get("action") == "pause"
                and not self.session.media_paused
                and self.session.controller.stage is not Stage.NEW
            ):
                input_adapter.set_media_paused(True)
                await self.session.media_pause(str(message.get("reason", "media_lost"))[:40])
            elif message.get("action") == "resume" and self.session.media_paused:
                if input_adapter.request_resume():
                    await self.session.media_resume()

    async def run(self) -> None:
        failed = False
        try:
            await self.runner.add_workers(self.worker)
            await self.runner.run()
        except Exception:
            failed = True
            self.session.pipeline_error(
                processor="PipelineWorker",
                category="unexpected_failure",
                error="Pipeline worker stopped unexpectedly",
                exception_type=None,
            )
            raise
        finally:
            await self.session.close(failed=failed)

    async def stop(self) -> None:
        await self.session.close()
        await self.runner.cancel()
