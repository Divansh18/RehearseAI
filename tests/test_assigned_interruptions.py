"""Stage B: interruption handling and response-latency instrumentation on the assigned path.

Synthetic conversations with an injected clock. These verify routing and state; they say nothing
about how reliably a real microphone, VAD or browser behaves.
"""

import asyncio
import json

import pytest
from support import plan_dict

from rehearse_ai.interview.controller import Stage, UtteranceKind
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog

CLOSING = "Thank you for your time. The hiring team will share updates."


def deliver(session):
    utterance = session.controller.pending
    context = f"int-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


def begin_output(session):
    utterance = session.controller.pending
    context = f"int-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    return context


async def settle(times=8):
    for _ in range(times):
        await asyncio.sleep(0)


class Rig:
    def __init__(self, tmp_path, *, decisions=None, minutes=20):
        self.now = [500.0]
        self.spoken, self.calls = [], []
        self.decisions = list(decisions or [])
        self.gate = None  # an Event the next decision waits on
        self.model_seconds = 0.0

        async def infer(kind, payload):
            self.calls.append((kind, payload))
            if kind == "assigned_closing":
                return {"text": CLOSING}
            if kind == "assigned_decision":
                if self.gate is not None:
                    gate = self.gate
                    self.gate = None
                    await gate.wait()
                self.now[0] += self.model_seconds
                if self.decisions:
                    return self.decisions.pop(0)
            return {"action": "advance", "text": "", "covered_future_questions": []}

        async def speak(utterance):
            self.spoken.append(utterance)

        self.session = InterviewSession(
            SessionLog(tmp_path, "int"),
            infer,
            speak,
            lambda _event: None,
            interview_plan=plan_dict(target_minutes=minutes),
            opening=False,
            clock=lambda: self.now[0],
        )
        self.log = tmp_path / "int.jsonl"

    @property
    def controller(self):
        return self.session.controller

    @property
    def last(self):
        return self.spoken[-1]

    def rows(self, event=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if event is None or row["event"] == event]

    def decision_calls(self):
        return [c for c in self.calls if c[0] == "assigned_decision"]

    async def say(self, text):
        await self.session.user_started()
        await self.session.user_stopped(text)
        await settle()

    async def start(self):
        await self.session.start()
        deliver(self.session)


