"""Private Conversation PDF, rendered only from persisted interview evidence.

A light, print-friendly record with a restrained teal accent. Nothing here is inferred: a status,
label or coverage entry appears only where it was recorded, and the document carries no scores,
recommendations, personality assessments or summaries. Candidate wording is the stored machine
transcription. Interviewer text is labelled by what the server observed, so prepared or interrupted
speech is never shown as if it had been delivered in full.
"""

import os
import uuid
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image as PillowImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as PdfImage,
)

FONT = Path(__file__).resolve().parent / "fonts" / "NotoSans-Regular.ttf"
TEXT_FONT = "RehearseNoto"
LABEL_FONT = "Helvetica-Bold"  # static labels only; all candidate and model text uses TEXT_FONT

INK = colors.HexColor("#1b2428")
MUTED = colors.HexColor("#5d6a6f")
FAINT = colors.HexColor("#8a979b")
RULE = colors.HexColor("#d8e1e2")
ACCENT = colors.HexColor("#0f766e")
ACCENT_SOFT = colors.HexColor("#eaf4f3")
PANEL = colors.HexColor("#f5f8f8")
CAUTION = colors.HexColor("#8a5a00")

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 46
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X

# What the interviewer's text was, by what was recorded. Anything else is left unlabelled.
KIND_LABELS = {
    "greeting": ("Opening", "Opening"),
    "opening_ack": ("Opening", "Opening"),
    "question": ("Question {n}", "Question"),
    "follow_up": ("Follow-up on question {n}", "Follow-up"),
    "nudge": ("Follow-up on question {n}", "Follow-up"),
    "clarification": ("Clarification on question {n}", "Clarification"),
    "repeat": ("Question {n} repeated", "Question repeated"),
    "information": ("Reply to a side request", "Reply to a side request"),
    "closing": ("Closing", "Closing"),
    "early_feedback": ("Closing", "Closing"),
}
OUTPUT_STATES = {
    "prepared": "Prepared; speech not observed",
    "output_started": "Speech started; completion not confirmed",
    "cancelled": "Cancelled before speech",
    "interrupted": "Interrupted; only part of it may have been heard",
    "failed": "Speech failed",
}
STATUS_TEXT = {
    "completed": "Completed",
    "ended_early": "Ended early at the candidate's request",
    "interrupted": "Interrupted before completion",
    "failed": "Failed",
    "in_progress": "In progress",
    "starting": "Starting",
}
REASON_TEXT = {
    "session_disconnected": "the connection ended",
    "server_restart": "the server restarted",
    "pipeline_failed": "the voice pipeline stopped",
    "signalling_failed": "the connection could not be established",
    "signalling_rejected": "the connection was not accepted",
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(name: str | None) -> tuple[Any, str]:
    if name:
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return UTC, "UTC"


def _when(value: datetime | None, zone_name: str | None = None) -> str:
    instant = _utc(value)
    if instant is None:
        return "Not recorded"
    zone, _ = _zone(zone_name)
    local = instant.astimezone(zone)
    return local.strftime("%d %b %Y, %H:%M ") + (local.tzname() or "UTC")


def _duration(started: datetime | None, ended: datetime | None) -> str:
    if not started or not ended:
        return "Not available"
    return _seconds_text(int((_utc(ended) - _utc(started)).total_seconds()))


def _seconds_text(seconds: int) -> str:
    if seconds < 0:
        return "Not available"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes:d}m {seconds:02d}s"


def _offset(value: datetime | None, started: datetime | None) -> str:
    stamp, origin = _utc(value), _utc(started)
    if stamp and origin and stamp >= origin:
        elapsed = int((stamp - origin).total_seconds())
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        return (
            f"+{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"+{minutes:02d}:{seconds:02d}"
        )
    return "time not recorded"


def completion_status(record: dict[str, Any]) -> str:
    """The recorded outcome in words. Only recorded reasons are named; none is guessed."""
    status = str(record.get("status", ""))
    text = STATUS_TEXT.get(status, status.replace("_", " ").capitalize() or "Unknown")
    reason = REASON_TEXT.get(str(record.get("termination_reason") or ""))
    coverage = record.get("coverage") or {}
    if status == "completed" and coverage.get("end_reason") == "time_budget":
        text = "Completed; the planned time was reached"
    elif status in {"interrupted", "failed"} and reason:
        text = f"{text} ({reason})"
    return text


def _is_partial(record: dict[str, Any]) -> bool:
    return str(record.get("status")) in {"ended_early", "interrupted", "failed"}


