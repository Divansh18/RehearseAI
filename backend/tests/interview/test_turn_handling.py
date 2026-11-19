"""Turn aggregation and cancellation races on the assigned path.

These are the shared-behaviour cases of the earlier plan-less suites, restated for assigned
sessions: how several speech segments become one turn and one decision, how a partial turn waits
for its continuation, and that a stop or correction can never be overtaken by stale work.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from support import plan_dict

from rehearse_ai.interview.controller import Stage
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.voice.pipeline import InterviewInput

PLAN = plan_dict()
FIRST = PLAN["questions"][0]["question"]
SECOND = PLAN["questions"][1]["question"]


def deliver(session):
    utterance = session.controller.pending
    context = f"turn-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def settle():
    for _ in range(4):
        await asyncio.sleep(0)


def final(text, number):
    return TranscriptionFrame(
        text=text, user_id="candidate", timestamp=f"segment-{number}", finalized=False
    )


def make(tmp_path, name, infer, *, speak=None):
    adapter = InterviewInput()
    frames = []

    async def push(frame, _direction=FrameDirection.DOWNSTREAM):
        frames.append(frame)

    adapter.push_frame = push
    session = InterviewSession(
        SessionLog(tmp_path, name),
        infer,
        speak or adapter.speak,
        lambda _event: None,
        interview_plan=PLAN,
        opening=False,
    )
    adapter.session = session
    return session, adapter, frames


@pytest.mark.asyncio
async def test_several_final_segments_make_one_turn_and_one_decision(tmp_path):
    calls = []

    async def infer(kind, payload):
        calls.append(kind)
        return {"action": "advance", "text": ""}

    session, adapter, _frames = make(tmp_path, "segments", infer)
    await session.start()
    deliver(session)
    # A final segment can arrive before the turn-start frame; a duplicate stop must not infer twice.
    for frame in (
        final("I built an ingestion service", 1),
        UserStartedSpeakingFrame(),
        final("for scheduling interviews", 2),
        final("using Python and Postgres", 3),
        UserStoppedSpeakingFrame(),
        UserStoppedSpeakingFrame(),
    ):
        await adapter.process_frame(frame, FrameDirection.DOWNSTREAM)
    await settle()
    assert session.controller.answers[0] == (
        "I built an ingestion service for scheduling interviews using Python and Postgres",
    )
    assert calls == ["assigned_decision"]
    assert session.controller.pending.text == SECOND
    await session.close()


@pytest.mark.asyncio
async def test_a_fragment_after_a_follow_up_waits_for_its_continuation(tmp_path):
    actions = iter(
        [
            {"action": "follow_up", "text": "What did the service do?"},
            {"action": "wait", "text": ""},
            {"action": "advance", "text": ""},
        ]
    )
    spoken = []
    payloads = []

    async def infer(_kind, payload):
        payloads.append(payload)
        return next(actions)

    async def speak(utterance):
        spoken.append(utterance)

    session, _adapter, _frames = make(tmp_path, "fragment", infer, speak=speak)
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built an ingestion service")
    await settle()
    assert spoken[-1].text == "What did the service do?"
    deliver(session)

    await session.user_started()
    await session.user_stopped("So the idea", segment_count=1)
    await settle()
    assert session.controller.stage is Stage.AWAITING_PROPOSAL  # waiting, nothing spoken
    assert spoken[-1].text == "What did the service do?"

    await session.user_started()
    await session.user_stopped("was to schedule interviews using Python", segment_count=1)
    await settle()
    assert session.controller.answers[0][-1] == (
        "So the idea was to schedule interviews using Python"
    )
    assert spoken[-1].text == SECOND
    assert len(payloads) == 3
    assert payloads[1]["remaining_follow_ups"] == 0
    await session.close()


@pytest.mark.asyncio
async def test_the_decision_sees_the_whole_answer_after_a_nudge(tmp_path):
    payloads = []

    async def infer(_kind, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return {"action": "nudge", "text": "Tell me more about that."}
        return {"action": "advance", "text": ""}

    session, adapter, _frames = make(tmp_path, "accumulated", infer)
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I'm proud of the ingestion service.")
    await settle()
    deliver(session)
    segments = ["It validates each document", "and stores a report", "within a minute."]
    for number, text in enumerate(segments, 1):
        await adapter.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await adapter.process_frame(final(text, number), FrameDirection.DOWNSTREAM)
        await adapter.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await settle()
    assert len(payloads) >= 2
    seen = payloads[-1]
    assert seen["current_question_answer_before_latest_turn"].startswith(
        "I'm proud of the ingestion service."
    )
    assert (
        "within a minute."
        in seen["latest_candidate_turn"] + seen["current_question_answer_before_latest_turn"]
    )
    await session.close()


@pytest.mark.asyncio
async def test_a_stop_overtakes_a_decision_that_ignores_cancellation(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def infer(kind, _payload):
        calls.append(kind)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass  # a provider call that swallows cancellation
            return {"action": "advance", "text": ""}
        return {"action": "advance", "text": ""}

    session, _adapter, _frames = make(tmp_path, "stubborn", infer)
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built an ingestion service.")
    await started.wait()
    await session.user_started()
    await session.user_stopped("I want to end the interview.")
    release.set()
    await settle()
    assert session.controller.stage in {Stage.DELIVERING, Stage.ENDED_EARLY}
    assert session.controller.pending.text.startswith("Thank you for your time")
    assert session.controller.pending.text != SECOND  # the late "advance" never plays
    await session.close()


@pytest.mark.asyncio
async def test_a_stop_between_the_marker_and_speech_discards_the_question(tmp_path):
    marker_sent = asyncio.Event()
    release = asyncio.Event()
    frames = []

    async def infer(_kind, _payload):
        return {"action": "advance", "text": ""}

    session, adapter, _ = make(tmp_path, "marker-race", infer)

    async def push(frame, _direction=FrameDirection.DOWNSTREAM):
        frames.append(frame)
        if type(frame).__name__ == "UtteranceMarkerFrame" and frame.utterance_id == 2:
            marker_sent.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    adapter.push_frame = push
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built an ingestion service.")
    await marker_sent.wait()
    await session.user_started()
    await session.user_stopped("Can I end this interview?")
    release.set()
    await settle()
    assert not any(isinstance(f, TTSSpeakFrame) and f.text == SECOND for f in frames)
    assert session.controller.pending.text.startswith("Thank you for your time")
    await session.close()


@pytest.mark.asyncio
async def test_a_stop_while_a_prepared_follow_up_is_unheard_discards_it(tmp_path):
    spoken = []

    async def infer(_kind, _payload):
        return {"action": "follow_up", "text": "Which component did you own?"}

    async def speak(utterance):
        spoken.append(utterance)

    session, _adapter, _frames = make(tmp_path, "stop-output", infer, speak=speak)
    await session.start()
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built an ingestion service.")
    await settle()
    cancelled = spoken[-1]
    assert cancelled.text == "Which component did you own?"
    await session.user_started()
    session.bot_stopped()  # Pipecat can emit this after cancellation
    await session.user_stopped("Can we end the interview?")
    await settle()
    assert not session.controller.delivery_finished(cancelled.id)
    assert spoken[-1].text.startswith("Thank you for your time")
    await session.close()
