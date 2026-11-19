"""Soft duration budget on the live assigned path, plus honest coverage records."""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support import TEST_URL

from rehearse_ai.database.models import Account, InterviewRecord
from rehearse_ai.interview.budget import (
    ActiveClock,
    RemainingQuestion,
    pressure,
    question_seconds,
    skip_for_time,
)
from rehearse_ai.interview.controller import Stage
from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog
from rehearse_ai.records.store import InterviewStore

CLOSING = "That concludes the interview. Thank you for your time, Nyla."


def timed_plan(minutes: int = 10) -> dict:
    """Five questions: three required, two optional; budgets sum to 85% of the target."""
    each = int(minutes * 60 * 0.85 / 5)
    rows = [
        ("Background", True, "must"),
        ("Design", True, "must"),
        ("Mentoring", False, "should"),
        ("Tooling", False, "could"),
        ("Reliability", True, "must"),
    ]
    return {
        "title": "Timed interview",
        "purpose": "Exercise the time budget",
        "questions": [
            {
                "topic": topic,
                "question": f"Question about {topic.lower()} in your recent work?",
                "required": required,
                "follow_up_guidance": f"Listen for detail on {topic}",
                "max_probes": 1,
                "priority": priority,
                "budget_seconds": each,
            }
            for topic, required, priority in rows
        ],
        "focus_areas": [],
        "target_minutes": minutes,
        "interviewer_style": "Calm and concise",
        "closing_instructions": "Thank the candidate briefly",
    }


def deliver(session):
    utterance = session.controller.pending
    context = f"timed-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def settle():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