@pytest.mark.asyncio
async def test_interrupting_model_generation_discards_the_stale_decision(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    rig.gate = asyncio.Event()  # the first decision is "still thinking"
    await rig.say("I built the ingestion service.")
    assert len(rig.decision_calls()) == 1
    await rig.say("and I owned the queue workers.")  # the candidate carries on
    rig.gate.set() if rig.gate else None
    await settle()
    assert rig.rows("inference_cancel_requested")
    # Only the continuation's decision produced speech, and it saw both parts exactly once.
    next_prompts = [u for u in rig.spoken if u.kind is UtteranceKind.QUESTION]
    assert [u.question_number for u in next_prompts] == [1, 2]
    assert rig.controller.answers[0] == (
        "I built the ingestion service. and I owned the queue workers.",
    ) or (
        " ".join(rig.controller.answers[0]).count("ingestion") == 1
        and " ".join(rig.controller.answers[0]).count("queue workers") == 1
    )
    assert (
        len([r for r in rig.rows("inference_completed") if r["purpose"] == "assigned_decision"])
        == 1
    )


@pytest.mark.asyncio
async def test_interrupting_before_first_audio_recovers_the_prompt_once_without_a_second_probe(
    tmp_path,
):
    rig = Rig(
        tmp_path,
        decisions=[
            {
                "action": "follow_up",
                "text": "Which part was hardest?",
                "covered_future_questions": [],
            }
        ],
    )
    await rig.start()
    await rig.say("I built the ingestion service.")
    probe = rig.last
    assert probe.kind is UtteranceKind.FOLLOW_UP
    old_context = f"int-{probe.id}"
    rig.session.tts_started(probe.id, old_context)  # speech requested, no audio yet
    await rig.session.user_started()
    interrupted = rig.rows("agent_interrupted")[-1]
    assert (
        interrupted["output_started"] is False
        and interrupted["cancellation_reason"] == "candidate_speech_onset"
    )
    await rig.session.user_stopped("Sorry, one more detail about retries.")
    await settle()
    assert rig.rows("unheard_prompt_recovery")
    replay = rig.last
    assert replay.kind is UtteranceKind.FOLLOW_UP and replay.id != probe.id
    assert replay.text == probe.text
    # The old context cannot produce audio or an output-start for the discarded utterance.
    rig.session.tts_first_audio(old_context)
    rig.session.bot_started()
    started = [r["utterance_id"] for r in rig.rows("agent_output_started")]
    assert probe.id not in started
    # One probe was allowed and one was spent; recovery did not spend another.
    assert rig.controller.remaining_follow_ups == 0
    assert (
        len([u for u in rig.spoken if u.kind is UtteranceKind.FOLLOW_UP]) == 2
    )  # original + replay
    assert len(rig.decision_calls()) == 1


@pytest.mark.asyncio
async def test_interrupting_after_enough_audio_accepts_the_answer_once_and_never_resumes(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("I built the ingestion service.")  # advances to question two
    question = rig.last
    assert question.question_number == 2
    begin_output(rig.session)
    rig.now[0] += 4.0  # four seconds of the question were played
    calls_before = len(rig.decision_calls())
    await rig.say("We retried with idempotent workers.")
    event = rig.rows("agent_interrupted")[-1]
    assert event["output_started"] is True and event["heard_enough"] is True
    assert 3900 <= event["heard_ms"] <= 4100
    assert rig.rows("barge_in_answer_accepted")
    assert len(rig.decision_calls()) == calls_before + 1  # the continuation is processed once
    assert "idempotent workers" in " ".join(rig.controller.answers[1])
    # The interrupted audio finishing later is ignored: no delivery, no resumption.
    rig.session.bot_stopped()
    assert rig.rows("cancelled_output_stop_ignored")
    assert question.id not in [r["utterance_id"] for r in rig.rows("agent_delivery_reported")]
    assert len([u for u in rig.spoken if u.text == question.text]) == 1


@pytest.mark.asyncio
async def test_a_barely_heard_question_is_asked_again_and_the_words_are_not_an_answer(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("I built the ingestion service.")
    question = rig.last
    begin_output(rig.session)
    rig.now[0] += 0.4  # as in the real session: talked over within the first half second
    calls_before = len(rig.decision_calls())
    await rig.say("I also owned the retry logic.")
    event = rig.rows("agent_interrupted")[-1]
    assert event["output_started"] is True and event["heard_enough"] is False
    # Nothing was decided about question two: it is asked again, with its probe allowance intact.
    assert len(rig.decision_calls()) == calls_before
    assert rig.last.kind is UtteranceKind.QUESTION and rig.last.text == question.text
    assert rig.controller.answers[1] == ()
    assert rig.controller.remaining_follow_ups >= 1
    # The words went to the answer they continue, not to the unheard question.
    assert "retry logic" in " ".join(rig.controller.answers[0])


@pytest.mark.asyncio
async def test_floor_control_over_a_barely_heard_prompt_replays_it_and_records_nothing(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("I built the ingestion service.")
    question = rig.last
    begin_output(rig.session)
    rig.now[0] += 0.46
    calls_before = len(rig.decision_calls())
    answers_before = [tuple(a) for a in rig.controller.answers]
    await rig.say("Yes. Please cut.")
    assert rig.rows("floor_control_replay")
    assert rig.last.text == question.text and rig.last.id != question.id
    assert len(rig.decision_calls()) == calls_before
    assert [tuple(a) for a in rig.controller.answers] == answers_before
    # The same words after a prompt that was properly heard are a real reply.
    begin_output(rig.session)
    rig.now[0] += 5
    await rig.say("Yes.")
    assert len(rig.decision_calls()) == calls_before + 1


async def reach_closing(rig):
    """Exhaust the time budget so the next answer leads straight to the closing."""
    await rig.start()
    rig.now[0] += 100000
    await rig.say("I built the ingestion service.")
    assert rig.last.kind is UtteranceKind.CLOSING


@pytest.mark.asyncio
async def test_speech_over_a_heard_closing_ends_the_interview_without_resuming_it(tmp_path):
    rig = Rig(tmp_path, minutes=5)
    await reach_closing(rig)
    closing = rig.last
    begin_output(rig.session)
    rig.now[0] += 3
    await rig.say("Thanks, bye!")
    assert rig.controller.stage is Stage.COMPLETE
    assert len(rig.rows("interview_complete")) == 1 and rig.rows("closing_interrupted_not_resumed")
    assert [u for u in rig.spoken if u.kind is UtteranceKind.CLOSING] == [closing]  # not replayed
    names = [r["event"] for r in rig.rows()]
    assert names.index("coverage_summary") < names.index("interview_complete")
    rig.session.bot_stopped()  # the cancelled audio's stop is ignored; no second completion
    assert len(rig.rows("interview_complete")) == 1


@pytest.mark.asyncio
async def test_a_barely_heard_closing_is_replayed_once_then_ends(tmp_path):
    rig = Rig(tmp_path, minutes=5)
    await reach_closing(rig)
    begin_output(rig.session)
    rig.now[0] += 0.3
    await rig.say("Sorry, just finishing my point.")
    assert rig.rows("closing_replayed_after_early_interruption")
    assert rig.last.kind is UtteranceKind.CLOSING and rig.controller.stage is Stage.DELIVERING
    begin_output(rig.session)
    rig.now[0] += 0.3
    await rig.say("And one more thing.")
    assert rig.controller.stage is Stage.COMPLETE  # replayed once only
    assert len([u for u in rig.spoken if u.kind is UtteranceKind.CLOSING]) == 2


@pytest.mark.asyncio
async def test_speech_over_an_early_end_goodbye_still_ends_the_interview(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("Please end the interview.")
    assert rig.last.kind is UtteranceKind.EARLY_FEEDBACK
    begin_output(rig.session)
    rig.now[0] += 3
    await rig.say("Okay, bye.")
    assert rig.controller.stage is Stage.ENDED_EARLY
    assert len(rig.rows("interview_ended_early")) == 1
    assert rig.rows("coverage_summary")[-1]["summary"]["end_reason"] == "ended_early"


@pytest.mark.asyncio
async def test_media_loss_is_a_distinct_pause_that_replays_the_prompt_and_freezes_the_clock(
    tmp_path,
):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("I built the ingestion service.")
    question = rig.last
    begin_output(rig.session)
    rig.now[0] += 2
    await rig.session.media_pause("camera_lost")
    paused = rig.rows("agent_interrupted")[-1]
    assert paused["cancellation_reason"] == "media_lost"
    calls = len(rig.decision_calls())
    rig.now[0] += 600  # ten minutes with the device gone
    await rig.say("This speech arrives during the pause.")  # ignored: nothing is listening
    assert len(rig.decision_calls()) == calls and rig.controller.answers[1] == ()
    await rig.session.media_resume()
    await settle()
    assert rig.last.text == question.text and rig.last.id != question.id  # replayed once
    assert rig.controller.remaining_follow_ups >= 1
    assert rig.session._clock.paused_seconds == 600
    # A conversational interruption on the same prompt is recorded as something different.
    begin_output(rig.session)
    rig.now[0] += 4
    await rig.say("We retried failed jobs.")
    assert rig.rows("agent_interrupted")[-1]["cancellation_reason"] == "candidate_speech_onset"


@pytest.mark.asyncio
async def test_response_latency_breaks_the_wait_into_server_side_stages(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    session, now = rig.session, rig.now
    rig.model_seconds = 1.3
    await session.user_started()
    now[0] += 3.0
    session.turn_signal("vad_stopped", frame_id=1, stop_secs=0.35)  # speech ends (estimate)
    now[0] += 0.9
    session.transcript_segment(
        "I built the ingestion service.", frame_timestamp="t", finalized=False, frame_id=2
    )
    now[0] += 0.55
    await session.user_stopped("I built the ingestion service.")  # 1.45 s after the VAD stop
    await settle()
    prepared = rig.last
    context = f"int-{prepared.id}"
    session.tts_started(prepared.id, context)
    now[0] += 0.12
    session.tts_first_audio(context)
    now[0] += 0.01
    session.bot_started()
    row = rig.rows("response_latency")[-1]
    assert row["utterance_id"] == prepared.id
    assert row["stt_final_after_speech_end_ms"] == 900
    assert row["speech_end_to_turn_complete_ms"] == 1450
    assert row["last_stt_to_turn_complete_ms"] == 550
    assert row["model_ms"] == 1300
    assert row["prepared_to_tts_first_audio_ms"] == 120
    assert row["tts_first_audio_to_output_started_ms"] == 10
    assert row["speech_end_to_output_started_ms"] == 1450 + 1300 + 120 + 10
    assert "not browser playback" in row["basis"]
    # Once reported it is not repeated for the next utterance of the same turn.
    assert len(rig.rows("response_latency")) == 1


@pytest.mark.asyncio
async def test_a_transcript_that_arrives_after_the_boundary_shows_as_negative_wait(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    session, now = rig.session, rig.now
    await session.user_started()
    session.turn_signal("vad_stopped", frame_id=1, stop_secs=0.35)
    now[0] += 1.1
    await session.user_stopped("I built the ingestion service.", segment_count=0)
    now[0] += 0.4
    session.transcript_segment("late", frame_timestamp="t", finalized=True, frame_id=2)
    # The measurement keeps whatever was observed: the late transcript is visible as negative.
    assert session._marks["last_stt"] - session._marks["turn_complete"] == pytest.approx(0.4)
