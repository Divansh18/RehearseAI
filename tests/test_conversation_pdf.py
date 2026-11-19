"""The Conversation PDF: content, honesty of labels, layout and historical compatibility."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from rehearse_ai.records.conversation_pdf import (
    build_pdf,
    completion_status,
    fit_image,
    question_rows,
)

START = datetime(2026, 11, 17, 13, 19, 4, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return START + timedelta(seconds=seconds)


def record(**overrides):
    base = {
        "id": str(uuid.uuid4()),
        "candidate_email": "priya.raman@example.com",
        "candidate_name": "Priya Raman",
        "timezone": "Asia/Kolkata",
        "interview_title": "Senior Backend Engineer interview",
        "created_at": START,
        "started_at": START,
        "ended_at": at(176),
        "status": "completed",
        "termination_reason": "normal_completion",
    }
    base.update(overrides)
    return base


def agent(seconds, text, status="output_completed", kind=None, number=None):
    return {
        "speaker": "agent",
        "timestamp_utc": at(seconds),
        "text": text,
        "status": status,
        "interrupted": status in {"interrupted", "cancelled"},
        "utterance_kind": kind,
        "question_number": number,
    }


def candidate(seconds, text):
    return {
        "speaker": "candidate",
        "timestamp_utc": at(seconds),
        "text": text,
        "status": "machine_transcription",
        "interrupted": False,
    }


def render(tmp_path, rec, entries, name="record.pdf"):
    path = tmp_path / "private" / name
    build_pdf(path, rec, entries)
    reader = PdfReader(path)
    return path, reader, "\n".join(page.extract_text() for page in reader.pages)


def jpeg(path: Path, size=(1280, 651)) -> Path:
    Image.new("RGB", size, (20, 60, 70)).save(path, "JPEG")
    return path


def test_header_uses_candidate_email_and_states_title_date_duration_and_status(tmp_path):
    _, reader, text = render(tmp_path, record(), [agent(0, "Hello", kind="greeting")])
    assert "Candidate email" in text and "priya.raman@example.com" in text
    assert "Candidate account" not in text
    assert "Senior Backend Engineer interview" in text and "Priya Raman" in text
    assert "17 Nov 2026, 18:49 IST" in text  # the assignment's timezone, named
    assert "2m 56s" in text and "Completed" in text
    assert "Page 1 of 1" in text and reader.metadata.title == "Conversation PDF"


@pytest.mark.parametrize(
    ("status", "reason", "coverage", "expected"),
    [
        ("completed", "normal_completion", None, "Completed"),
        (
            "completed",
            "normal_completion",
            {"end_reason": "time_budget"},
            "planned time was reached",
        ),
        ("ended_early", "spoken_early_end", None, "Ended early at the candidate's request"),
        (
            "interrupted",
            "session_disconnected",
            None,
            "Interrupted before completion (the connection ended)",
        ),
        ("interrupted", "server_restart", None, "(the server restarted)"),
        ("failed", "pipeline_failed", None, "Failed (the voice pipeline stopped)"),
        ("interrupted", None, None, "Interrupted before completion"),
    ],
)
def test_status_names_only_recorded_outcomes(status, reason, coverage, expected):
    assert expected in completion_status(
        {"status": status, "termination_reason": reason, "coverage": coverage}
    )
    # Nothing is invented: an unknown reason adds no explanation.
    assert "(" not in completion_status({"status": "interrupted", "termination_reason": "other"})


def test_partial_interviews_say_so_and_complete_ones_do_not(tmp_path):
    for status, partial in (
        ("completed", False),
        ("ended_early", True),
        ("interrupted", True),
        ("failed", True),
    ):
        _, _, text = render(tmp_path, record(status=status), [candidate(5, "Hi")], f"{status}.pdf")
        assert ("partial interview conversation" in text) is partial


def test_coverage_comes_only_from_recorded_evidence():
    coverage = {
        "planned": 6,
        "asked": [1, 2, 3],
        "answered": [1, 2],
        "no_knowledge_or_declined": [2],
        "covered_from_prior_turns": [],
        "skipped_for_time": [5],
        "required_not_reached": [4, 6],
        "optional_not_reached": [],
    }
    rows = question_rows(
        {"coverage": coverage, "question_topics": ["Background", "Idempotency", "Disagreement"]}
    )
    outcomes = {n: outcome for n, _, outcome in rows}
    assert outcomes == {
        1: "Answered",
        2: "Declined or said they did not know",
        3: "Asked; no answer recorded",  # asked, nothing recorded: not called "skipped"
        4: "Not reached (required)",
        5: "Skipped to respect the time budget",
        6: "Not reached (required)",
    }
    assert [topic for _, topic, _ in rows][:4] == ["Background", "Idempotency", "Disagreement", ""]
    assert question_rows({}) is None and question_rows({"coverage": {"asked": [1]}}) is None


def test_missing_coverage_is_stated_not_guessed(tmp_path):
    _, _, text = render(tmp_path, record(), [candidate(5, "Hi")])
    assert "Question coverage was not recorded for this interview." in text
    assert "Answered" not in text and "Declined" not in text


def test_conversation_labels_only_where_the_data_supports_them(tmp_path):
    entries = [
        agent(0, "Hi Priya, how's your day going?", kind="greeting"),
        candidate(7, "Good."),
        agent(12, "Describe your role.", kind="question", number=1),
        candidate(30, "I led the ledger redesign."),
        agent(33, "How did you cut latency?", kind="follow_up", number=1),
        agent(40, "How did you cut latency?", kind="repeat", number=1),
        agent(50, "I can't provide interview feedback.", kind="information", number=2),
        agent(60, "An unlabelled historical entry"),  # no kind was recorded
        agent(70, "That concludes the interview.", kind="closing"),
    ]
    _, _, text = render(tmp_path, record(), entries)
    for label in (
        "Opening",
        "Question 1",
        "Follow-up on question 1",
        "Question 1 repeated",
        "Reply to a side request",
        "Closing",
    ):
        assert label in text
    assert text.index("Describe your role") < text.index("I led the ledger redesign")
    entry = text[
        text.index("An unlabelled historical entry") - 60 : text.index(
            "An unlabelled historical entry"
        )
    ]
    assert "Question" not in entry and "Follow-up" not in entry
    # Candidate entries carry no question labels: that was never recorded for them.
    assert re.search(r"CANDIDATE\s+·\s+\+00:30\s*\n", text)
    assert "+00:12" in text and "+01:10" in text


def test_output_that_was_not_delivered_is_never_shown_as_delivered(tmp_path):
    entries = [
        agent(1, "Fully spoken.", status="output_completed"),
        agent(2, "Cut off mid-way.", status="interrupted"),
        agent(3, "Never reached speech.", status="prepared"),
        agent(4, "Started only.", status="output_started"),
        agent(5, "Cancelled one.", status="cancelled"),
        agent(6, "Failed one.", status="failed"),
    ]
    _, _, text = render(tmp_path, record(), entries)
    assert "Interrupted; only part of it may have been heard" in text
    assert "Prepared; speech not observed" in text
    assert "Speech started; completion not confirmed" in text
    assert "Cancelled before speech" in text and "Speech failed" in text
    blocks = {}
    for block in re.split(r"INTERVIEWER\s+·\s+\+\d\d:\d\d", text)[1:]:
        for body in ("Fully spoken.", "Cut off mid-way.", "Never reached speech.", "Started only."):
            if body in block:
                blocks[body] = block
    delivered = blocks["Fully spoken."]
    assert not any(tag in delivered for tag in ("Interrupted", "Prepared", "started", "Cancelled"))
    assert "Interrupted" in blocks["Cut off mid-way."]
    assert "Prepared; speech not observed" in blocks["Never reached speech."]
    assert "Speech started; completion" in blocks["Started only."]


def test_machine_transcription_is_preserved_exactly_including_corrections(tmp_path):
    spoken = "I used web books, I said webhooks, not web books."
    _, _, text = render(tmp_path, record(), [candidate(5, spoken)])
    assert spoken in text.replace("\n", " ")
    assert "machine transcription" in text.lower()


def test_unicode_html_and_long_answers_paginate_without_losing_text(tmp_path):
    long_answer = (
        "Zoë García <script>alert(1)</script> "
        + "Résultats mesurés très bien, и это сработало. " * 60
    )
    entries = [candidate(10 + i, long_answer) for i in range(20)]
    _, reader, text = render(tmp_path, record(), entries)
    flat = " ".join(text.split())
    assert len(reader.pages) > 3 and "<script>alert(1)</script>" in flat
    assert flat.count("и это сработало") >= 20 * 59  # nothing dropped by wrapping
    assert re.search(rf"Page {len(reader.pages)} of {len(reader.pages)}", text)
    assert "Page 1 of" in text


def test_a_minimal_record_with_missing_metadata_still_renders(tmp_path):
    _, _, text = render(
        tmp_path,
        {"id": str(uuid.uuid4()), "candidate_email": "x@example.com", "status": "interrupted"},
        [
            {
                "speaker": "candidate",
                "text": "",
                "timestamp_utc": None,
                "status": "machine_transcription",
            }
        ],
    )
    assert "x@example.com" in text and "Not recorded" in text and "[No final text]" in text
    assert "time not recorded" in text and "No interview image was saved" in text
    _, _, empty = render(tmp_path, record(), [], "empty.pdf")
    assert "No conversation was saved for this interview." in empty


def test_the_record_contains_no_assessment_language(tmp_path):
    _, _, text = render(tmp_path, record(), [candidate(5, "I built a service.")])
    lowered = text.lower()
    assert "contains no scores, hiring recommendation or assessment" in lowered
    for banned in ("recommend hiring", "strength", "weakness", "rating", "personality", "summary"):
        assert banned not in lowered


@pytest.mark.parametrize(
    ("kind", "heading", "caption"),
    [
        ("room", "INTERVIEW ROOM IMAGE", "browser-rendered view of the interview room"),
        ("camera", "CANDIDATE CAMERA SNAPSHOT", "Camera only: the interview-room image could not"),
        (None, "CANDIDATE CAMERA SNAPSHOT", "from before interview-room images were introduced"),
    ],
)
def test_images_are_captioned_as_exactly_what_they_are(tmp_path, kind, heading, caption):
    image = jpeg(tmp_path / "image.jpg")
    _, reader, text = render(
        tmp_path,
        record(snapshot_path=image, snapshot_kind=kind, snapshot_captured_at=at(15)),
        [candidate(5, "Hi")],
    )
    flat = " ".join(text.split())
    assert heading in flat and caption in flat
    assert "not identity verification or tamper-proof evidence" in flat
    if kind != "room":  # an old or fallback still is never presented as the interview room
        assert "INTERVIEW ROOM IMAGE" not in flat
        assert "not an operating-system screenshot" not in flat
    assert sum(len(page.images) for page in reader.pages) == 1


def test_a_missing_image_file_or_record_is_stated_and_does_not_block_the_pdf(tmp_path):
    gone = tmp_path / "gone.jpg"  # recorded as available, file missing from disk
    _, _, text = render(
        tmp_path, record(snapshot_path=gone, snapshot_kind="room"), [candidate(5, "Hi")]
    )
    assert "No interview image was saved" in text and "conversation record below" in text


def test_the_image_keeps_the_rooms_proportions_for_wide_and_tall_rooms():
    for width, height in ((1280, 651), (748, 1480), (1000, 1000)):
        shown_w, shown_h = fit_image(width, height)
        assert shown_w / shown_h == pytest.approx(width / height)
        assert shown_h <= 235.01 and shown_w <= 503


def test_the_final_page_is_never_nearly_empty_across_conversation_lengths(tmp_path):
    image = jpeg(tmp_path / "image.jpg")
    for count in range(1, 33):
        entries = []
        for i in range(count):
            entries.append(
                agent(
                    i * 10,
                    f"Question number {i} about reliability and design.",
                    kind="question",
                    number=i + 1,
                )
            )
            entries.append(candidate(i * 10 + 5, ("An answer with real substance. " * (1 + i % 5))))
        rec = record(snapshot_path=image, snapshot_kind="room", snapshot_captured_at=at(15))
        _, reader, _ = render(tmp_path, rec, entries, f"n{count}.pdf")
        if len(reader.pages) < 2:
            continue
        lines = [
            line
            for line in reader.pages[-1].extract_text().splitlines()
            if line.strip()
            and "RehearseAI" not in line
            and "Priya Raman" not in line
            and not line.startswith("Page ")
        ]
        assert len(lines) >= 4, (count, len(reader.pages), lines)  # at least two exchanges
        header_lines = [
            line
            for line in reader.pages[-1].extract_text().splitlines()
            if line.strip() == "CONVERSATION"
        ]
        assert not header_lines or len(lines) > 3  # a heading is never alone at a page's foot


def test_conversation_heading_never_stands_alone_at_the_foot_of_a_page(tmp_path):
    image = jpeg(tmp_path / "image.jpg")
    for count in range(1, 12):
        entries = [candidate(i * 5, "Short answer. " * (i + 1)) for i in range(count)]
        _, reader, _ = render(
            tmp_path,
            record(snapshot_path=image, snapshot_kind="room", coverage={"planned": 6 + count % 4}),
            entries,
            f"h{count}.pdf",
        )
        for page in reader.pages:
            lines = [line for line in page.extract_text().splitlines() if line.strip()]
            body = [
                line for line in lines if "RehearseAI" not in line and not line.startswith("Page ")
            ]
            if "CONVERSATION" in body:
                assert body.index("CONVERSATION") < len(body) - 3


def test_pdf_is_private_and_atomic(tmp_path):
    path, _, _ = render(tmp_path, record(), [candidate(5, "Hi")])
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob(".*tmp*"))
