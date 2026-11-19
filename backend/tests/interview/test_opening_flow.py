"""Stage A: the assigned interview opening, using synthetic conversations (no audio, no models)."""

import asyncio
import json

import pytest
from support import plan_dict

from rehearse_ai.interview.controller import Stage, UtteranceKind
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog

ACK = "Glad to hear it."


def deliver(session):
    utterance = session.controller.pending
    context = f"opening-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


def begin_output(session):
    utterance = session.controller.pending
    context = f"opening-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()


async def settle(times=6):
    for _ in range(times):
        await asyncio.sleep(0)


class Rig:
    def __init__(self, tmp_path, *, name="Drake Example", minutes=20, wait=0.0, plan=None, ack=ACK):
        self.now = [100.0]
        self.spoken, self.calls = [], []
        self.ack = ack
        plan = plan or plan_dict(target_minutes=minutes)

        async def infer(kind, payload):
            self.calls.append((kind, payload))
            if kind == "opening_ack":
                if isinstance(self.ack, Exception):
                    raise self.ack
                return {"text": self.ack}
            if kind == "assigned_closing":
                return {"text": "Thank you for your time. The hiring team will share updates."}
            return {"action": "advance", "text": "", "covered_future_questions": []}

        async def speak(utterance):
            self.spoken.append(utterance)

        self.session = InterviewSession(
            SessionLog(tmp_path, "opening"),
            infer,
            speak,
            lambda _event: None,
            interview_plan=plan,
            self_reported_name=name,
            clock=lambda: self.now[0],
            opening_wait_secs=wait,
        )
        self.log = tmp_path / "opening.jsonl"
        self.plan = plan

    @property
    def controller(self):
        return self.session.controller

    def rows(self, event=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if event is None or row["event"] == event]

    async def say(self, text):
        await self.session.user_started()
        await self.session.user_stopped(text)
        await settle()

    async def begin(self):
        await self.session.start()
        deliver(self.session)  # greeting delivered

    @property
    def last(self):
        return self.spoken[-1]


@pytest.mark.asyncio
async def test_opens_with_the_confirmed_name_and_never_asks_for_or_infers_one(tmp_path):
    rig = Rig(tmp_path, name="Drake Example")
    await rig.session.start()
    assert rig.last.kind is UtteranceKind.GREETING
    assert rig.last.text == "Hi Drake, how's your day going?"
    assert rig.controller.preferred_name == "Drake Example"
    # No name was supplied: the greeting stays generic and nothing asks for a name.
    anon = Rig(tmp_path / "anon", name=None)
    (tmp_path / "anon").mkdir(exist_ok=True)
    await anon.session.start()
    assert anon.last.text == "Hi, how's your day going?"
    assert all(u.kind is not UtteranceKind.NAME_PROMPT for u in anon.spoken)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "greeting"),
    [
        ("Anne-Marie O'Neil", "Hi Anne-Marie, how's your day going?"),
        ("María José García", "Hi María, how's your day going?"),
        ("  Zoë   van der Berg ", "Hi Zoë, how's your day going?"),
        ("X" * 80 + " Surname", "Hi, how's your day going?"),
        ("J. Smith", "Hi, how's your day going?"),
    ],
)
async def test_real_names_never_break_the_session_or_the_greeting(tmp_path, name, greeting):
    rig = Rig(tmp_path, name=name)  # memory rules reject some of these; that must not crash
    await rig.session.start()
    assert rig.last.text == greeting


@pytest.mark.asyncio
async def test_greeting_reply_is_acknowledged_then_question_one_follows_untouched(tmp_path):
    rig = Rig(tmp_path)
    await rig.begin()
    await rig.say("I'm doing well, a bit tired but good.")
    assert rig.calls[0][0] == "opening_ack"
    assert set(rig.calls[0][1]) == {"greeting_response"}  # the name is not in this prompt data
    assert rig.last.kind is UtteranceKind.OPENING_ACK
    assert rig.last.text == f"{ACK} Let's get started."
    assert rig.controller.stage is Stage.DELIVERING
    deliver(rig.session)  # acknowledgement delivered; question one follows by itself
    await settle()
    assert rig.last.kind is UtteranceKind.QUESTION
    assert rig.last.text == rig.plan["questions"][0]["question"]
    deliver(rig.session)
    # Nothing said during the opening became an answer or spent question one's probes.
    assert rig.controller.answers[0] == ()
    assert rig.controller.remaining_follow_ups == 1
    assert rig.controller.remaining_clarifications == 1
    assert rig.session._opening_seconds == 0  # fake clock: no time passed


