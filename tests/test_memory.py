"""Candidate facts and corrections through the session routing entry point."""

import asyncio

import pytest

from rehearse_ai.interview import Stage, UtteranceKind
from rehearse_ai.session import InterviewSession
from rehearse_ai.session_log import SessionLog


def deliver(session):
    utterance = session.controller.pending
    context = f"memory-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def question_one(session):
    await session.start()
    deliver(session)
    session.controller.submit_greeting("I'm doing well")
    await session._queue_speech(session.controller.prepare_readiness("Good to hear."))
    deliver(session)
    await session._queue_speech(session.controller.submit_readiness(True))
    deliver(session)
    assert session.controller.current_question_number == 1
    assert session.controller.stage is Stage.AWAITING_ANSWER


@pytest.mark.asyncio
async def test_manual_name_confirmation_is_authoritative_but_explicit_correction_can_change_it(
    tmp_path,
):
    async def infer(_kind, _payload):
        return {"action": "wait", "text": ""}

    session = InterviewSession(
        SessionLog(tmp_path, "name"), infer, lambda _: settle(), lambda _: None
    )
    session._remember_spoken_name("Devanj", "My name is Devanj")
    tentative = session.controller.memory.get("candidate_name")
    assert tentative.value == "Devanj" and not tentative.confirmed
    await question_one(session)
    await session.user_started()
    await session.user_stopped("It's Divansh, not Devanj.")
    assert session.controller.preferred_name == "Divansh"
    assert session.controller.pending.text == "Thanks, I'll call you Divansh."
    assert session.controller.memory.corrections[-1].source_turn == 1
    deliver(session)
    assert await session.confirm_name("Divansh")
    session._remember_spoken_name("Devanj", "My name is Devanj")
    assert session.controller.preferred_name == "Divansh"
    assert session.controller.memory.get("candidate_name").confirmed
    await session.user_started()
    await session.user_stopped("It's Divans, not Divansh.")
    assert session.controller.preferred_name == "Divans"
    assert session.controller.answers[0] == ()
    assert session.controller.memory.corrections[-1].source_turn == 2
    await session.close()


