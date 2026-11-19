"""Bounded real-model check of automatic plan preparation using INVENTED documents only.

Runs the real path: build a PDF/DOCX from invented text, extract it with the production
extractor, call the configured OpenAI model through ``generate_plan`` and report grounding,
duplication, duration fit and a revision. It prints no keys. Each scenario makes at most a few
model calls. Results are for human review; they are not a pass/fail test.

    uv run --project backend python evaluation/plan_preparation_check.py \
        [--out evaluation/plan_checks]
"""

import argparse
import asyncio
import io
import json
import re
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from rehearse_ai.config import Settings
from rehearse_ai.plans.preparation import (
    OpenAIPlanGenerator,
    PlanInputs,
    _squash,
    _tokens,
    generate_plan,
    question_range,
)
from rehearse_ai.scheduling.documents import extract_document

DOCS = Path(__file__).parent / "plan_docs"
SCENARIOS = [
    ("backend", "Backend engineer", 30, "pdf", "docx"),
    ("ai", "AI engineer", 45, "docx", "pdf"),
    ("ops", "Non-software: logistics operations manager", 20, "pdf", "docx"),
]
SENSITIVE = re.compile(
    r"\b(age|old are you|married|marital|religio\w*|pregnan\w*|disabilit\w*|ethnic\w*|"
    r"nationalit\w*|children|kids|sexual|political)\b",
    re.I,
)


def make_pdf(text: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    lines = [wrapped for raw in text.splitlines() for wrapped in (textwrap.wrap(raw, 90) or [""])]
    for start in range(0, len(lines), 50):
        text_object = pdf.beginText(50, 750)
        for line in lines[start : start + 50]:
            text_object.textLine(line)
        pdf.drawText(text_object)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def make_docx(text: str) -> bytes:
    import zipfile
    from xml.sax.saxutils import escape

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in text.splitlines())
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


async def to_text(workdir: Path, name: str, kind: str, role: str) -> str:
    source = (DOCS / name).read_text(encoding="utf-8")
    data = make_pdf(source) if kind == "pdf" else make_docx(source)
    path = workdir / f"{name}.{kind}"
    path.write_bytes(data)
    return (await extract_document(path, kind, role)).text


def assess(plan, resume: str, minutes: int) -> dict:
    questions = plan.questions
    bags = [_tokens(q.question) for q in questions]
    similarity = max(
        (len(a & b) / len(a | b) for i, a in enumerate(bags) for b in bags[i + 1 :] if a | b),
        default=0.0,
    )
    seconds = sum(q.budget_seconds or 0 for q in questions)
    low, high = question_range(minutes)
    return {
        "question_count": len(questions),
        "allowed_range": [low, high],
        "budget_total_seconds": seconds,
        "budget_share_of_target": round(seconds / (minutes * 60), 2),
        "resume_grounded": sum(q.source in {"resume", "both"} for q in questions),
        "evidence_verified": all(
            _squash(q.evidence_quote) in _squash(resume)
            for q in questions
            if q.source in {"resume", "both"}
        ),
        "max_pairwise_similarity": round(similarity, 2),
        "must_questions": sum(q.priority == "must" for q in questions),
        "sensitive_terms_in_questions": sorted(
            {m.group(0).lower() for q in questions for m in SENSITIVE.finditer(q.question)}
        ),
    }


async def run(out: Path) -> None:
    settings = Settings.load()
    generator = OpenAIPlanGenerator(settings.openai_api_key, settings.llm_model)
    out.mkdir(parents=True, exist_ok=True)
    report = {"model": settings.llm_model, "scenarios": {}}
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for key, label, minutes, resume_kind, job_kind in SCENARIOS:
            resume = await to_text(workdir, f"{key}_resume.txt", resume_kind, "resume")
            job = await to_text(workdir, f"{key}_job.txt", job_kind, "job")
            instructions = (DOCS / f"{key}_instructions.txt").read_text(encoding="utf-8").strip()
            inputs = PlanInputs(resume, job, instructions, minutes)
            started = time.monotonic()
            try:
                plan = await generate_plan(generator, "plan_prepare", inputs)
            except Exception as exc:
                report["scenarios"][key] = {"label": label, "failed": type(exc).__name__}
                print(f"{key}: FAILED {type(exc).__name__}")
                continue
            elapsed = round(time.monotonic() - started, 1)
            entry = {
                "label": label,
                "target_minutes": minutes,
                "seconds_to_generate": elapsed,
                "assessment": assess(plan, resume, minutes),
                "plan": plan.model_dump(mode="json"),
            }
            if key == "backend":
                request = (
                    "Replace the last question with one about handling disagreement "
                    "with a colleague."
                )
                started = time.monotonic()
                revised = await generate_plan(
                    generator,
                    "plan_revise",
                    inputs,
                    current_plan=plan.model_dump(mode="json"),
                    revision_request=request,
                )
                entry["revision"] = {
                    "request": request,
                    "seconds": round(time.monotonic() - started, 1),
                    "assessment": assess(revised, resume, minutes),
                    "last_question_before": plan.questions[-1].question,
                    "last_question_after": revised.questions[-1].question,
                    "unchanged_questions": sum(
                        a.question == b.question
                        for a, b in zip(plan.questions[:-1], revised.questions[:-1], strict=False)
                    ),
                }
            report["scenarios"][key] = entry
            print(f"{key}: ok in {elapsed}s -> {json.dumps(entry['assessment'])}")
    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("evaluation/plan_checks"))
    arguments = parser.parse_args()
    sys.exit(asyncio.run(run(arguments.out)))