@pytest.mark.asyncio
async def test_long_small_talk_still_cannot_become_the_first_answer(tmp_path):
    rig = Rig(tmp_path)
    await rig.begin()
    await rig.say("Good! Honestly I'm excited to talk about the ingestion service and Kafka.")
    deliver(rig.session)
    await settle()
    deliver(rig.session)
    assert rig.controller.answers[0] == () and rig.controller.remaining_follow_ups == 1
    assert [kind for kind, _ in rig.calls] == ["opening_ack"]  # no decision on the small talk


@pytest.mark.asyncio
async def test_a_ready_reply_skips_small_talk_and_the_model(tmp_path):
    rig = Rig(tmp_path)
    await rig.begin()
    await rig.say("I'm good, let's start.")
    assert rig.calls == []  # nothing to acknowledge
    assert (
        rig.last.kind is UtteranceKind.OPENING_ACK and rig.last.text == "Great, let's get started."
    )
    deliver(rig.session)
    await settle()
    assert rig.last.text == rig.plan["questions"][0]["question"]


@pytest.mark.asyncio
async def test_a_failing_or_unsuitable_acknowledgement_falls_back_to_a_neutral_one(tmp_path):
    for bad in (RuntimeError("model down"), "Great job! What a strength. Ready?", ""):
        rig = Rig(tmp_path / str(abs(hash(str(bad)))), ack=bad)
        (tmp_path / str(abs(hash(str(bad))))).mkdir(exist_ok=True)
        await rig.begin()
        await rig.say("Doing well, how are you?")
        assert rig.last.kind is UtteranceKind.OPENING_ACK
        assert rig.last.text == "Doing well, thanks. Let's get started."


@pytest.mark.asyncio
async def test_repeat_is_honoured_once_then_the_opening_moves_on(tmp_path):
    rig = Rig(tmp_path)
    await rig.begin()
    await rig.say("Sorry, could you say that again?")
    assert (
        rig.last.kind is UtteranceKind.GREETING
        and rig.last.text == "Hi Drake, how's your day going?"
    )
    deliver(rig.session)
    await rig.say("Can you repeat that?")  # the allowance is spent: treated as a reply
    assert rig.last.kind is UtteranceKind.OPENING_ACK
    assert len(rig.rows("opening_repeat_limit")) == 1


@pytest.mark.asyncio
async def test_a_spoken_stop_during_the_opening_ends_politely_without_a_question(tmp_path):
    rig = Rig(tmp_path)
    await rig.begin()
    await rig.say("Please end the interview.")
    assert rig.last.kind is UtteranceKind.EARLY_FEEDBACK
    deliver(rig.session)
    assert rig.controller.stage is Stage.ENDED_EARLY
    summary = rig.rows("coverage_summary")[-1]["summary"]
    assert summary["asked"] == [] and summary["end_reason"] == "ended_early"
    assert all(u.kind is not UtteranceKind.QUESTION for u in rig.spoken)


@pytest.mark.asyncio
async def test_a_spoken_name_correction_during_the_opening_updates_memory_only(tmp_path):
    rig = Rig(tmp_path, name="Drake Example")
    await rig.begin()
    await rig.say("My name is Drayke, not Drake.")
    raw = [row for row in rig.rows("candidate_final") if "Drayke" in row["text"]]
    assert raw and raw[0]["text"] == "My name is Drayke, not Drake."  # raw transcript preserved
    assert rig.controller.preferred_name == "Drayke"
    assert rig.controller.answers[0] == ()  # a correction is not an answer
    assert rig.controller.remaining_follow_ups == 1
    # The opening is still open: the interview carries on afterwards.
    deliver(rig.session)
    await settle()
    assert rig.controller.stage in {Stage.AWAITING_GREETING, Stage.AWAITING_CORRECTION_RETURN} or (
        rig.controller.pending is not None
    )


