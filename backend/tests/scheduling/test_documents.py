"""Bounded, explained extraction of untrusted résumé and job-description uploads."""

import asyncio
import io
import zipfile

import pytest
from support import JOB_TEXT, RESUME_TEXT, make_docx, make_pdf

from rehearse_ai.scheduling import documents
from rehearse_ai.scheduling.documents import (
    DocumentError,
    detect_kind,
    extract_document,
    extract_file,
    store_document,
)


def extract(tmp_path, data: bytes, name: str, role: str = "resume", **kwargs):
    kind = detect_kind(name, data)
    path = tmp_path / f"upload.{kind}"
    path.write_bytes(data)
    return asyncio.run(extract_document(path, kind, role, **kwargs))


def rejected(tmp_path, data: bytes, name: str, role: str = "resume") -> DocumentError:
    with pytest.raises(DocumentError) as raised:
        extract(tmp_path, data, name, role)
    return raised.value


def test_pdf_and_docx_text_are_extracted_in_an_isolated_process(tmp_path):
    pdf = extract(tmp_path, make_pdf(RESUME_TEXT), "resume.pdf")
    assert "PRIVATE-EVIDENCE-SPAN" in pdf.text and pdf.pages == 1
    docx = extract(tmp_path, make_docx(RESUME_TEXT), "resume.docx")
    assert "PRIVATE-EVIDENCE-SPAN" in docx.text
    job = extract(tmp_path, make_docx(JOB_TEXT), "role.docx", "job")
    assert "document processing platform" in job.text


def test_encrypted_pdf_is_rejected_with_an_explanation(tmp_path):
    error = rejected(tmp_path, make_pdf(RESUME_TEXT, encrypt=True), "resume.pdf")
    assert error.code == "encrypted" and "password" in error.message


def test_scanned_image_only_pdf_is_rejected_not_silently_empty(tmp_path):
    error = rejected(tmp_path, make_pdf("", draw_text=False), "resume.pdf")
    assert error.code == "no_text"
    assert "OCR" in error.message and "not supported" in error.message


def test_garbled_text_is_rejected(tmp_path):
    error = rejected(tmp_path, make_pdf("#@%&*~^ " * 60), "resume.pdf")
    assert error.code == "unreadable"


def test_damaged_pdf_is_rejected_as_unreadable(tmp_path):
    error = rejected(tmp_path, b"%PDF-1.4\n" + b"\x00garbage" * 50, "resume.pdf")
    assert error.code == "unreadable"


def test_too_many_pages_and_too_long_text_are_rejected_not_truncated(tmp_path):
    many = rejected(tmp_path, make_pdf(RESUME_TEXT, pages=documents.MAX_PDF_PAGES + 1), "r.pdf")
    assert many.code == "too_many_pages"
    long_text = "Built and operated systems for customers. " * 900
    error = rejected(tmp_path, make_docx(long_text), "r.docx")
    assert error.code == "too_long" and "rather than risk" in error.message


def test_type_checks_use_content_as_well_as_extension():
    for name, data in (
        ("resume.txt", b"plain text" * 40),
        ("resume.doc", b"legacy" * 40),
        ("resume.pdf", b"PK\x03\x04" + b"x" * 100),  # a zip named .pdf
        ("resume.docx", b"%PDF-1.7" + b"x" * 100),  # a PDF named .docx
    ):
        with pytest.raises(DocumentError) as raised:
            detect_kind(name, data)
        assert raised.value.code == "unsupported_type"
    with pytest.raises(DocumentError) as raised:
        detect_kind("resume.docx", bytes.fromhex("D0CF11E0A1B11AE1") + b"x" * 64)
    assert raised.value.code == "encrypted_or_legacy"
    with pytest.raises(DocumentError) as raised:
        detect_kind("resume.pdf", b"")
    assert raised.value.code == "empty"
    with pytest.raises(DocumentError) as raised:
        detect_kind("resume.pdf", b"%PDF-" + b"0" * documents.MAX_UPLOAD_BYTES)
    assert raised.value.code == "too_large"


def test_docx_bomb_dtd_and_missing_body_are_rejected(tmp_path):
    bomb = make_docx(RESUME_TEXT, extra_files={"word/padding.bin": b"\x00" * (9 * 1024 * 1024)})
    assert rejected(tmp_path, bomb, "r.docx").code == "too_large"
    dtd = make_docx(
        "",
        xml='<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "boom">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>",
    )
    assert rejected(tmp_path, dtd, "r.docx").code == "unreadable"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "not a docx")
    assert rejected(tmp_path, buffer.getvalue(), "r.docx").code == "unsupported_type"


def test_empty_text_document_and_short_job_description_are_rejected(tmp_path):
    assert rejected(tmp_path, make_docx("Hi"), "r.docx").code == "no_text"
    assert rejected(tmp_path, make_docx("Short role."), "j.docx", "job").code == "no_text"


def test_extraction_is_killed_when_it_exceeds_the_time_limit(tmp_path):
    with pytest.raises(DocumentError) as raised:
        extract(tmp_path, make_pdf(RESUME_TEXT), "resume.pdf", timeout=0.001)
    assert raised.value.code == "timeout"


def test_uploads_are_stored_privately_and_never_logged(tmp_path, caplog):
    caplog.set_level("DEBUG")
    name, digest = store_document(tmp_path / "private", make_pdf(RESUME_TEXT), "pdf")
    path = tmp_path / "private" / name
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert len(digest) == 64
    extract_file(path, "pdf", "resume")
    assert "PRIVATE-EVIDENCE-SPAN" not in caplog.text
