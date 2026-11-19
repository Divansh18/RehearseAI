import asyncio

import pytest
from support import plan_dict

from rehearse_ai.interview.session import InterviewSession
from rehearse_ai.interview.session_log import SessionLog


@pytest.fixture
def harness(tmp_path):
    spoken = []
    published = []
    decisions = []

    async def infer(kind, payload):
        decisions.append((kind, payload))
        if kind == "feedback":
            return {
                "lines": [
                    "Your parser story showed clear ownership.",
                    "Add a concrete result next time.",
                ]
            }
        return {"action": "advance", "text": ""}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "test-session"),
        infer,
        speak,
        published.append,
        interview_plan=plan_dict(),
        opening=False,
    )
    return session, spoken, published, decisions


def deliver(session):
    utterance = session.controller.pending
    context = f"context-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()
    session.tts_finished(context)
    session.bot_stopped()


def begin_output(session):
    utterance = session.controller.pending
    context = f"context-{utterance.id}"
    session.tts_started(utterance.id, context)
    session.tts_first_audio(context)
    session.bot_started()


async def start_questions(session):
    await session.start()


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_barge_in_cancels_obsolete_inference_and_keeps_answer(tmp_path):
    future = asyncio.Event()
    spoken = []

    async def infer(_kind, _payload):
        await future.wait()
        return {"action": "follow_up", "text": "Obsolete question?"}

    async def speak(utterance):
        spoken.append(utterance)

    session = InterviewSession(
        SessionLog(tmp_path, "cancel"),
        infer,
        speak,
        lambda _: None,
        interview_plan=plan_dict(),
        opening=False,
    )
    await start_questions(session)
    deliver(session)
    await session.user_started()
    await session.user_stopped("I built a compiler")
    await settle()
    pending = session.response_task
    await session.user_started()
    assert pending.cancelling()
    await session.user_stopped("and measured its compile time")
    assert session.controller.answers[0] == ("I built a compiler and measured its compile time",)
    assert len(spoken) == 1  # Question one only: the obsolete follow-up was never spoken.
    await session.close()


@pytest.mark.asyncio
async def test_empty_barge_in_retries_with_new_id(harness):
    session, spoken, _published, _decisions = harness
    await start_questions(session)
    first = spoken[-1]
    await session.user_started()
    await session.user_stopped("")
    assert spoken[-1].id != first.id
    assert spoken[-1].text == first.text
    assert not session.controller.delivery_finished(first.id)
    await session.close()