@pytest.mark.asyncio
async def test_silence_after_the_greeting_moves_on_after_a_bounded_wait(tmp_path):
    rig = Rig(tmp_path, wait=0.05)
    await rig.begin()
    await asyncio.sleep(0.15)
    assert rig.rows("opening_reply_timeout")
    assert rig.last.text == "Whenever you're ready, let's get started."
    deliver(rig.session)
    await settle()
    assert rig.last.text == rig.plan["questions"][0]["question"]
    # Speaking cancels the wait: no timeout fires while the candidate is talking.
    quiet = Rig(tmp_path / "talk", wait=0.05)
    (tmp_path / "talk").mkdir(exist_ok=True)
    await quiet.begin()
    await quiet.session.user_started()
    await asyncio.sleep(0.15)
    assert not quiet.rows("opening_reply_timeout")


@pytest.mark.asyncio
async def test_talking_over_the_greeting_is_the_reply_and_the_greeting_does_not_resume(tmp_path):
    rig = Rig(tmp_path)
    await rig.session.start()
    greeting = rig.last
    begin_output(rig.session)
    await rig.session.user_started()  # "Hi Drake" is still playing
    await rig.session.user_stopped("Good thanks, you?")
    await settle()
    assert rig.calls[0][0] == "opening_ack"
    assert rig.last.kind is UtteranceKind.OPENING_ACK
    assert [u for u in rig.spoken if u.kind is UtteranceKind.GREETING] == [greeting]  # once only
    # Even before any audio, a reply is still a reply (the greeting is never replayed).
    early = Rig(tmp_path / "early")
    (tmp_path / "early").mkdir(exist_ok=True)
    await early.session.start()
    await early.session.user_started()
    await early.session.user_stopped("Good thanks")
    await settle()
    assert early.last.kind is UtteranceKind.OPENING_ACK
    assert len([u for u in early.spoken if u.kind is UtteranceKind.GREETING]) == 1


@pytest.mark.asyncio
async def test_talking_over_the_acknowledgement_goes_straight_to_question_one(tmp_path):
    rig = Rig(tmp_path)
    await rig.begin()
    await rig.say("Doing fine.")
    begin_output(rig.session)  # the acknowledgement is playing
    await rig.say("Okay yes, go ahead.")
    assert rig.last.kind is UtteranceKind.QUESTION
    assert rig.last.text == rig.plan["questions"][0]["question"]
    assert rig.controller.answers[0] == () and rig.controller.remaining_follow_ups == 1


@pytest.mark.asyncio
async def test_a_late_acknowledgement_cannot_speak_after_the_candidate_moved_on(tmp_path):
    rig = Rig(tmp_path)
    gate = asyncio.Event()
    original = rig.session._infer

    async def slow(kind, payload):
        if kind == "opening_ack":
            await gate.wait()
        return await original(kind, payload)

    rig.session._infer = slow
    await rig.begin()
    await rig.say("Doing fine.")  # the acknowledgement is still being generated
    assert not [u for u in rig.spoken if u.kind is UtteranceKind.OPENING_ACK]
    await rig.say("Actually, let's just start.")  # the candidate continues
    gate.set()
    await settle()
    acks = [u for u in rig.spoken if u.kind is UtteranceKind.OPENING_ACK]
    assert len(acks) == 1 and acks[0].text == "Great, let's get started."


async def _opening_run(path, opening_secs):
    path.mkdir(exist_ok=True)
    plan = plan_dict(target_minutes=5)
    for item in plan["questions"]:
        item["budget_seconds"] = 60  # fits a five-minute interview
    rig = Rig(path, plan=plan)
    await rig.session.start()
    rig.now[0] += 1
    deliver(rig.session)
    rig.now[0] += opening_secs
    await rig.say("Good, let's start.")
    deliver(rig.session)
    await settle()
    deliver(rig.session)  # question one is delivered
    rig.now[0] += 20
    await rig.say("I built the ingestion service with two colleagues.")
    await rig.session.close()
    return rig


@pytest.mark.asyncio
async def test_the_opening_counts_against_a_short_time_budget(tmp_path):
    quick = await _opening_run(tmp_path / "quick", 10)
    slow = await _opening_run(tmp_path / "slow", 200)
    assert quick.session._opening_seconds == 11 and slow.session._opening_seconds == 201
    assert not quick.rows("questions_skipped_for_time")  # plenty of time: nothing dropped
    assert slow.rows("questions_skipped_for_time")  # a long opening left less room
    assert quick.rows("coverage_summary")[-1]["summary"]["opening_seconds"] == 11
    assert slow.rows("coverage_summary")[-1]["summary"]["opening_seconds"] == 201
