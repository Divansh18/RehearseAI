import asyncio
import json

import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from support import plan_dict

from rehearse_ai.interview.controller import (
    UtteranceKind,
)
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.prompts import load_prompt
from rehearse_ai.voice.pipeline import InterviewInput, inference_adapter


def output_started(session):
    utterance = session.controller.pending
    context = f"tts-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    return context


def deliver(session):
    context = output_started(session)
    session.tts_finished(context)
    session.bot_stopped()


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def turn(adapter, *segments):
    await adapter.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    for number, text in enumerate(segments, 1):
        await adapter.process_frame(
            TranscriptionFrame(
                text=text, user_id="candidate", timestamp=f"segment-{number}", finalized=False
            ),
            FrameDirection.DOWNSTREAM,
        )
    await adapter.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


def harness(tmp_path, infer=None, *, collect_name=False):
    adapter = InterviewInput()
    adapter.push_frame = lambda *_args, **_kwargs: asyncio.sleep(0)
    spoken = []

    async def default_infer(kind, payload):
        assert kind == "opening_ack"
        assert payload["greeting_response"]
        return {"text": "Thanks for telling me."}

    async def speak(utterance):
        spoken.append(utterance)

    log = SessionLog(tmp_path, "opening")
    session = InterviewSession(
        log, infer or default_infer, speak, lambda _: None, interview_plan=plan_dict()
    )
    adapter.session = session
    return session, adapter, spoken, log


@pytest.mark.asyncio
async def test_spoken_stop_during_greeting_preempts_opening_inference(tmp_path):
    calls = []

    async def infer(kind, payload):
        calls.append((kind, payload))
        return {"text": "This should not run."}

    session, adapter, spoken, log = harness(tmp_path, infer)
    await session.start()
    await turn(adapter, "Can we end the interview?")
    assert calls == []
    assert session.controller.pending.kind is UtteranceKind.EARLY_FEEDBACK
    assert not any(item.kind is UtteranceKind.QUESTION for item in spoken)
    rows = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert any(row["event"] == "early_end_requested" for row in rows)
    await session.close()


@pytest.mark.asyncio
async def test_spoken_stop_during_ready_greeting_ack_blocks_question_one(tmp_path):
    session, adapter, spoken, _log = harness(tmp_path)
    await session.start()
    deliver(session)
    await turn(adapter, "I'm good, let's start.")
    await settle()
    assert session.controller.pending.kind is UtteranceKind.OPENING_ACK
    await turn(adapter, "Stop the interview.")
    assert session.controller.pending.kind is UtteranceKind.EARLY_FEEDBACK
    assert not any(item.kind is UtteranceKind.QUESTION for item in spoken)
    await session.close()


@pytest.mark.asyncio
async def test_speech_queue_and_pipeline_failures_are_published(tmp_path):
    published = []

    async def broken_speak(_utterance):
        raise RuntimeError("synthetic transport failure")

    session = InterviewSession(
        SessionLog(tmp_path, "errors"),
        lambda *_args: asyncio.sleep(0),
        broken_speak,
        published.append,
        interview_plan=plan_dict(),
    )
    await session.start()
    session.pipeline_error(processor="OpenAITTSService", category="provider_error")
    errors = [
        message["event"] for message in published if message["event"]["event"] == "response_error"
    ]
    assert [error["failure_stage"] for error in errors] == ["speech_queue", "pipeline"]
    # Browser errors carry fixed labels only; the utterance id and details stay in the log.
    assert set(errors[0]) == {"event", "failure_stage", "processor", "error_code"}
    assert errors[1]["processor"] == "OpenAITTSService"
    await session.close()


@pytest.mark.asyncio
async def test_spoken_stop_cancels_opening_ack_inference(tmp_path):
    started = asyncio.Event()
    cancelled = []

    async def infer(kind, _payload):
        assert kind == "opening_ack"
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    session, adapter, spoken, _log = harness(tmp_path, infer)
    await session.start()
    deliver(session)
    await turn(adapter, "I'm okay.")
    await started.wait()
    await turn(adapter, "I wanna", "end the interview.")
    await settle()
    assert cancelled
    assert session.controller.pending.kind is UtteranceKind.EARLY_FEEDBACK
    assert not any(item.kind is UtteranceKind.QUESTION for item in spoken)
    await session.close()


@pytest.mark.asyncio
async def test_opening_ack_uses_versioned_prompt_and_structured_schema():
    calls = []

    class FakeLLM:
        async def run_inference(self, context, **kwargs):
            calls.append((context, kwargs))
            return '{"text":"Thanks for telling me."}'

    result = await inference_adapter(FakeLLM())(
        "opening_ack", {"greeting_response": "I'm a little nervous."}
    )
    assert result == {"text": "Thanks for telling me."}
    assert calls[0][1]["system_instruction"] == load_prompt("opening_ack")
    assert "If they ask how you are" in calls[0][1]["system_instruction"]
    assert "Avoid parroting" in calls[0][1]["system_instruction"]
    assert calls[0][1]["response_schema"]["required"] == ["text"]
