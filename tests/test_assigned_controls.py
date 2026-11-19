"""Conversational controls on the assigned path: repeat, finish, restart, not knowing, detours.

Synthetic conversations with scripted models and an injected clock. They assert controller state
(pending prompt, probe allowance, answer records, coverage, terminal reason, model calls) as well as
the words spoken. They say nothing about speech recognition or real microphones.
"""

import asyncio
import json

import pytest
from support import plan_dict

from rehearse_ai.interview.controller import Stage, UtteranceKind
from rehearse_ai.interview.controls import interpret
from rehearse_ai.interview.session import (
    FEEDBACK_DECLINED,
    FINISH_CLARIFICATION,
    OUTCOME_UNKNOWN,
    REPEAT_EXHAUSTED,
    RESTART_UNAVAILABLE,
    InterviewSession,
)
from rehearse_ai.interview.session_log import SessionLog

Q = [item["question"] for item in plan_dict()["questions"]]


# --- the classifier on its own ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Can you repeat the question again? I just forgot.", "repeat"),
        ("Okay. Can you repeat it?", "repeat"),
        ("Sorry, could you say that again?", "repeat"),
        ("say that again please", "repeat"),
        ("I'm done with the interview", "finish"),
        ("Oh, actually, I'm done with the interview. Can you just give me feedback?", "finish"),
        ("Please end the interview.", "finish"),
        ("I don't want to continue", "finish"),
        ("I'm done.", "finish_unclear"),
        ("I'm finished", "finish_unclear"),
        ("I'm done with my answer.", "none"),
        ("So it worked like that. That's how we did it. I'm done.", "none"),
        ("Please don't end the interview.", "none"),
        ("don't end the interview", "none"),
        ("I don't want to end the interview", "none"),
        ("I'm not done", "none"),
        ("So can we roll back to the first question?", "back"),
        ("go back to question one", "back"),
        ("let's start over", "restart"),
        ("restart the interview", "restart"),
        (
            "No. I'm saying can you go with the starting of the interview "
            "and let's start the interview again?",
            "restart",
        ),
        ("if it fails we restart the job again", "none"),
        ("I stopped the pipeline and restarted it", "none"),
        ("I don't know.", "gap"),
        ("I haven't used Kafka.", "gap"),
        ("I have no experience with Docker or GitHub Actions.", "gap"),
        ("I'd rather not answer that.", "gap"),
        ("I don't know if you heard me.", "none"),
        ("I don't know what you mean.", "not_understood"),
        ("I don't understand the question.", "not_understood"),
        ("Can you give me feedback?", "feedback"),
        ("Why can't you provide me feedback?", "feedback"),
        ("I really like getting feedback from users on the product.", "none"),
        ("Who is the hiring person?", "outcome"),
        ("How many questions are left?", "progress"),
        ("The next steps in the pipeline were validation.", "none"),
    ],
)
def test_clear_controls_are_recognised_and_lookalikes_are_not(text, kind):
    assert interpret(text).kind == kind


def test_a_mixed_answer_keeps_its_substance_and_names_what_was_not_used():
    intent = interpret("I haven't used Kafka, but I used Airflow for backfills.")
    assert intent.kind == "gap" and intent.gap == "not_used" and intent.subject == "Kafka"
    assert "Airflow for backfills" in intent.residual
    assert interpret("Honestly I don't know, I never used it.").gap == "dont_know"


def test_finish_outranks_feedback_and_repeat_outranks_gap():
    assert interpret("I'm done with the interview. Can I get feedback?").kind == "finish"
    assert interpret("I don't know. Can you repeat it?").kind == "repeat"


# --- the live assigned path -------------------------------------------------------------------


def deliver(session):
    utterance = session.controller.pending
    context = f"ctl-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


def begin_output(session):
    utterance = session.controller.pending
    context = f"ctl-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    return context


async def settle(times=10):
    for _ in range(times):
        await asyncio.sleep(0)


