import asyncio

import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from support import plan_dict

from rehearse_ai.interview.controller import Stage, UtteranceKind
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.voice.pipeline import InterviewInput


def deliver(session):
    utterance = session.controller.pending
    context = f"context-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def start_questions(session):
    await session.start()


def begin_output(session):
    utterance = session.controller.pending
    context = f"context-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def final(text, number):
    return TranscriptionFrame(
        text=text, user_id="candidate", timestamp=f"segment-{number}", finalized=False
    )


@pytest.mark.asyncio
async def test_media_pause_discards_partial_segments_without_advancing(tmp_path):
    decisions = []
    input_adapter = InterviewInput()

    async def push(_frame, _direction=FrameDirection.DOWNSTREAM):
        pass

    async def cancel():
        pass

    async def infer(kind, payload):
        decisions.append((kind, payload))
        return {"action": "advance", "text": ""}

    input_adapter.push_frame = push
    session = InterviewSession(
        SessionLog(tmp_path, "media-partial"),
        infer,
        input_adapter.speak,
        lambda _: None,
        cancel_audio=cancel,
        interview_plan=plan_dict(),
        opening=False,
    )
    input_adapter.session = session
    await start_questions(session)
    deliver(session)
    await input_adapter.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await input_adapter.process_frame(final("I built the first half", 1), FrameDirection.DOWNSTREAM)
    input_adapter.set_media_paused(True)
    await session.media_pause("screen_sharing")
    assert input_adapter.request_resume() is False
    await input_adapter.process_frame(final("stale text", 2), FrameDirection.DOWNSTREAM)
    assert session.controller.answers[0] == ()
    assert decisions == []
    await input_adapter.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert not session.media_paused
    await input_adapter.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await input_adapter.process_frame(final("I built a parser", 3), FrameDirection.DOWNSTREAM)
    await input_adapter.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await settle()
    assert session.controller.answers[0] == ("I built a parser",)
    assert len(decisions) == 1
    await session.close()


@pytest.mark.asyncio
async def test_media_pause_cancels_output_and_resume_requeues_fresh_id(tmp_path):
    spoken = []
    published = []

    async def speak(utterance):
        spoken.append(utterance)

    async def cancel():
        pass

    session = InterviewSession(
        SessionLog(tmp_path, "media-output"),
        lambda *_: None,
        speak,
        published.append,
        cancel_audio=cancel,
        interview_plan=plan_dict(),
        opening=False,
    )
    await start_questions(session)
    original = session.controller.pending
    begin_output(session)
    await session.media_pause("camera")
    assert session.media_paused
    assert session.controller.pending is None
    session.tts_finished(f"context-{original.id}")
    session.bot_stopped()
    assert not any(
        entry["event"]["event"] == "agent_delivery_reported"
        and entry["event"]["utterance_id"] == original.id
        for entry in published
    )
    assert not session.tts_audio_chunk(f"context-{original.id}", 320)
    await session.media_resume()
    replacement = session.controller.pending
    assert replacement.id != original.id
    assert replacement.text == original.text
    deliver(session)
    assert session.controller.current_question_number == 1
    assert (
        sum(
            entry["event"]["event"] == "agent_delivery_reported"
            and entry["event"]["utterance_id"] == replacement.id
            for entry in published
        )
        == 1
    )
    await session.close()


@pytest.mark.asyncio
async def test_split_stt_correction_updates_memory_without_answer_or_decision(tmp_path):
    input_adapter = InterviewInput()
    input_adapter.push_frame = lambda _frame, _direction=FrameDirection.DOWNSTREAM: settle()

    async def infer(_kind, _payload):
        raise AssertionError("A correction alone is not an interview answer")

    session = InterviewSession(
        SessionLog(tmp_path, "split-correction"),
        infer,
        input_adapter.speak,
        lambda _: None,
        interview_plan=plan_dict(),
        opening=False,
    )
    input_adapter.session = session
    await start_questions(session)
    deliver(session)
    for frame in (
        final("I said webhooks,", 1),
        UserStartedSpeakingFrame(),
        final("not web books.", 2),
        UserStoppedSpeakingFrame(),
    ):
        await input_adapter.process_frame(frame, FrameDirection.DOWNSTREAM)
    assert session.controller.memory.get("technical_terms").value == "webhooks"
    assert session.controller.answers[0] == ()
    assert session.controller.remaining_follow_ups == 1
    assert session.controller.pending.kind is UtteranceKind.CORRECTION_ACK
    await session.close()


@pytest.mark.asyncio
async def test_stale_llm_result_after_continuation_cannot_advance(tmp_path):
    release_old = asyncio.Event()
    calls = 0
    spoken = []

    async def infer(_kind, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await release_old.wait()
            except asyncio.CancelledError:
                await release_old.wait()
            return {"action": "advance", "text": ""}
        return {"action": "wait", "text": ""}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "stale"),
        infer,
        speak,
        lambda _: None,
        interview_plan=plan_dict(),
        opening=False,
    )
    await start_questions(session)
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built an API")
    await settle()
    await session.user_started()
    await session.user_stopped("and owned its deployment")
    await settle()
    release_old.set()
    await settle()
    assert session.controller.stage is Stage.AWAITING_PROPOSAL
    assert len(spoken) == 1  # Question one only: the stale decision spoke nothing.
    assert session.controller.answers[0] == ("I built an API and owned its deployment",)
    await session.close()
