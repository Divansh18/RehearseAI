"""Private PDF rendering from persisted conversation rows only."""

import os
import uuid
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
)

FONT = Path(__file__).resolve().parent / "fonts" / "NotoSans-Regular.ttf"
INK = colors.HexColor("#181818")
MUTED = colors.HexColor("#666666")
RULE = colors.HexColor("#dedede")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _time(value: datetime | None) -> str:
    normalized = _utc(value)
    return normalized.strftime("%d %b %Y, %H:%M:%S UTC") if normalized else "Not recorded"


def _duration(started: datetime | None, ended: datetime | None) -> str:
    if not started or not ended:
        return "Not available"
    seconds = int((_utc(ended) - _utc(started)).total_seconds())
    if seconds < 0:
        return "Not available"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes:d}m {seconds:02d}s"


def _entry_time(value: datetime | None, started: datetime | None) -> str:
    stamp, origin = _utc(value), _utc(started)
    if stamp and origin and stamp >= origin:
        elapsed = int((stamp - origin).total_seconds())
        minutes, seconds = divmod(elapsed, 60)
        return f"+{minutes:02d}:{seconds:02d}"
    return _time(value)


def _entry_state(entry: dict[str, Any]) -> str:
    if entry["speaker"] == "candidate":
        return "Machine transcription"
    return {
        "prepared": "Prepared; no output observed",
        "output_started": "Server output started; completion unconfirmed",
        "output_completed": "Server output completed",
        "cancelled": "Cancelled before output",
        "interrupted": "Interrupted; exact words heard unknown",
        "failed": "Output failed",
    }.get(entry["status"], "Output status unknown")


def build_pdf(path: Path, record: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Write one id-derived PDF atomically; no browser text or LLM summary is used."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if "RehearseNoto" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("RehearseNoto", str(FONT)))

    body = ParagraphStyle(
        "body",
        fontName="RehearseNoto",
        fontSize=9.5,
        leading=14.5,
        textColor=INK,
        splitLongWords=1,
    )
    muted = ParagraphStyle(
        "muted",
        parent=body,
        fontSize=8,
        leading=12,
        textColor=MUTED,
    )
    label = ParagraphStyle(
        "label",
        parent=body,
        fontSize=8.5,
        leading=12,
    )
    title = ParagraphStyle(
        "title",
        parent=body,
        fontSize=19,
        leading=25,
        spaceAfter=5,
    )

    def paragraph(value: object, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)

    started = record.get("started_at")
    ended = record.get("ended_at")
    status = str(record["status"]).replace("_", " ").title()
    story = [
        paragraph("REHEARSEAI", label),
        Spacer(1, 3),
        paragraph("Interview Transcript", title),
        paragraph(f"Interview ID  {record['id']}", muted),
        Spacer(1, 14),
    ]

    fields = [
        ("Candidate account", record["candidate_email"]),
        ("Interview date", _time(record.get("created_at") or started)),
        ("Started", _time(started)),
        ("Ended", _time(ended)),
        ("Duration", _duration(started, ended)),
        ("Status", status),
    ]
    if record.get("candidate_name"):
        fields.insert(1, ("Candidate name (self-reported)", record["candidate_name"]))
    rows = [[paragraph(key, muted), paragraph(value, body)] for key, value in fields]
    metadata = Table(rows, colWidths=[145, A4[0] - 84 - 145], hAlign="LEFT")
    metadata.setStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
    story.extend([metadata, Spacer(1, 12)])
    if record["status"] in {"ended_early", "interrupted", "failed"}:
        story.append(paragraph("This is a partial interview conversation.", muted))
        story.append(Spacer(1, 5))
    story.extend(
        [
            paragraph(
                "Candidate text is machine-transcribed. Assistant output status reflects server "
                "events, not exact browser playback or words heard.",
                muted,
            ),
            Spacer(1, 13),
            HRFlowable(width="100%", thickness=0.6, color=RULE),
            Spacer(1, 10),
        ]
    )

    for entry in entries:
        story.append(CondPageBreak(65))
        speaker = "Candidate" if entry["speaker"] == "candidate" else "RehearseAI"
        stamp = _entry_time(entry.get("timestamp_utc"), started)
        story.append(paragraph(f"{speaker} / {stamp}", label))
        story.append(paragraph(_entry_state(entry), muted))
        story.append(Spacer(1, 3))
        story.append(paragraph(entry["text"] or "[No final text]"))
        story.append(Spacer(1, 8))
        story.append(HRFlowable(width="100%", thickness=0.4, color=RULE))
        story.append(Spacer(1, 8))

    temp = path.with_name(f".{uuid.uuid4().hex}.tmp.pdf")
    try:

        def footer(canvas, doc):
            canvas.saveState()
            canvas.setStrokeColor(RULE)
            canvas.line(42, 38, A4[0] - 42, 38)
            canvas.setFont("RehearseNoto", 8)
            canvas.setFillColor(MUTED)
            canvas.drawString(42, 25, "RehearseAI  /  Interview Transcript")
            canvas.drawRightString(A4[0] - 42, 25, f"Page {doc.page}")
            canvas.restoreState()

        document = SimpleDocTemplate(
            str(temp),
            pagesize=A4,
            rightMargin=42,
            leftMargin=42,
            topMargin=43,
            bottomMargin=56,
            title="Interview Transcript",
        )
        document.build(story, onFirstPage=footer, onLaterPages=footer)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
