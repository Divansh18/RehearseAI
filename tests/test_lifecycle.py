import asyncio
import json

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from support import plan_dict

from rehearse_ai.interview.controller import Stage
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.voice.pipeline import AudioObserver, UtteranceMarkerFrame


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def begin_output(session):
    utterance = session.controller.pending
    context = f"context-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    return context


def finish_output(session, context):
    session.tts_finished(context)
    session.bot_stopped()


async def start_questions(session):
    await session.start()
    finish_output(session, begin_output(session))
    session.controller.submit_greeting("I'm doing well")
    await session._queue_speech(session.controller.prepare_readiness("Good to hear."))
    finish_output(session, begin_output(session))
    await session._queue_speech(session.controller.submit_readiness(True))


def events(log):
    return [json.loads(line) for line in log.path.read_text().splitlines()]


def final_segment(text, number):
    return TranscriptionFrame(
        text=text, user_id="candidate", timestamp=f"segment-{number}", finalized=False
    )


async def feed_turn(adapter, segments):
    await adapter.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    for number, segment in enumerate(segments, start=1):
        await adapter.process_frame(final_segment(segment, number), FrameDirection.DOWNSTREAM)
    await adapter.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_audio_observer_correlates_tts_frames_with_utterance_id(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("No inference expected")

    async def speak(_utterance):
        pass

    log = SessionLog(tmp_path, "audio-ids")
    session = InterviewSession(log, infer, speak, lambda _: None, interview_plan=plan_dict())
    observer = AudioObserver()
    observer.session = session

    async def forward(_frame, _direction=FrameDirection.DOWNSTREAM):
        pass

    observer.push_frame = forward
    await session.start()
    utterance_id = session.controller.pending.id
    session.tts_context_assigned(utterance_id, "tts-context")
    for frame in (
        UtteranceMarkerFrame(utterance_id=utterance_id),
        TTSStartedFrame(context_id="tts-context"),
        TTSAudioRawFrame(b"\0" * 32, 24000, 1, context_id="tts-context"),
        TTSStoppedFrame(context_id="tts-context"),
    ):
        await observer.process_frame(frame, FrameDirection.DOWNSTREAM)
    names = [row["event"] for row in events(log)]
    assert "tts_started" in names
    assert names.count("tts_first_audio_generated") == 1
    assert any(
        row["event"] == "tts_generation_finished"
        and row["utterance_id"] == utterance_id
        and row["audio_bytes"] == 32
        and row["audio_chunks"] == 1
        for row in events(log)
    )
    assert session.controller.stage is Stage.DELIVERING
    await session.close()


@pytest.mark.asyncio
async def test_audio_from_cancelled_tts_context_is_not_forwarded(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("No inference expected")

    async def speak(_utterance):
        pass

    log = SessionLog(tmp_path, "late-audio")
    session = InterviewSession(log, infer, speak, lambda _: None, interview_plan=plan_dict())
    observer = AudioObserver()
    observer.session = session
    forwarded = []

    async def forward(frame, _direction=FrameDirection.DOWNSTREAM):
        forwarded.append(frame)

    observer.push_frame = forward
    await session.start()
    first = session.controller.pending
    session.tts_context_assigned(first.id, "old")
    await observer.process_frame(UtteranceMarkerFrame(first.id), FrameDirection.DOWNSTREAM)
    await observer.process_frame(TTSStartedFrame(context_id="old"), FrameDirection.DOWNSTREAM)
    await session.user_started()
    await observer.process_frame(
        TTSAudioRawFrame(b"\x01" * 32, 24000, 1, context_id="old"),
        FrameDirection.DOWNSTREAM,
    )
    assert not any(isinstance(frame, TTSAudioRawFrame) for frame in forwarded)
    assert not any(
        row["event"] == "tts_first_audio_generated" and row["utterance_id"] == first.id
        for row in events(log)
    )
    assert any(row["event"] == "stale_tts_audio_discarded" for row in events(log))
    await session.close()


@pytest.mark.asyncio
async def test_late_cancelled_tts_stop_cannot_finish_fresh_output(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("No inference expected")

    async def speak(_utterance):
        pass

    log = SessionLog(tmp_path, "late-stop")
    session = InterviewSession(log, infer, speak, lambda _: None, interview_plan=plan_dict())
    observer = AudioObserver()
    observer.session = session
    forwarded_stops = []

    class Output:
        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            if direction is FrameDirection.DOWNSTREAM and isinstance(
                frame, BotStoppedSpeakingFrame
            ):
                session.bot_stopped()

    sender = BaseOutputTransport.MediaSender(
        Output(),
        destination=None,
        sample_rate=24000,
        audio_chunk_size=4800,
        params=TransportParams(),
    )

    async def forward(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSStoppedFrame):
            forwarded_stops.append(frame.context_id)
            await sender._handle_frame(frame)

    observer.push_frame = forward
    await session.start()
    first = session.controller.pending
    session.tts_context_assigned(first.id, "old")
    await observer.process_frame(TTSStartedFrame(context_id="old"), FrameDirection.DOWNSTREAM)
    await observer.process_frame(
        TTSAudioRawFrame(b"\x01" * 32, 24000, 1, context_id="old"),
        FrameDirection.DOWNSTREAM,
    )
    session.bot_started()
    await session.user_started()
    session.bot_stopped()  # Pipecat's interruption stop retires A's stop debt.

    second = session.controller.retry_interrupted()
    await session._queue_speech(second)
    session.tts_context_assigned(second.id, "new")
    await observer.process_frame(TTSStartedFrame(context_id="new"), FrameDirection.DOWNSTREAM)
    await observer.process_frame(
        TTSAudioRawFrame(b"\x02" * 32, 24000, 1, context_id="new"),
        FrameDirection.DOWNSTREAM,
    )
    session.bot_started()
    sender._bot_speaking = True
    sender._tts_audio_received = True

    await observer.process_frame(
        TTSAudioRawFrame(b"\x03" * 32, 24000, 1, context_id="old"),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(TTSStoppedFrame(context_id="old"), FrameDirection.DOWNSTREAM)
    await observer.process_frame(TTSStoppedFrame(context_id="new"), FrameDirection.DOWNSTREAM)
    await observer.process_frame(TTSStoppedFrame(context_id="new"), FrameDirection.DOWNSTREAM)

    deliveries = [row for row in events(log) if row["event"] == "agent_delivery_reported"]
    assert forwarded_stops == ["new"]
    assert [row["utterance_id"] for row in deliveries] == [second.id]
    assert session.controller.stage is Stage.AWAITING_GREETING
    assert not any(row["utterance_id"] == first.id for row in deliveries)
    assert any(row["event"] == "stale_tts_audio_discarded" for row in events(log))
    assert len([row for row in events(log) if row["event"] == "stale_tts_stop_discarded"]) == 2
    await session.close()


@pytest.mark.asyncio
async def test_fresh_output_retires_missing_cancelled_stop_debt(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("No inference expected")

    async def speak(_utterance):
        pass

    log = SessionLog(tmp_path, "stop-debt")
    session = InterviewSession(log, infer, speak, lambda _: None, interview_plan=plan_dict())
    await session.start()
    first = session.controller.pending
    session.tts_started(first.id, "old")
    session.tts_first_audio("old")
    session.bot_started()
    await session.user_started()  # No cancellation BotStopped callback is observed.

    second = session.controller.retry_interrupted()
    await session._queue_speech(second)
    session.tts_started(second.id, "new")
    session.tts_first_audio("new")
    session.bot_started()
    session.tts_finished("new")
    session.bot_stopped()

    deliveries = [row for row in events(log) if row["event"] == "agent_delivery_reported"]
    assert [row["utterance_id"] for row in deliveries] == [second.id]
    assert any(row["event"] == "cancelled_output_stop_debt_retired" for row in events(log))
    await session.close()