@pytest.mark.asyncio
async def test_spoken_technical_correction_does_not_spend_probe_or_advance(tmp_path):
    calls = []

    async def infer(kind, payload):
        calls.append((kind, payload))
        return {"action": "follow_up", "text": "What did you build?", "facts": []}

    session = InterviewSession(
        SessionLog(tmp_path, "terms"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    session.controller.memory.supply(
        "project_purpose",
        "It uses web books to review PRs",
        source_turn=1,
        supplied_wording="It uses web books to review PRs",
    )
    await session.user_started()
    await session.user_stopped("I said webhooks, not web books.")
    assert (
        session.controller.memory.get("project_purpose").value == "It uses webhooks to review PRs"
    )
    assert session.controller.memory.get("project_purpose").confirmed
    assert session.controller.memory.corrections[-1].superseded_wording == "web books"
    assert session.controller.answers[0] == ()
    assert session.controller.remaining_follow_ups == 1
    assert session.controller.pending.kind is UtteranceKind.CORRECTION_ACK
    assert calls == []
    deliver(session)
    assert session.controller.stage is Stage.AWAITING_ANSWER
    await session.user_started()
    await session.user_stopped("I built a GitHub pull-request reviewer.")
    await settle()
    assert calls[-1][1]["session_memory"]["project_purpose"]["value"] == (
        "It uses webhooks to review PRs"
    )
    assert session.controller.remaining_follow_ups == 0
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn",
    [
        "I said webhooks, not web books. I built a GitHub pull-request app.",
        "I built a GitHub pull-request app. I said webhooks, not web books.",
    ],
)
async def test_correction_with_new_answer_content_processes_content_once(tmp_path, turn):
    calls = []

    async def infer(kind, payload):
        calls.append((kind, payload))
        return {"action": "follow_up", "text": "What was your role?", "facts": []}

    session = InterviewSession(
        SessionLog(tmp_path, "combined"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped(turn)
    await settle()
    assert session.controller.memory.get("technical_terms").value == "webhooks"
    assert session.controller.answers[0] == ("I built a GitHub pull-request app.",)
    assert len(calls) == 1
    assert session.controller.remaining_follow_ups == 0
    await session.close()


@pytest.mark.asyncio
async def test_ambiguous_correction_asks_once_without_becoming_an_answer(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("Ambiguous correction must not infer an interview answer")

    session = InterviewSession(
        SessionLog(tmp_path, "ambiguous"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("It's MySQL, not PostgreSQL.")
    assert session.controller.pending.kind is UtteranceKind.CORRECTION_ACK
    assert "Which detail" in session.controller.pending.text
    deliver(session)
    assert session.controller.stage is Stage.AWAITING_ANSWER
    await session.user_started()
    await session.user_stopped("The technology")
    assert session.controller.memory.get("technologies").value == "MySQL"
    assert session.controller.remaining_follow_ups == 1
    assert session.controller.answers[0] == ()
    await session.close()


@pytest.mark.asyncio
async def test_explicit_technology_correction_and_bounded_ambiguous_repetition(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("A correction alone must not invoke decision inference")

    session = InterviewSession(
        SessionLog(tmp_path, "technology"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    session.controller.memory.supply(
        "project_purpose",
        "I used PostgreSQL for event storage",
        source_turn=1,
        supplied_wording="I used PostgreSQL for event storage",
    )
    await session.user_started()
    await session.user_stopped("Actually, I used MySQL, not PostgreSQL.")
    assert session.controller.memory.get("technologies").value == "MySQL"
    assert session.controller.memory.get("project_purpose").value == (
        "I used MySQL for event storage"
    )
    deliver(session)
    await session.user_started()
    await session.user_stopped("It's Redis, not Memcached.")
    assert "Which detail" in session.controller.pending.text
    deliver(session)
    await session.user_started()
    await session.user_stopped("It's Redis, not Memcached.")
    assert "couldn't place" in session.controller.pending.text
    assert session.controller.memory.get("technical_terms") is None
    assert session.controller.remaining_follow_ups == 1
    await session.close()


@pytest.mark.asyncio
async def test_stop_precedes_correction_and_repeat_preserves_question(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("No decision inference expected")

    session = InterviewSession(
        SessionLog(tmp_path, "controls"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("It's Divansh, not Devanj.")
    deliver(session)
    await session.user_started()
    await session.user_stopped("Please repeat the question.")
    assert session.controller.pending.kind is UtteranceKind.REPEAT
    assert session.controller.remaining_follow_ups == 1
    await session.user_started()
    await session.user_stopped("Stop the interview.")
    assert session.controller.stage is Stage.DELIVERING
    assert session.controller.pending.kind is UtteranceKind.EARLY_FEEDBACK
    await session.close()


@pytest.mark.asyncio
async def test_facts_require_literal_latest_turn_and_keep_unknown_unknown(tmp_path):
    async def infer(_kind, _payload):
        return {
            "action": "follow_up",
            "text": "What did you implement?",
            "facts": [
                {"field": "project_name", "wording": "PR Guard"},
                {"field": "technologies", "wording": "PostgreSQL"},
            ],
        }

    session = InterviewSession(
        SessionLog(tmp_path, "facts"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("I built PR Guard for GitHub pull requests.")
    await settle()
    assert session.controller.memory.get("project_name").value == "PR Guard"
    assert session.controller.memory.get("project_name").source_turn == 1
    assert session.controller.memory.get("technologies") is None
    await session.close()


@pytest.mark.asyncio
async def test_model_cannot_restore_superseded_stt_wording_as_a_fact(tmp_path):
    async def infer(_kind, _payload):
        return {
            "action": "follow_up",
            "text": "What did you build yourself?",
            "facts": [{"field": "project_purpose", "wording": "using web books to receive events"}],
        }

    session = InterviewSession(
        SessionLog(tmp_path, "superseded"), infer, lambda _: settle(), lambda _: None
    )
    session.controller.memory.supply(
        "technical_terms",
        "webhooks",
        source_turn=1,
        supplied_wording="I said webhooks, not web books",
        confirmed=True,
        correction=True,
        superseded_wording="web books",
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("I built a GitHub app using web books to receive events.")
    await settle()
    assert session.controller.memory.get("project_purpose") is None
    assert session.controller.pending.text == "What did you build yourself?"
    await session.close()


@pytest.mark.asyncio
async def test_correction_cancels_obsolete_answer_decision(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def infer(_kind, _payload):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"action": "advance", "text": "", "facts": []}

    session = InterviewSession(
        SessionLog(tmp_path, "obsolete"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("I built a GitHub pull-request reviewer.")
    await entered.wait()
    await session.user_started()
    await session.user_stopped("I said webhooks, not web books.")
    release.set()
    await settle()
    assert session.controller.current_question_number == 1
    assert session.controller.pending.kind is UtteranceKind.CORRECTION_ACK
    assert session.controller.memory.get("technical_terms").value == "webhooks"
    assert session.controller.answers[0] == ("I built a GitHub pull-request reviewer.",)
    deliver(session)
    await settle()
    assert session.controller.pending is not None
    assert session.controller.current_question_number == 1
    await session.close()


@pytest.mark.asyncio
async def test_manual_name_change_discards_an_obsolete_decision(tmp_path):
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    async def infer(_kind, payload):
        calls.append(payload)
        if len(calls) == 1:
            first_entered.set()
            try:
                await release_first.wait()
            except asyncio.CancelledError:
                await release_first.wait()
            return {"action": "advance", "text": "", "facts": []}
        return {"action": "follow_up", "text": "What did you own?", "facts": []}

    session = InterviewSession(
        SessionLog(tmp_path, "manual-stale"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("I built a GitHub pull-request reviewer.")
    await first_entered.wait()
    assert await session.confirm_name("Maya")
    await settle()
    assert calls[1]["session_memory"]["candidate_name"]["value"] == "Maya"
    assert session.controller.pending.text == "What did you own?"
    release_first.set()
    await settle()
    assert session.controller.current_question_number == 1
    assert session.controller.pending.text == "What did you own?"
    await session.close()


@pytest.mark.asyncio
async def test_correction_during_unheard_question_replays_that_question(tmp_path):
    async def infer(_kind, _payload):
        raise AssertionError("Correction alone must not invoke the decision model")

    session = InterviewSession(
        SessionLog(tmp_path, "unheard"), infer, lambda _: settle(), lambda _: None
    )
    await session.start()
    deliver(session)
    session.controller.submit_greeting("I'm well")
    await session._queue_speech(session.controller.prepare_readiness("Good."))
    deliver(session)
    await session._queue_speech(session.controller.submit_readiness(True))
    unheard = session.controller.pending
    await session.user_started()
    await session.user_stopped("I said webhooks, not web books.")
    assert session.controller.pending.kind is UtteranceKind.CORRECTION_ACK
    deliver(session)
    await settle()
    assert session.controller.pending.kind is UtteranceKind.QUESTION
    assert session.controller.pending.id != unheard.id
    assert session.controller.answers[0] == ()
    await session.close()


@pytest.mark.asyncio
async def test_interrupted_correction_ack_does_not_become_the_project_answer(tmp_path):
    async def infer(_kind, _payload):
        return {"action": "follow_up", "text": "What did you own?", "facts": []}

    session = InterviewSession(
        SessionLog(tmp_path, "ack-barge"), infer, lambda _: settle(), lambda _: None
    )
    await question_one(session)
    await session.user_started()
    await session.user_stopped("I said webhooks, not web books.")
    ack_id = session.controller.pending.id
    await session.user_started()
    await session.user_stopped("I built a GitHub webhook processor.")
    await settle()
    assert session.controller.answers[0] == ("I built a GitHub webhook processor.",)
    assert session.controller.pending.kind is UtteranceKind.FOLLOW_UP
    assert session.controller.pending.id != ack_id
    assert session.controller.memory.get("technical_terms").value == "webhooks"
    await session.close()


@pytest.mark.asyncio
async def test_project_memory_correction_and_missing_detail_follow_up(tmp_path):
    calls = []

    async def infer(_kind, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "action": "follow_up",
                "text": "What did you build yourself?",
                "facts": [
                    {"field": "project_name", "wording": "PR Guard"},
                    {"field": "project_purpose", "wording": "posts risk reports as PR comments"},
                ],
                "project_status": "technical_example",
            }
        return {"action": "advance", "text": "", "facts": []}

    session = InterviewSession(
        SessionLog(tmp_path, "acceptance"), infer, lambda _: settle(), lambda _: None
    )
    session._remember_spoken_name("Mia", "My name is Mia")
    assert await session.confirm_name("Maya")
    await question_one(session)
    await session.user_started()
    await session.user_stopped("I built PR Guard; it posts risk reports as PR comments.")
    await settle()
    assert session.controller.memory.get("project_purpose").value == (
        "posts risk reports as PR comments"
    )
    assert session.controller.pending.text == "What did you build yourself?"
    deliver(session)
    await session.user_started()
    await session.user_stopped("I said webhooks, not web books.")
    assert session.controller.remaining_follow_ups == 0
    deliver(session)
    await session.user_started()
    await session.user_stopped("I implemented the webhook handler and wrote its tests.")
    await settle()
    assert calls[1]["session_memory"]["candidate_name"] == {
        "value": "Maya",
        "confirmed": True,
    }
    assert calls[1]["session_memory"]["technical_terms"]["value"] == "webhooks"
    assert calls[1]["correction_overlays"][-1] == {
        "field": "technical_terms",
        "superseded": "web books",
        "current": "webhooks",
    }
    assert calls[1]["session_memory"]["project_purpose"]["value"] == (
        "posts risk reports as PR comments"
    )
    assert session.controller.pending.text == "What was the hardest technical problem in it?"
    assert session.controller.preferred_name == "Maya"
    await session.close()