class Harness:
    def __init__(self, tmp_path, *, decisions=None, minutes=10):
        self.now = [1000.0]
        self.spoken = []
        self.published = []
        self.calls = []
        self.decisions = decisions or []
        self.log_path = tmp_path / "timed.jsonl"

        async def infer(kind, payload):
            self.calls.append((kind, payload))
            if kind == "assigned_closing":
                return {"text": CLOSING}
            if self.decisions:
                return self.decisions.pop(0)
            return {"action": "advance", "text": "", "covered_future_questions": []}

        async def speak(utterance):
            self.spoken.append(utterance)

        self.session = InterviewSession(
            SessionLog(tmp_path, "timed"),
            infer,
            speak,
            self.published.append,
            interview_plan=timed_plan(minutes),
            opening=False,
            self_reported_name="Nyla Example",
            clock=lambda: self.now[0],
        )

    def rows(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def events(self, name):
        return [row for row in self.rows() if row["event"] == name]

    async def answer(self, text="I worked on it with two colleagues and shipped it."):
        await self.session.user_started()
        await self.session.user_stopped(text)
        await settle()

    def pump(self):
        deliver(self.session)


# --- policy -------------------------------------------------------------------------


def test_active_clock_excludes_pauses_and_ignores_unstarted_time():
    now = [100.0]
    clock = ActiveClock(lambda: now[0])
    now[0] = 150.0
    assert clock.active_seconds() == 0.0  # not started
    clock.start()
    now[0] = 190.0
    clock.pause()
    clock.pause()  # idempotent
    now[0] = 700.0
    assert clock.active_seconds() == 40.0 and clock.paused_seconds == 510.0
    clock.resume()
    now[0] = 730.0
    assert clock.active_seconds() == 70.0 and clock.paused_seconds == 510.0


def test_pressure_and_skip_policy():
    remaining = [
        RemainingQuestion(1, True, 100),
        RemainingQuestion(2, False, 100),
        RemainingQuestion(3, False, 100),
        RemainingQuestion(4, True, 100),
    ]
    assert pressure(0, 600, remaining) == "normal"
    assert pressure(481, 600, remaining) == "tight"  # past 80 percent
    assert pressure(250, 600, remaining) == "normal"  # 350s left against 200s of required work
    assert pressure(420, 600, remaining) == "tight"  # 180s left: required work no longer fits
    assert pressure(600, 600, remaining) == "exhausted"
    assert skip_for_time(0, 600, remaining) == []  # everything fits
    assert skip_for_time(0, 350, remaining) == [3]  # room for one optional question only
    assert skip_for_time(390, 600, remaining) == [2, 3]  # 210s left: required work only
    assert skip_for_time(0, 3000, remaining) == []
    assert skip_for_time(601, 600, remaining) == [1, 2, 3, 4]
    assert question_seconds([{"budget_seconds": 90}, {}], 600) == [90, 255]


# --- live execution -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_pace_asks_every_question_and_allows_a_probe(tmp_path):
    h = Harness(
        tmp_path,
        decisions=[
            {"action": "follow_up", "text": "What did you own?", "covered_future_questions": []}
        ],
    )
    await h.session.start()
    h.pump()
    h.now[0] += 60
    await h.answer()
    assert h.spoken[-1].text == "What did you own?"
    h.pump()
    assert h.calls[0][1]["time_pressure"] == "normal"
    for expected in range(2, 6):
        h.now[0] += 40
        await h.answer()
        assert h.spoken[-1].text.startswith("Question about"), expected
        h.pump()
    h.now[0] += 40
    await h.answer()
    assert h.spoken[-1].text == CLOSING
    h.pump()
    summary = h.events("coverage_summary")[-1]["summary"]
    assert summary["asked"] == [1, 2, 3, 4, 5] and summary["skipped_for_time"] == []
    assert summary["required_not_reached"] == [] and summary["end_reason"] == "completed"
    assert summary["ended_for_time"] is False


@pytest.mark.asyncio
async def test_tight_budget_stops_optional_probing_and_drops_optional_questions(tmp_path):
    h = Harness(
        tmp_path,
        decisions=[
            {"action": "follow_up", "text": "What did you own?", "covered_future_questions": []}
        ],
    )
    await h.session.start()
    h.pump()
    h.now[0] += 490  # 490 of 600 seconds used: tight
    await h.answer()
    # The proposed follow-up is refused and the interview moves on, skipping optional questions.
    assert h.calls[0][1]["time_pressure"] == "tight"
    assert any(row["reason"] == "probe_budget" for row in h.events("decision_rejected"))
    assert h.spoken[-1].text.startswith("Question about design")
    assert h.events("questions_skipped_for_time")[0]["question_numbers"] == [3, 4]
    h.pump()
    await h.answer()
    assert h.spoken[-1].text.startswith("Question about reliability")
    h.pump()
    await h.answer()
    assert h.spoken[-1].text == CLOSING
    h.pump()
    summary = h.events("coverage_summary")[-1]["summary"]
    assert summary["asked"] == [1, 2, 5] and summary["skipped_for_time"] == [3, 4]
    assert summary["required_not_reached"] == [] and summary["optional_not_reached"] == [3, 4]
    assert summary["end_reason"] == "completed"
    assert "feedback" not in h.spoken[-1].text.lower()


@pytest.mark.asyncio
async def test_exhausted_budget_closes_after_the_turn_finishes_without_cutting_it_off(tmp_path):
    h = Harness(tmp_path)
    await h.session.start()
    h.pump()
    await h.session.user_started()
    h.now[0] += 5000  # the budget runs out while the candidate is still talking
    assert h.session.controller.stage is Stage.AWAITING_ANSWER  # nothing interrupted them
    await h.session.user_stopped("Here is my complete answer, finished at last.")
    await settle()
    assert h.session.controller.answers[0] == ("Here is my complete answer, finished at last.",)
    assert not [kind for kind, _ in h.calls if kind == "assigned_decision"]  # no further probing
    assert h.events("inference_skipped_budget_exhausted")
    assert h.spoken[-1].text == CLOSING
    assert not [kind for kind, _ in h.calls if kind == "assigned_closing"]  # fixed text
    h.pump()
    summary = h.events("coverage_summary")[-1]["summary"]
    assert summary["end_reason"] == "time_budget" and summary["ended_for_time"] is True
    assert summary["asked"] == [1] and summary["answered"] == [1]
    # Required questions that were never reached are recorded, not hidden.
    assert summary["required_not_reached"] == [2, 5] and summary["optional_not_reached"] == [3, 4]
    names = [row["event"] for row in h.rows()]
    assert names.index("coverage_summary") < names.index("interview_complete")
    assert h.session.controller.stage is Stage.COMPLETE


@pytest.mark.asyncio
async def test_media_pause_time_does_not_count_toward_the_budget(tmp_path):
    h = Harness(tmp_path)
    await h.session.start()
    h.pump()
    h.now[0] += 100
    await h.session.media_pause("camera_lost")
    h.now[0] += 3600  # an hour with the device unplugged
    await h.session.media_resume()
    h.now[0] += 50
    await h.answer()
    assert h.calls[0][1]["time_pressure"] == "normal"  # 150 active seconds, not 3750
    assert h.spoken[-1].text.startswith("Question about design")
    h.pump()
    assert not h.events("questions_skipped_for_time")
    summary_seconds = h.session._clock.active_seconds()
    assert 149 <= summary_seconds <= 151 and h.session._clock.paused_seconds == 3600


@pytest.mark.asyncio
async def test_early_stop_and_disconnect_record_what_was_not_reached(tmp_path):
    h = Harness(tmp_path)
    await h.session.start()
    h.pump()
    await h.answer("Please end the interview.")
    assert h.spoken[-1].text.startswith("Thank you for your time")
    h.pump()
    summary = h.events("coverage_summary")[-1]["summary"]
    assert summary["end_reason"] == "ended_early"
    assert summary["required_not_reached"] == [2, 5]
    assert summary["optional_not_reached"] == [3, 4] and summary["asked"] == [1]


@pytest.mark.asyncio
async def test_disconnect_records_coverage_before_the_session_ends(tmp_path):
    h = Harness(tmp_path)
    await h.session.start()
    h.pump()
    await h.answer()
    h.pump()
    await h.session.close()
    names = [row["event"] for row in h.rows()]
    assert names.index("coverage_summary") < names.index("session_ended")
    summary = h.events("coverage_summary")[-1]["summary"]
    assert summary["end_reason"] == "disconnected_before_completion"
    assert summary["asked"] == [1, 2] and summary["required_not_reached"] == [5]


# --- live-path behaviours that must survive the context reduction --------------------


@pytest.mark.asyncio
async def test_feedback_refusal_repeat_and_correction_still_work_on_the_bounded_context(tmp_path):
    h = Harness(tmp_path)
    await h.session.start()
    h.pump()
    await h.answer("Could you give me feedback on my answer?")
    assert "can't provide interview feedback" in h.spoken[-1].text
    assert h.session.controller.answers[0] == ()  # not recorded as an answer
    h.pump()
    await h.answer("Can you repeat the question?")
    assert h.spoken[-1].text.startswith("Question about background")
    h.pump()
    await h.answer("I used web books for event delivery.")
    await settle()
    h.pump()
    await h.answer("I said webhooks, not web books.")
    assert h.session.controller.memory.get("technical_terms").value == "webhooks"
    assert not [kind for kind, _ in h.calls if kind == "feedback"]
    for _, payload in h.calls:
        assert "assigned_interview" not in payload


@pytest.mark.asyncio
async def test_interrupting_the_question_accepts_the_answer_and_keeps_the_budget(tmp_path):
    h = Harness(tmp_path)
    await h.session.start()
    utterance = h.session.controller.pending
    context = f"timed-{utterance.id}"
    h.session.tts_started(utterance.id, context)
    h.session.tts_first_audio(context)
    h.session.bot_started()  # the question is being spoken...
    h.now[0] += 3  # ...and plays long enough to be heard...
    await h.session.user_started()  # ...before the candidate talks over it
    h.now[0] += 20
    await h.session.user_stopped("Sure, I led the data platform team for three years.")
    await settle()
    assert h.session.controller.answers[0] == (
        "Sure, I led the data platform team for three years.",
    )
    assert h.calls and h.calls[0][1]["time_pressure"] == "normal"
    assert h.spoken[-1].text.startswith("Question about design")


# --- persisted coverage and historical compatibility ---------------------------------


@pytest.mark.asyncio
async def test_coverage_is_persisted_and_historical_records_remain_readable(tmp_path):
    engine = create_async_engine(TEST_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        candidate = Account(
            id=uuid.uuid4(),
            email=f"history-{uuid.uuid4().hex}@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        async with sessions.begin() as db:
            db.add(candidate)
        store = InterviewStore(sessions, tmp_path / "logs", tmp_path / "pdf")
        # A historical-style practice record: no plan version and no coverage at all.
        old_id = await store.create_attempt(candidate.id, "build", "prompt")
        old_log = SessionLog(store.logs, str(old_id))
        for kind, values in (
            ("session_started", {}),
            ("agent_prepared", {"speaker": "agent", "utterance_id": 1, "text": "Hello"}),
            (
                "candidate_final",
                {"speaker": "candidate", "text": "A practice answer", "turn_number": 1},
            ),
            ("interview_complete", {}),
        ):
            await store.apply_event(old_log.record(kind, **values))
        old_log.close()
        assert await store.generate_pdf(old_id) is True
        # A new-style record carries its coverage summary.
        new_id = await store.create_attempt(candidate.id, "build", "prompt")
        new_log = SessionLog(store.logs, str(new_id))
        summary = {
            "planned": 5,
            "asked": [1, 2],
            "required_not_reached": [5],
            "end_reason": "time_budget",
        }
        for kind, values in (
            ("session_started", {}),
            ("coverage_summary", {"summary": summary}),
            ("interview_complete", {}),
        ):
            await store.apply_event(new_log.record(kind, **values))
        assert await store.generate_pdf(new_id) is True
        async with sessions() as db:
            old = await db.get(InterviewRecord, old_id)
            new = await db.get(InterviewRecord, new_id)
            assert old.coverage_summary is None and old.plan_version_id is None
            assert old.pdf_status == "available" and new.pdf_status == "available"
            assert new.coverage_summary == summary
        assert store.pdf_path(old_id).is_file() and store.pdf_path(new_id).is_file()
        # Summaries are ignored after a record is final, so they cannot rewrite history.
        await store.apply_event(new_log.record("coverage_summary", summary={"planned": 99}))
        new_log.close()
        async with sessions() as db:
            assert (await db.get(InterviewRecord, new_id)).coverage_summary == summary
            assert await db.scalar(select(Account.id).where(Account.id == candidate.id))
    finally:
        await engine.dispose()
