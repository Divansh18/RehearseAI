"""Private PDF rendering from persisted conversation rows only."""

import os
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import CondPageBreak, HRFlowable, Paragraph, SimpleDocTemplate, Spacer

FONT = Path(__file__).resolve().parent / "fonts" / "NotoSans-Regular.ttf"


def _date(value: datetime | None) -> str:
    return value.astimezone().strftime("%d %b %Y, %H:%M:%S %Z") if value else "Not recorded"


def build_pdf(path: Path, record: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Write one id-derived PDF atomically; no browser text or LLM summary is used."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if "RehearseNoto" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("RehearseNoto", str(FONT)))
    body = ParagraphStyle(
        "body",
        fontName="RehearseNoto",
        fontSize=9.6,
        leading=15,
        textColor=colors.HexColor("#171717"),
        spaceAfter=8,
        alignment=TA_LEFT,
        splitLongWords=1,
    )
    small = ParagraphStyle(
        "small",
        parent=body,
        fontSize=8,
        leading=12,
        textColor=colors.HexColor("#555555"),
        spaceAfter=5,
    )
    heading = ParagraphStyle(
        "heading",
        parent=body,
        fontSize=18,
        leading=24,
        spaceAfter=12,
    )
    story = [Paragraph("RehearseAI interview transcript", heading)]
    status = record["status"].replace("_", " ").title()
    fields = (
        ("Interview", record["id"]),
        ("Candidate", record["candidate_email"]),
        ("Status", status),
        ("Reason", record.get("termination_reason") or "Not recorded"),
        ("Started", _date(record.get("started_at"))),
        ("Ended", _date(record.get("ended_at"))),
    )
    for label, value in fields:
        story.append(Paragraph(f"{escape(label)}: {escape(str(value))}", small))
    story.append(Spacer(1, 9))
    story.append(
        Paragraph(
            "Candidate speech is machine transcription. Assistant completion means server output "
            "completed; it does not confirm browser playback or exact words heard.",
            small,
        )
    )
    if status in {"Interrupted", "Failed", "Ended Early"}:
        story.append(Paragraph("This is a partial interview conversation.", small))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#dddddd")))
    story.append(Spacer(1, 12))
    for entry in entries:
        story.append(CondPageBreak(70))
        speaker = "You" if entry["speaker"] == "candidate" else "RehearseAI"
        state = entry["status"].replace("_", " ")
        stamp = _date(entry["timestamp_utc"])
        story.append(Paragraph(f"{speaker} - {stamp} ({state})", small))
        # Paragraph accepts markup, so escape untrusted transcript text first.
        safe_text = escape(entry["text"]).replace("\n", "<br/>")
        story.append(Paragraph(safe_text or "[No final text]", body))
        story.append(Spacer(1, 7))

    temp = path.with_name(f".{uuid.uuid4().hex}.tmp.pdf")
    try:

        def footer(canvas, doc):
            canvas.saveState()
            canvas.setFont("RehearseNoto", 8)
            canvas.setFillColor(colors.HexColor("#777777"))
            canvas.drawString(42, 26, "RehearseAI - private interview transcript")
            canvas.drawRightString(A4[0] - 42, 26, str(doc.page))
            canvas.restoreState()

        document = SimpleDocTemplate(
            str(temp),
            pagesize=A4,
            rightMargin=42,
            leftMargin=42,
            topMargin=45,
            bottomMargin=45,
            title="RehearseAI interview transcript",
        )
        document.build(story, onFirstPage=footer, onLaterPages=footer)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
