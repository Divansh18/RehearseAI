"""Stage A: transcripts delivered after the turn boundary they belong to (seen in a real session).

Pipecat's turn boundary is a system frame and can overtake the transcript (a data frame) that
caused it. Frame ids reveal the true order, so late transcripts are placed by creation order.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from support import plan_dict

from rehearse_ai.pipeline import InterviewInput
from rehearse_ai.session import InterviewSession
from rehearse_ai.session_log import SessionLog

DOWN = FrameDirection.DOWNSTREAM


def final(text, number=1, *, result=None):
    frame = TranscriptionFrame(
        text=text, user_id="candidate", timestamp=f"segment-{number}", finalized=False
    )
    if result is not None:
        frame.result = result
    return frame


def deliver(session):
    utterance = session.controller.pending
    context = f"late-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def settle(times=8):
    for _ in range(times):
        await asyncio.sleep(0)


class Rig:
    def __init__(self, tmp_path, grace=0.05):
        self.adapter = InterviewInput(late_grace_secs=grace)
        self.calls, self.spoken = [], []

        async def push(frame, direction=DOWN):
            return None

        self.adapter.push_frame = push

        async def infer(kind, payload):
            self.calls.append((kind, payload))
            return {"action": "advance", "text": "", "covered_future_questions": []}

        async def speak(utterance):
            self.spoken.append(utterance)

        self.session = InterviewSession(
            SessionLog(tmp_path, "late"),
            infer,
            speak,
            lambda _event: None,
            interview_plan=plan_dict(),
            opening=False,
        )
        self.adapter.session = self.session
        self.log = tmp_path / "late.jsonl"

    async def start(self):
        await self.session.start()
        deliver(self.session)

    async def feed(self, *frames):
        for frame in frames:
            await self.adapter.process_frame(frame, DOWN)
        await settle()

    def rows(self, event):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if row["event"] == event]


@pytest.mark.asyncio
async def test_a_transcript_that_trails_its_own_boundary_belongs_to_that_turn(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    transcript = final("I built the ingestion service.")  # created first, so a lower id...
    started, stopped = UserStartedSpeakingFrame(), UserStoppedSpeakingFrame()
    assert transcript.id < stopped.id
    # ...but the boundary overtakes it, exactly as in the real session.
    await rig.feed(started, stopped)
    assert not rig.rows("candidate_final")  # the empty boundary is held, not processed
    await rig.feed(transcript)
    finals = rig.rows("candidate_final")
    assert len(finals) == 1 and finals[0]["text"] == "I built the ingestion service."
    assert finals[0]["segment_count"] == 1
    assert rig.rows("stt_final_segment")[0]["turn_correlation"] == "late_for_boundary"
    assert rig.session.controller.answers[0] == ("I built the ingestion service.",)
    assert rig.adapter._segments == []  # nothing is left to leak into a later turn
    assert len([c for c in rig.calls if c[0] == "assigned_decision"]) == 1


@pytest.mark.asyncio
async def test_an_empty_boundary_with_no_transcript_is_reported_empty_after_a_short_wait(tmp_path):
    rig = Rig(tmp_path, grace=0.02)
    await rig.start()
    await rig.feed(UserStartedSpeakingFrame(), UserStoppedSpeakingFrame())
    await asyncio.sleep(0.08)
    await settle()
    finals = rig.rows("candidate_final")
    assert len(finals) == 1 and finals[0]["segment_count"] == 0
    assert rig.session.controller.answers[0] == ()


@pytest.mark.asyncio
async def test_a_late_fragment_is_processed_now_not_merged_into_a_later_unrelated_turn(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    first = final("I built the ingestion service.", 1)
    trailing = final("Mostly with queues.", 2)  # created before the boundary...
    started, stopped = UserStartedSpeakingFrame(), UserStoppedSpeakingFrame()
    await rig.feed(started, first, stopped)  # the turn is routed with what it had
    assert rig.rows("candidate_final")[0]["text"] == "I built the ingestion service."
    await rig.feed(trailing)  # ...and arrives afterwards
    assert rig.rows("late_transcript_continuation")
    assert rig.rows("stt_final_segment")[-1]["turn_correlation"] == "late_after_boundary"
    assert rig.adapter._segments == []
    joined = " ".join(rig.session.controller.answers[0])
    assert "ingestion service" in joined and "Mostly with queues." in joined
    # A genuinely new turn later starts clean: nothing stale is prefixed to it.
    later = final("We also added metrics.", 3)
    await rig.feed(UserStartedSpeakingFrame(), later, UserStoppedSpeakingFrame())
    last = rig.rows("candidate_final")[-1]
    assert last["text"] == "We also added metrics." and last["segment_count"] == 1


@pytest.mark.asyncio
async def test_speaking_again_before_the_late_transcript_arrives_joins_the_open_turn(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    transcript = final("I built the ingestion service.")
    await rig.feed(UserStartedSpeakingFrame(), UserStoppedSpeakingFrame())  # boundary held
    await rig.feed(UserStartedSpeakingFrame())  # the candidate keeps talking
    await asyncio.sleep(0.1)  # the held boundary must not fire into the new turn
    assert not rig.rows("candidate_final")
    await rig.feed(transcript)  # the earlier speech's text joins the turn that is now open
    assert not rig.rows("candidate_final")
    await rig.feed(final("It used queues.", 2), UserStoppedSpeakingFrame())
    finals = rig.rows("candidate_final")
    assert len(finals) == 1 and finals[0]["segment_count"] == 2
    assert finals[0]["text"] == "I built the ingestion service. It used queues."


@pytest.mark.asyncio
async def test_ordinary_ordering_is_unchanged(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.feed(
        UserStartedSpeakingFrame(),
        final("I built", 1),
        final("the ingestion service.", 2),
        UserStoppedSpeakingFrame(),
    )
    finals = rig.rows("candidate_final")
    assert len(finals) == 1 and finals[0]["segment_count"] == 2
    assert {row["turn_correlation"] for row in rig.rows("stt_final_segment")} == {"active_turn"}
    # A transcript that arrives before the turn-start frame is still carried into that turn.
    await rig.feed(
        final("It used queues.", 3), UserStartedSpeakingFrame(), UserStoppedSpeakingFrame()
    )
    assert rig.rows("stt_final_segment")[-1]["turn_correlation"] == "pre_start_segment"
    assert rig.rows("candidate_final")[-1]["text"] == "It used queues."


@pytest.mark.asyncio
async def test_provider_confidence_and_audio_position_are_logged_when_available(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    result = SimpleNamespace(
        start=12.5,
        duration=2.25,
        channel=SimpleNamespace(alternatives=[SimpleNamespace(confidence=0.912)]),
    )
    await rig.feed(
        UserStartedSpeakingFrame(),
        final("I built the ingestion service.", 1, result=result),
        final("It used queues.", 2),  # no provider details: the fields are simply absent
        UserStoppedSpeakingFrame(),
    )
    first, second = rig.rows("stt_final_segment")
    assert first["stt_confidence"] == 0.912 and first["stt_audio_end_s"] == 14.75
    assert first["stt_audio_start_s"] == 12.5
    assert "stt_confidence" not in second


def test_details_are_read_from_the_real_deepgram_result_type():
    from deepgram.listen.v1.types import ListenV1Results
    from deepgram.listen.v1.types.listen_v1results_channel import ListenV1ResultsChannel
    from deepgram.listen.v1.types.listen_v1results_channel_alternatives_item import (
        ListenV1ResultsChannelAlternativesItem,
    )

    from rehearse_ai.pipeline import stt_details

    result = ListenV1Results(
        type="Results",
        channel_index=[0, 1],
        duration=2.25,
        start=12.5,
        is_final=True,
        speech_final=False,
        channel=ListenV1ResultsChannel(
            alternatives=[
                ListenV1ResultsChannelAlternativesItem(
                    transcript="hello world", confidence=0.912, words=[]
                )
            ]
        ),
        metadata={
            "request_id": "x",
            "model_info": {"name": "m", "version": "v", "arch": "a"},
            "model_uuid": "u",
        },
    )
    frame = TranscriptionFrame(text="hello world", user_id="c", timestamp="t", result=result)
    assert stt_details(frame) == {
        "stt_confidence": 0.912,
        "stt_audio_start_s": 12.5,
        "stt_audio_end_s": 14.75,
    }
    assert stt_details(TranscriptionFrame(text="x", user_id="c", timestamp="t")) == {}