def question_rows(record: dict[str, Any]) -> list[tuple[int, str, str]] | None:
    """Per-question outcome from the recorded coverage summary, or None when it was not recorded."""
    coverage = record.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(coverage.get("planned"), int):
        return None

    def numbers(key: str) -> set[int]:
        value = coverage.get(key)
        return {n for n in value if type(n) is int} if isinstance(value, list) else set()

    declined = numbers("no_knowledge_or_declined")
    answered = numbers("answered")
    covered = numbers("covered_from_prior_turns")
    skipped = numbers("skipped_for_time")
    required_missed = numbers("required_not_reached")
    optional_missed = numbers("optional_not_reached")
    asked = numbers("asked")
    topics = record.get("question_topics") or []
    rows = []
    for n in range(1, min(coverage["planned"], 60) + 1):
        if n in declined:
            outcome = "Declined or said they did not know"
        elif n in answered:
            outcome = "Answered"
        elif n in covered:
            outcome = "Covered by an earlier answer"
        elif n in skipped:
            outcome = "Skipped to respect the time budget"
        elif n in required_missed:
            outcome = "Not reached (required)"
        elif n in optional_missed:
            outcome = "Not reached"
        elif n in asked:
            outcome = "Asked; no answer recorded"
        else:
            outcome = "No record"
        topic = topics[n - 1] if n - 1 < len(topics) and isinstance(topics[n - 1], str) else ""
        rows.append((n, topic, outcome))
    return rows


def _label_for(entry: dict[str, Any]) -> str | None:
    """A label for an interviewer entry, only where its kind was recorded."""
    if entry["speaker"] == "candidate":
        return None
    labels = KIND_LABELS.get(str(entry.get("utterance_kind") or ""))
    if labels is None:
        return None
    with_number, without = labels
    number = entry.get("question_number")
    return with_number.format(n=number) if type(number) is int else without


