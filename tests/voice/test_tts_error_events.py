"""Exercise conversational controls through the Pipecat input adapter."""

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

from rehearse_ai.interview.controller import Stage
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.voice.pipeline import InterviewInput


def harness(tmp_path, infer=None):
    spoken = []
    calls = []

    async def default_infer(kind, payload):
        calls.append((kind, payload))
        if kind == "opening_ack":
            return {"text": "Thanks for sharing."}
        if kind == "feedback":
            return {"lines": ["Thanks for practicing."]}
        return {"action": "wait", "text": "", "project_status": "unclear"}

    log = SessionLog(tmp_path, "routing")

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        log, infer or default_infer, speak, lambda _: None, interview_plan=plan_dict()
    )
    adapter = InterviewInput()
    adapter.session = session
    adapter.push_frame = lambda *_args, **_kwargs: asyncio.sleep(0)
    return session, adapter, spoken, calls, log


async def turn(adapter, *segments):
    await adapter.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    for index, text in enumerate(segments):
        await adapter.process_frame(
            TranscriptionFrame(
                text=text, user_id="candidate", timestamp=str(index), finalized=True
            ),
            FrameDirection.DOWNSTREAM,
        )
    await adapter.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def deliver(session):
    utterance = session.controller.pending
    context = f"context-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


@pytest.mark.asyncio
async def test_tts_no_audio_error_is_correlated_and_not_mistaken_for_delivery(tmp_path):
    session, adapter, spoken, _, log = harness(tmp_path)
    await session.start()
    first = session.controller.pending
    context = "a31d57c2-7219-4345-9f5b-0e0286095643"
    session.tts_started(first.id, context)
    session.pipeline_error(
        processor="OpenAITTSService",
        category="unknown",
        error=f"TTS context {context} completed with no audio",
    )
    assert session.controller.pending == first
    assert session.controller.stage is Stage.DELIVERING
    await turn(adapter, "Can you repeat that?")
    assert spoken[-1].id != first.id
    session.pipeline_error(
        processor="OpenAITTSService",
        category="unknown",
        error=f"TTS context {context} completed with no audio",
    )
    await session.close()
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    errors = [event for event in events if event["event"] == "response_error"]
    assert errors[0]["error_code"] == "tts_context_no_audio"
    assert errors[0]["utterance_id"] == first.id
    assert errors[0]["recoverable"] is True
    assert any(event["event"] == "stale_pipeline_error_ignored" for event in events)
    assert not any(
        event["event"] == "agent_delivery_reported" and event["utterance_id"] == first.id
        for event in events
    )