class Rig:
    def __init__(self, tmp_path, *, decisions=None, name="Nyla Example", minutes=20):
        self.now = [100.0]
        self.spoken, self.calls = [], []
        self.decisions = list(decisions or [])
        self.gate = None

        async def infer(kind, payload):
            self.calls.append((kind, payload))
            if kind == "assigned_decision":
                if self.gate is not None:
                    gate, self.gate = self.gate, None
                    await gate.wait()
                if self.decisions:
                    return self.decisions.pop(0)
            return {"action": "advance", "text": "", "covered_future_questions": []}

        async def speak(utterance):
            self.spoken.append(utterance)

        self.session = InterviewSession(
            SessionLog(tmp_path, "ctl"),
            infer,
            speak,
            lambda _event: None,
            interview_plan=plan_dict(target_minutes=minutes),
            self_reported_name=name,
            opening=False,
            clock=lambda: self.now[0],
        )
        self.log = tmp_path / "ctl.jsonl"

    controller = property(lambda self: self.session.controller)
    last = property(lambda self: self.spoken[-1])

    def rows(self, event=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if event is None or row["event"] == event]

    def decisions_made(self):
        return len([c for c in self.calls if c[0] == "assigned_decision"])

    async def say(self, text):
        await self.session.user_started()
        await self.session.user_stopped(text)
        await settle()

    async def start(self):
        await self.session.start()
        deliver(self.session)

    async def to_question(self, number):
        """Answer through to the given question, with it delivered and nothing yet said to it."""
        await self.start()
        for _ in range(number - 1):
            await self.say("I built it with two colleagues and shipped it.")
            deliver(self.session)
        assert self.controller.pending_prompt.question_number == number

    def coverage(self):
        return self.controller.coverage_snapshot()


def follow_up(text="Which part was hardest?"):
    return {"action": "follow_up", "text": text, "covered_future_questions": []}