class _NumberedCanvas(pdf_canvas.Canvas):
    """Draws "Page X of Y" once the page count is known."""

    def __init__(self, *args: Any, footer_note: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved: list[dict[str, Any]] = []
        self.footer_note = footer_note

    def showPage(self) -> None:  # noqa: N802 - reportlab API
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        self.saveState()
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(MARGIN_X, 40, PAGE_WIDTH - MARGIN_X, 40)
        self.setFont(TEXT_FONT, 7.5)
        self.setFillColor(FAINT)
        self.drawString(MARGIN_X, 27, self.footer_note)
        self.drawRightString(PAGE_WIDTH - MARGIN_X, 27, f"Page {self._pageNumber} of {total}")
        self.restoreState()


def build_pdf(path: Path, record: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Write one id-derived PDF atomically. No browser text or model summary is used."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if TEXT_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(TEXT_FONT, str(FONT)))

    base = ParagraphStyle(
        "base", fontName=TEXT_FONT, fontSize=9.5, leading=14, textColor=INK, splitLongWords=1
    )
    muted = ParagraphStyle("muted", parent=base, fontSize=8, leading=11.5, textColor=MUTED)
    faint = ParagraphStyle("faint", parent=muted, textColor=FAINT)
    heading = ParagraphStyle(
        "heading", parent=base, fontName=LABEL_FONT, fontSize=10, leading=14, textColor=ACCENT
    )
    title = ParagraphStyle("title", parent=base, fontSize=21, leading=26, textColor=INK)
    subtitle = ParagraphStyle("subtitle", parent=base, fontSize=10.5, leading=15, textColor=MUTED)
    key = ParagraphStyle("key", parent=muted, fontName=LABEL_FONT, fontSize=7.5, textColor=MUTED)
    interviewer_label = ParagraphStyle(
        "ilabel",
        parent=base,
        fontName=LABEL_FONT,
        fontSize=7.5,
        leading=11,
        textColor=ACCENT,
        keepWithNext=1,
        spaceAfter=1,
    )
    candidate_label = ParagraphStyle(
        "clabel", parent=interviewer_label, textColor=INK, keepWithNext=1
    )
    state = ParagraphStyle(
        "state", parent=muted, fontSize=7.5, leading=10.5, textColor=CAUTION, keepWithNext=1
    )
    interviewer_text = ParagraphStyle(
        "itext",
        parent=base,
        backColor=ACCENT_SOFT,
        borderPadding=(4, 7, 4, 7),
        leftIndent=7,
        rightIndent=7,
        spaceBefore=6,
    )
    candidate_text = ParagraphStyle(
        "ctext", parent=base, leftIndent=7, rightIndent=7, spaceBefore=2
    )
    undelivered_text = ParagraphStyle("utext", parent=interviewer_text, textColor=MUTED)
    cell = ParagraphStyle("cell", parent=base, fontSize=9, leading=13)

    def para(value: object, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)

    zone_name = record.get("timezone")
    started, ended = record.get("started_at"), record.get("ended_at")
    name = record.get("candidate_name")
    interview_title = record.get("interview_title") or "Interview"

    story: list[Any] = [
        Paragraph("CONVERSATION PDF", ParagraphStyle("kicker", parent=heading, fontSize=8)),
        Spacer(1, 4),
        para(interview_title, title),
        Spacer(1, 3),
        para(_when(started or record.get("created_at"), zone_name), subtitle),
        Spacer(1, 14),
    ]

    # --- Summary -------------------------------------------------------------------------
    fields = [("Candidate email", record.get("candidate_email") or "Not recorded")]
    if name:
        fields.insert(0, ("Candidate (self-reported)", name))
    fields += [
        ("Date", _when(started or record.get("created_at"), zone_name)),
        ("Duration", _duration(started, ended)),
        ("Status", completion_status(record)),
    ]
    coverage = record.get("coverage") if isinstance(record.get("coverage"), dict) else None
    if coverage and type(coverage.get("active_seconds")) is int:
        active = _seconds_text(coverage["active_seconds"])
        if active != _duration(started, ended):
            fields.append(("Active time (pauses excluded)", active))
    summary = Table(
        [[Paragraph(escape(k), key), para(v, cell)] for k, v in fields],
        colWidths=[160, CONTENT_WIDTH - 160],
        hAlign="LEFT",
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEABOVE", (0, 0), (-1, 0), 1.2, ACCENT),
            ]
        )
    )
    story += [summary, Spacer(1, 6)]
    if _is_partial(record):
        story += [para("This is a partial interview conversation.", muted)]
    story += [
        para(
            "Generated from saved records. This record contains no scores, hiring recommendation "
            "or assessment of the candidate.",
            faint,
        ),
        Spacer(1, 12),
    ]

    # --- Interview image -------------------------------------------------------------------
    story.append(_image_block(record, heading, muted, para))
    story.append(Spacer(1, 14))

    # --- Question coverage -----------------------------------------------------------------
    rows = question_rows(record)
    story.append(Paragraph("QUESTION COVERAGE", heading))
    story.append(Spacer(1, 5))
    if rows is None:
        story += [para("Question coverage was not recorded for this interview.", muted)]
    else:
        table = Table(
            [
                [para(f"Q{n}", cell), para(topic or "", cell), para(outcome, cell)]
                for n, topic, outcome in rows
            ],
            colWidths=[30, CONTENT_WIDTH - 30 - 190, 190],
            repeatRows=0,
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story += [
            table,
            Spacer(1, 3),
            para(
                "Recorded by the interview system. Where it was not recorded, no status is shown.",
                faint,
            ),
        ]
    story.append(Spacer(1, 14))

    # --- Conversation ----------------------------------------------------------------------
    header = [
        Paragraph("CONVERSATION", ParagraphStyle("ch", parent=heading, keepWithNext=1)),
        Spacer(1, 3),
        para(
            "Candidate wording is the stored machine transcription. Interviewer entries show what "
            "the server observed; that is not exact browser playback or the words heard.",
            muted,
        ),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=0.6, color=RULE),
        Spacer(1, 8),
    ]
    blocks: list[list[Any]] = []
    for entry in entries:
        candidate = entry["speaker"] == "candidate"
        stamp = _offset(entry.get("timestamp_utc"), started)
        label = _label_for(entry)
        head = ("CANDIDATE" if candidate else "INTERVIEWER") + f"  ·  {stamp}"
        if label:
            head += f"  ·  {label}"
        block: list[Any] = [
            Paragraph(escape(head), candidate_label if candidate else interviewer_label)
        ]
        text = entry.get("text") or "[No final text]"
        if candidate:
            block.append(para(text, candidate_text))
        else:
            output = OUTPUT_STATES.get(str(entry.get("status") or ""))
            if output:
                block.append(para(output, state))
            block.append(para(text, undelivered_text if output else interviewer_text))
        block.append(Spacer(1, 10))
        blocks.append(block)
    if not blocks:
        blocks = [[para("No conversation was saved for this interview.", muted)]]
    # Units of the conversation that must not be split from each other: the heading travels with
    # the first exchange, and the last two exchanges travel together so the final page is never
    # a lone entry. Everything else flows freely; there are no other forced page breaks.
    units = [list(block) for block in blocks]
    units[0] = header + units[0]
    if len(units) >= 2:
        units[-2:] = [units[-2] + units[-1]]
    for index, unit in enumerate(units):
        if index in (0, len(units) - 1):
            story.append(KeepTogether(unit))
        else:
            story.extend(unit)

    footer_note = f"RehearseAI  ·  Conversation PDF  ·  {str(record.get('id', ''))[:8]}"
    temp = path.with_name(f".{uuid.uuid4().hex}.tmp.pdf")
    try:

        def first_page(canvas: pdf_canvas.Canvas, doc: Any) -> None:
            canvas.saveState()
            canvas.setFillColor(ACCENT)
            canvas.rect(MARGIN_X, PAGE_HEIGHT - 43, 7, 7, stroke=0, fill=1)
            canvas.setFont(LABEL_FONT, 10)
            canvas.drawString(MARGIN_X + 12, PAGE_HEIGHT - 42, "RehearseAI")
            canvas.setFont(TEXT_FONT, 8)
            canvas.setFillColor(FAINT)
            canvas.drawRightString(PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 42, "Admin-only record")
            canvas.setStrokeColor(ACCENT)
            canvas.setLineWidth(1.2)
            canvas.line(MARGIN_X, PAGE_HEIGHT - 50, PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 50)
            canvas.restoreState()

        def later_pages(canvas: pdf_canvas.Canvas, doc: Any) -> None:
            canvas.saveState()
            canvas.setFillColor(ACCENT)
            canvas.rect(MARGIN_X, PAGE_HEIGHT - 33, 5, 5, stroke=0, fill=1)
            canvas.setFont(LABEL_FONT, 8)
            canvas.drawString(MARGIN_X + 9, PAGE_HEIGHT - 33, "RehearseAI")
            canvas.setFont(TEXT_FONT, 8)
            canvas.setFillColor(FAINT)
            canvas.drawRightString(
                PAGE_WIDTH - MARGIN_X,
                PAGE_HEIGHT - 33,
                f"{name or record.get('candidate_email', '')}",
            )
            canvas.setStrokeColor(RULE)
            canvas.setLineWidth(0.5)
            canvas.line(MARGIN_X, PAGE_HEIGHT - 40, PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 40)
            canvas.restoreState()

        document = SimpleDocTemplate(
            str(temp),
            pagesize=A4,
            rightMargin=MARGIN_X,
            leftMargin=MARGIN_X,
            topMargin=70,
            bottomMargin=56,
            title="Conversation PDF",
            author="RehearseAI",
        )
        document.build(
            story,
            onFirstPage=first_page,
            onLaterPages=later_pages,
            canvasmaker=lambda *a, **k: _NumberedCanvas(*a, footer_note=footer_note, **k),
        )
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _image_block(
    record: dict[str, Any], heading: ParagraphStyle, muted: ParagraphStyle, para: Any
) -> Any:
    """The one stored image, captioned by what it actually is. Never labelled as more than that."""
    captured = _when(record.get("snapshot_captured_at"), record.get("timezone"))
    snapshot = record.get("snapshot_path")
    kind = record.get("snapshot_kind")
    parts: list[Any] = []
    if snapshot and Path(snapshot).is_file():
        if kind == "room":
            parts.append(Paragraph("INTERVIEW ROOM IMAGE", heading))
            caption = (
                f"Saved by the candidate's browser at {captured}. A browser-rendered view of the "
                "interview room: candidate camera, RehearseAI panel, status and controls. It is "
                "not an operating-system screenshot, and shared-screen contents are not included. "
                "The image was submitted by the browser: it is not identity verification or "
                "tamper-proof evidence."
            )
        elif kind == "camera":
            parts.append(Paragraph("CANDIDATE CAMERA SNAPSHOT", heading))
            caption = (
                f"Saved by the candidate's browser at {captured}. Camera only: the interview-room "
                "image could not be captured, so this fallback was saved instead. It was "
                "submitted by the browser: not identity verification or tamper-proof evidence."
            )
        else:
            parts.append(Paragraph("CANDIDATE CAMERA SNAPSHOT", heading))
            caption = (
                f"Saved at {captured}. Camera only, from before interview-room images were "
                "introduced. It is not an image of the interview room. It was submitted by the "
                "browser: not identity verification or tamper-proof evidence."
            )
        with PillowImage.open(snapshot) as source:
            width, height = source.size
        # Keep the room's own proportions; fit a bounded box.
        display_width, display_height = fit_image(width, height)
        parts += [
            Spacer(1, 5),
            PdfImage(str(snapshot), width=display_width, height=display_height, hAlign="LEFT"),
            Spacer(1, 4),
            para(caption, muted),
        ]
        return KeepTogether(parts)
    parts += [
        Paragraph("INTERVIEW IMAGE", heading),
        Spacer(1, 4),
        para(
            "No interview image was saved for this interview. This does not affect the "
            "conversation record below.",
            muted,
        ),
    ]
    return KeepTogether(parts)


def fit_image(width: int, height: int) -> tuple[float, float]:
    """Display size for an image: its own proportions, inside a bounded box."""
    scale = min(CONTENT_WIDTH * 0.92 / width, 235 / height)
    return width * scale, height * scale