@pytest.mark.asyncio
async def test_repeat_of_a_main_question_changes_nothing_but_the_repeat_allowance(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("Sorry. Sorry. Can you repeat the question again? I just forgot.")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[0]
    assert rig.decisions_made() == 0  # the model was never asked, so it cannot move on
    assert rig.controller.answers[0] == () and rig.controller.remaining_follow_ups == 1
    assert rig.controller.pending_prompt.text == Q[0]
    deliver(rig.session)
    assert rig.controller.stage is Stage.AWAITING_ANSWER
    assert rig.controller.current_question_number == 1
    assert rig.coverage()["asked"] == [1] and rig.coverage()["answered"] == []


@pytest.mark.asyncio
async def test_repeat_of_a_follow_up_repeats_that_follow_up_and_spends_no_second_probe(tmp_path):
    rig = Rig(tmp_path, decisions=[follow_up("Which part was hardest?")])
    await rig.start()
    await rig.say("I built the ingestion service.")
    probe = rig.last
    assert probe.kind is UtteranceKind.FOLLOW_UP
    deliver(rig.session)
    await rig.say("Say that again.")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == "Which part was hardest?"
    assert rig.controller.remaining_follow_ups == 0  # one probe, spent once
    assert rig.decisions_made() == 1
    assert rig.controller.pending_prompt.id == probe.id  # still the follow-up, not the repeat
    deliver(rig.session)
    assert rig.controller.stage is Stage.AWAITING_ANSWER


@pytest.mark.asyncio
async def test_a_prompt_that_was_never_heard_is_asked_again_in_kind_and_is_not_a_repeat(tmp_path):
    rig = Rig(tmp_path, decisions=[follow_up()])
    await rig.start()
    await rig.say("I built the ingestion service.")
    probe = rig.last
    rig.session.tts_started(probe.id, f"ctl-{probe.id}")  # requested, no audio yet
    await rig.session.user_started()
    await rig.session.user_stopped("Sorry, can you repeat it?")
    await settle()
    assert rig.last.kind is UtteranceKind.FOLLOW_UP and rig.last.text == probe.text
    assert rig.controller.remaining_follow_ups == 0 and rig.decisions_made() == 1
    assert rig.controller._repeats == [0, 0, 0]  # an unheard prompt does not use the allowance
    deliver(rig.session)
    await rig.say("Repeat that please.")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.controller._repeats[0] == 1


@pytest.mark.asyncio
async def test_repeated_repeats_end_in_an_explicit_recovery_never_a_silent_advance(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    for _ in range(2):
        await rig.say("Can you repeat the question?")
        assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[0]
        deliver(rig.session)
    await rig.say("Can you repeat the question?")  # the allowance is used up
    assert rig.last.kind is UtteranceKind.INFORMATION and rig.last.text == REPEAT_EXHAUSTED
    assert rig.rows("repeat_limit_reached")
    assert rig.decisions_made() == 0 and rig.controller.answers[0] == ()
    assert rig.controller.current_question_number == 1
    assert rig.controller.pending_prompt.text == Q[0]  # still the same question, not the next
    assert not [u for u in rig.spoken if u.question_number == 2]
    deliver(rig.session)
    # Admitting they cannot answer is the way forward the recovery offers.
    await rig.say("I don't know.")
    assert rig.last.kind is UtteranceKind.QUESTION and rig.last.question_number == 2


@pytest.mark.asyncio
async def test_an_interrupted_repeat_is_still_a_repeat_of_the_same_prompt(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("Repeat the question.")
    repeat = rig.last
    begin_output(rig.session)
    rig.now[0] += 0.3  # talked over almost at once
    await rig.say("Sorry, say that again?")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[0]
    assert rig.last.id != repeat.id
    assert rig.controller.current_question_number == 1 and rig.decisions_made() == 0
    assert rig.controller.answers[0] == () and rig.controller.remaining_follow_ups == 1
    assert rig.controller._repeats[0] == 2  # both requests counted, neither advanced


@pytest.mark.asyncio
async def test_after_a_feedback_detour_repeat_it_returns_to_the_same_question(tmp_path):
    rig = Rig(tmp_path)
    await rig.to_question(2)
    decisions = rig.decisions_made()
    await rig.say("Could you give me feedback?")
    assert rig.last.kind is UtteranceKind.INFORMATION and rig.last.text == FEEDBACK_DECLINED
    assert "hiring team" not in rig.last.text and "interview feedback" in rig.last.text
    assert rig.rows("feedback_request_declined")
    deliver(rig.session)
    await rig.say("Okay. Can you repeat it?")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[1]  # not question three
    assert rig.decisions_made() == decisions  # no model call for either control
    assert rig.controller.answers[1] == () and rig.controller.remaining_follow_ups == 1
    assert rig.coverage()["asked"] == [1, 2] and rig.controller.current_question_number == 2


@pytest.mark.asyncio
async def test_finish_with_a_feedback_request_closes_politely_without_feedback_or_a_question(
    tmp_path,
):
    rig = Rig(tmp_path)
    await rig.to_question(3)
    spoken_before = len(rig.spoken)
    await rig.say(
        "Oh, actually, I'm done with the interview. Can you just give me the free feedback?"
    )
    assert rig.last.kind is UtteranceKind.EARLY_FEEDBACK
    assert rig.last.text == "Thank you for your time, Nyla. We'll end the interview here."
    assert len(rig.spoken) == spoken_before + 1  # only the closing; no question, no refusal
    assert not rig.rows("feedback_request_declined") and rig.decisions_made() == 2
    deliver(rig.session)
    assert rig.controller.stage is Stage.ENDED_EARLY
    summary = rig.rows("coverage_summary")[-1]["summary"]
    assert summary["end_reason"] == "ended_early" and summary["asked"] == [1, 2, 3]
    assert len(rig.rows("interview_ended_early")) == 1 and not rig.rows("interview_complete")


@pytest.mark.asyncio
async def test_finishing_cancels_stale_inference_and_speech_so_no_question_plays_afterwards(
    tmp_path,
):
    rig = Rig(tmp_path)
    await rig.start()
    rig.gate = asyncio.Event()  # the decision for the answer is still being made
    await rig.say("I built the ingestion service.")
    await rig.say("I'm done with the interview.")
    rig.gate.set() if rig.gate else None
    await settle()
    kinds = [u.kind for u in rig.spoken]
    assert kinds[-1] is UtteranceKind.EARLY_FEEDBACK
    assert UtteranceKind.QUESTION not in kinds[1:] and UtteranceKind.FOLLOW_UP not in kinds
    assert rig.rows("inference_cancel_requested")
    # And while a question is audibly playing: it is cut off and never resumed.
    other = Rig(tmp_path / "b")
    (tmp_path / "b").mkdir(exist_ok=True)
    await other.start()
    await other.say("I built it with two colleagues.")  # question two is prepared, not yet heard
    question = other.last
    begin_output(other.session)
    other.now[0] += 2
    await other.say("I'm done with the interview.")
    assert other.last.kind is UtteranceKind.EARLY_FEEDBACK
    deliver(other.session)
    assert [u for u in other.spoken if u.text == question.text] == [question]
    assert other.controller.stage is Stage.ENDED_EARLY


@pytest.mark.asyncio
async def test_negated_and_ambiguous_endings_do_not_end_or_advance(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("Please don't end the interview.")
    assert not rig.session._stop_latched and rig.controller.stage is not Stage.EARLY_CLOSING
    assert rig.decisions_made() == 1  # it is ordinary speech, so the model reads it
    deliver(rig.session) if rig.controller.pending else None
    clarify = Rig(tmp_path / "c")
    (tmp_path / "c").mkdir(exist_ok=True)
    await clarify.start()
    await clarify.say("I'm done.")
    assert clarify.last.text == FINISH_CLARIFICATION
    assert clarify.decisions_made() == 0 and clarify.controller.answers[0] == ()
    assert clarify.controller.current_question_number == 1 and not clarify.session._stop_latched
    deliver(clarify.session)
    await clarify.say("End the interview.")  # the clarification is answered
    assert clarify.last.kind is UtteranceKind.EARLY_FEEDBACK
    # A plain "yes" agrees to the clarification; the same "yes" elsewhere is just speech.
    agree = Rig(tmp_path / "e")
    (tmp_path / "e").mkdir(exist_ok=True)
    await agree.start()
    await agree.say("I'm finished.")
    deliver(agree.session)
    await agree.say("Yes.")
    assert agree.last.kind is UtteranceKind.EARLY_FEEDBACK and agree.decisions_made() == 0
    plain = Rig(tmp_path / "f")
    (tmp_path / "f").mkdir(exist_ok=True)
    await plain.start()
    await plain.say("Yes.")
    assert not plain.session._stop_latched and plain.decisions_made() == 1
    # "I'm done with my answer" is not a request to end either.
    answer = Rig(tmp_path / "d")
    (tmp_path / "d").mkdir(exist_ok=True)
    await answer.start()
    await answer.say("I'm done with my answer.")
    assert not answer.session._stop_latched and answer.decisions_made() == 1


@pytest.mark.asyncio
async def test_restart_and_go_back_are_declined_honestly_and_nothing_moves(tmp_path):
    rig = Rig(tmp_path)
    await rig.to_question(2)
    snapshot = [tuple(a) for a in rig.controller.answers]
    for request in (
        "So can we roll back to the first question?",
        "Let's start the interview again.",
    ):
        await rig.say(request)
        assert rig.last.text == RESTART_UNAVAILABLE and "can't restart" in rig.last.text
        deliver(rig.session)
    assert len(rig.rows("restart_declined")) == 2
    assert (
        rig.controller.current_question_number == 2 and rig.controller.pending_prompt.text == Q[1]
    )
    assert [tuple(a) for a in rig.controller.answers] == snapshot  # no answer deleted or added
    assert rig.coverage()["asked"] == [1, 2] and not [u for u in rig.spoken if u.text == Q[2]]
    # "Repeat this question" is a different request and still works.
    await rig.say("Please repeat the question.")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[1]
    assert not rig.session._stop_latched and rig.controller.stage is not Stage.COMPLETE


@pytest.mark.asyncio
async def test_a_run_of_detours_returns_to_the_prompt_instead_of_looping(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    for _ in range(3):
        await rig.say("Can we start over?")
        assert rig.last.text == RESTART_UNAVAILABLE
        deliver(rig.session)
    await rig.say("Can we start over?")  # the fourth in a row: back to the question itself
    assert rig.rows("control_detour_limit")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[0]
    assert rig.controller.current_question_number == 1 and rig.decisions_made() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "words",
    [
        "I don't know.",
        "I haven't used Kafka.",
        "I have no experience with that.",
        "I'd rather not answer that.",
    ],
)
async def test_not_knowing_or_declining_is_recorded_as_the_answer_and_not_probed_again(
    tmp_path, words
):
    rig = Rig(tmp_path)
    await rig.to_question(2)
    calls = rig.decisions_made()
    await rig.say(words)
    assert rig.controller.answers[1] == (words,)  # the actual words, as spoken
    assert rig.rows("candidate_gap_recorded")
    assert rig.decisions_made() == calls  # no model call, so no further demand for the detail
    assert rig.last.kind is UtteranceKind.QUESTION and rig.last.text == Q[2]  # moved on
    assert rig.controller._follow_ups == [0, 0, 0]  # no probe was spent anywhere
    deliver(rig.session)
    await rig.session.close()
    summary = rig.rows("coverage_summary")[-1]["summary"]
    assert summary["no_knowledge_or_declined"] == [2] and 2 in summary["answered"]
    assert summary["required_not_reached"] == []


@pytest.mark.asyncio
async def test_a_mixed_answer_keeps_the_useful_part_and_implies_nothing_about_the_missing_one(
    tmp_path,
):
    rig = Rig(tmp_path)
    await rig.to_question(2)
    mixed = "I haven't used Kafka, but I used Airflow for backfills."
    await rig.say(mixed)
    assert rig.controller.answers[1] == (mixed,)  # nothing discarded
    row = rig.rows("candidate_gap_recorded")[-1]
    assert row["gap"] == "not_used" and row["subject"] == "Kafka" and row["mixed"] is True
    assert rig.last.text == Q[2]  # no probe about Kafka (or anything else) on that question
    deliver(rig.session)
    await rig.say("We kept the owners in a shared document.")
    prior = rig.calls[-1][1]["prior_answers"]
    assert any("Airflow for backfills" in item["answer"] for item in prior)


@pytest.mark.asyncio
async def test_not_understanding_is_a_request_to_hear_it_again_not_an_admission(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("I don't understand the question.")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[0]
    assert not rig.rows("candidate_gap_recorded") and rig.controller.answers[0] == ()
    assert rig.decisions_made() == 0 and rig.controller.current_question_number == 1


@pytest.mark.asyncio
async def test_i_dont_know_if_you_heard_me_is_ordinary_speech_not_a_skip(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("I don't know if you heard me.")
    assert not rig.rows("candidate_gap_recorded") and not rig.rows("candidate_skipped_question")
    assert rig.decisions_made() == 1 and rig.controller.current_question_number == 1


@pytest.mark.asyncio
async def test_a_barely_heard_prompt_cannot_be_declined_it_is_asked_again(tmp_path):
    rig = Rig(tmp_path)
    await rig.to_question(2)
    answers = [tuple(a) for a in rig.controller.answers]
    # Question three starts and is talked over almost at once with "I don't know".
    rig.decisions.append({"action": "advance", "text": "", "covered_future_questions": []})
    await rig.say("We shared one document.")
    question = rig.last
    begin_output(rig.session)
    rig.now[0] += 0.3
    await rig.say("I don't know.")
    assert rig.last.text == question.text and rig.last.kind is UtteranceKind.QUESTION
    assert not rig.rows("candidate_gap_recorded")
    assert rig.controller.answers[2] == () and [tuple(a) for a in rig.controller.answers[:2]] != []
    assert rig.controller.answers[0] == answers[0]


@pytest.mark.asyncio
async def test_process_questions_get_bounded_factual_replies_and_nothing_is_invented(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say("Okay. Who is the hiring person? Who is the one who is hiring?")
    assert rig.last.text == OUTCOME_UNKNOWN
    assert "hiring decisions, contacts or next steps" in rig.last.text
    deliver(rig.session)
    await rig.say("How many questions are left?")
    assert "3 questions" in rig.last.text and "number 1" in rig.last.text
    assert "20 minutes" in rig.last.text
    assert rig.decisions_made() == 0 and rig.controller.answers[0] == ()
    assert rig.controller.current_question_number == 1 and rig.controller.remaining_follow_ups == 1


@pytest.mark.asyncio
async def test_answers_that_merely_mention_feedback_or_restarts_are_answers(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.say(
        "I really like getting feedback from users, and we restart the job again on failure."
    )
    assert rig.decisions_made() == 1 and not rig.rows("control_recognised")
    assert "feedback from users" in " ".join(rig.controller.answers[0])


@pytest.mark.asyncio
async def test_speech_over_a_control_reply_is_handled_as_a_fresh_control_or_answer(tmp_path):
    rig = Rig(tmp_path)
    await rig.to_question(2)
    await rig.say("Can you give me feedback?")
    begin_output(rig.session)
    rig.now[0] += 4  # the refusal was heard
    await rig.say("Can you repeat the question?")
    assert rig.last.kind is UtteranceKind.REPEAT and rig.last.text == Q[1]
    assert rig.controller.pending_prompt.text == Q[1] and rig.controller.answers[1] == ()
    assert rig.rows("agent_interrupted")[-1]["heard_enough"] is True


@pytest.mark.asyncio
async def test_a_late_repeat_request_supersedes_a_stale_decision_and_is_handled_once(tmp_path):
    rig = Rig(tmp_path, decisions=[follow_up()])
    await rig.start()
    rig.gate = asyncio.Event()
    await rig.say("I built the ingestion service.")  # decision pending
    await rig.session.late_transcript("Can you repeat it?", boundary_frame_id=1)
    rig.gate.set() if rig.gate else None
    await settle()
    repeats = [u for u in rig.spoken if u.kind is UtteranceKind.REPEAT]
    assert len(repeats) == 1 and repeats[0].text == Q[0]
    assert not [u for u in rig.spoken if u.kind is UtteranceKind.FOLLOW_UP]  # the stale decision
    # A duplicate delivery acknowledgement does nothing further.
    deliver(rig.session)
    stage = rig.controller.stage
    rig.session.bot_stopped()
    assert rig.controller.stage is stage and len(rig.rows("agent_delivery_reported")) == 2


@pytest.mark.asyncio
async def test_the_closing_is_fixed_neutral_and_promises_nothing_whatever_the_ending(tmp_path):
    forbidden = (
        "hiring",
        "team",
        "contact",
        "touch",
        "outcome",
        "feedback",
        "next steps",
        "update",
    )
    done = Rig(tmp_path / "done")
    (tmp_path / "done").mkdir(exist_ok=True)
    await done.start()
    for turn in range(3):
        await done.say("I built it with two colleagues and shipped it.")
        if turn < 2:
            deliver(done.session)  # questions are heard; the closing is delivered below
    assert done.last.kind is UtteranceKind.CLOSING
    assert done.last.text == "That concludes the interview. Thank you for your time, Nyla."
    assert not [kind for kind, _ in done.calls if kind == "assigned_closing"]
    deliver(done.session)
    assert done.controller.stage is Stage.COMPLETE
    assert done.rows("coverage_summary")[-1]["summary"]["end_reason"] == "completed"
    early = Rig(tmp_path / "early")
    (tmp_path / "early").mkdir(exist_ok=True)
    await early.start()
    await early.say("End the interview.")
    assert early.last.text == "Thank you for your time, Nyla. We'll end the interview here."
    deliver(early.session)
    assert early.rows("coverage_summary")[-1]["summary"]["end_reason"] == "ended_early"
    timed = Rig(tmp_path / "timed", minutes=5)
    (tmp_path / "timed").mkdir(exist_ok=True)
    await timed.start()
    timed.now[0] += 100000
    await timed.say("I built the ingestion service.")
    assert timed.last.kind is UtteranceKind.CLOSING
    deliver(timed.session)
    assert timed.rows("coverage_summary")[-1]["summary"]["end_reason"] == "time_budget"
    for rig in (done, early, timed):
        text = " ".join(
            u.text
            for u in rig.spoken
            if u.kind in {UtteranceKind.CLOSING, UtteranceKind.EARLY_FEEDBACK}
        )
        assert not [word for word in forbidden if word in text.lower()]
    # A spoken name correction is used in the closing; an unnameable name is simply left out.
    named = Rig(tmp_path / "named", name="Drake Example")
    (tmp_path / "named").mkdir(exist_ok=True)
    await named.start()
    await named.say("My name is Drayke, not Drake.")
    deliver(named.session)
    await named.say("End the interview.")
    assert "Drayke" in named.last.text
    anon = Rig(tmp_path / "anon", name=None)
    (tmp_path / "anon").mkdir(exist_ok=True)
    await anon.start()
    await anon.say("End the interview.")
    assert anon.last.text == "Thank you for your time. We'll end the interview here."


@pytest.mark.asyncio
async def test_an_end_request_is_detected_mid_turn_and_ambiguity_is_not(tmp_path):
    rig = Rig(tmp_path)
    await rig.start()
    await rig.session.user_started()  # the candidate is still speaking
    assert await rig.session.detect_stop_from_segments("I'm done.") is False
    assert await rig.session.detect_stop_from_segments("Please don't end the interview.") is False
    assert not rig.session._stop_latched
    handled = await rig.session.detect_stop_from_segments(
        "Oh, actually, I'm done with the interview. Can you just give me the feedback?"
    )
    assert handled and rig.session._stop_latched
    assert rig.rows("early_end_requested")[-1]["end_reason"] == "user_request"
    await rig.session.user_stopped(
        "Oh, actually, I'm done with the interview. Can you give feedback?"
    )
    await settle()
    assert rig.last.kind is UtteranceKind.EARLY_FEEDBACK and rig.decisions_made() == 0
    assert not rig.rows("feedback_request_declined")
