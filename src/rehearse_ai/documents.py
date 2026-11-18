"""Bounded text extraction from untrusted résumé and job-description uploads.

Only PDF and DOCX are accepted. Extraction runs in a separate, isolated Python process
with a wall-clock timeout so a hostile or pathological file cannot hang or crash the
server. Encrypted, empty, scanned (image-only) and garbled documents are rejected with a
specific explanation; incomplete text is never used silently. Document contents are
never logged.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_DOCX_UNCOMPRESSED = 8 * 1024 * 1024
EXTRACT_TIMEOUT_SECS = 20
MIN_CHARS = {"resume": 200, "job": 150}
MAX_CHARS = {"resume": 30_000, "job": 20_000}
ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DocumentError(Exception):
    """A document problem explained in words that are safe to show the administrator."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    pages: int


def detect_kind(filename: str, data: bytes) -> str:
    """Trust file contents, not the name, and require both to agree."""
    extension = Path(filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise DocumentError("unsupported_type", "Upload a PDF or DOCX file.")
    if not data:
        raise DocumentError("empty", "The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError(
            "too_large", f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    if data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        raise DocumentError(
            "encrypted_or_legacy",
            "This looks like a legacy Word file or a password-protected document. "
            "Save an unprotected .docx or a text-based PDF and upload that.",
        )
    kind = ALLOWED_EXTENSIONS[extension]
    if kind == "pdf" and not data.startswith(b"%PDF-"):
        raise DocumentError("unsupported_type", "The file is not a valid PDF.")
    if kind == "docx" and not data.startswith(b"PK\x03\x04"):
        raise DocumentError("unsupported_type", "The file is not a valid DOCX.")
    return kind


def clean_text(text: str) -> str:
    text = _CONTROL.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def validate_text(text: str, role: str, *, pages: int = 0) -> str:
    """Reject text that is empty, image-only, garbled or too long to use safely."""
    text = clean_text(text)
    label = "résumé" if role == "resume" else "job description"
    if len(text) < MIN_CHARS[role]:
        raise DocumentError(
            "no_text",
            f"No usable text could be read from this {label}. Scanned or image-only documents "
            "need OCR, which is not supported. Upload a text-based PDF or DOCX"
            + (" or paste the text." if role == "job" else "."),
        )
    visible = [char for char in text if not char.isspace()]
    letters = sum(char.isalpha() for char in visible)
    if not visible or letters / len(visible) < 0.5:
        raise DocumentError(
            "unreadable",
            f"The text in this {label} looks garbled or is mostly symbols, so it was not used.",
        )
    if len(text) > MAX_CHARS[role]:
        raise DocumentError(
            "too_long",
            f"This {label} is longer than {MAX_CHARS[role]:,} characters. "
            "Shorten it rather than risk silently using only part of it.",
        )
    return text


def extract_pdf(path: Path, role: str) -> ExtractedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise DocumentError(
                "encrypted",
                "This PDF is password-protected. Upload an unprotected copy.",
            )
        pages = len(reader.pages)
        if pages > MAX_PDF_PAGES:
            raise DocumentError("too_many_pages", f"This PDF has more than {MAX_PDF_PAGES} pages.")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure means unreadable input
        raise DocumentError("unreadable", "This PDF could not be read. It may be damaged.") from exc
    return ExtractedDocument(validate_text(text, role, pages=pages), pages)


def _paragraph_text(element: ET.Element) -> str:
    parts: list[str] = []
    for child in element:
        if child.tag == f"{_W}p":
            continue  # nested paragraphs (text boxes) are visited on their own
        if child.tag == f"{_W}t":
            parts.append(child.text or "")
        elif child.tag == f"{_W}tab":
            parts.append("\t")
        elif child.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
        else:
            parts.append(_paragraph_text(child))
    return "".join(parts)


def extract_docx(path: Path, role: str) -> ExtractedDocument:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if any(info.flag_bits & 0x1 for info in infos):
                raise DocumentError(
                    "encrypted", "This DOCX is password-protected. Upload an unprotected copy."
                )
            if sum(info.file_size for info in infos) > MAX_DOCX_UNCOMPRESSED:
                raise DocumentError("too_large", "This DOCX expands to an unsafe size.")
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise DocumentError("unsupported_type", "The file is not a valid DOCX.") from exc
            data = archive.read(info)
        head = data[:4096].lower()
        if b"<!doctype" in head or b"<!entity" in data[:65536].lower():
            raise DocumentError("unreadable", "This DOCX contains unsupported markup.")
        root = ET.fromstring(data)
        text = "\n".join(_paragraph_text(p) for p in root.iter(f"{_W}p"))
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(
            "unreadable", "This DOCX could not be read. It may be damaged."
        ) from exc
    return ExtractedDocument(validate_text(text, role), 0)


def extract_file(path: Path, kind: str, role: str) -> ExtractedDocument:
    return extract_pdf(path, role) if kind == "pdf" else extract_docx(path, role)


async def extract_document(
    path: Path, kind: str, role: str, *, timeout: float = EXTRACT_TIMEOUT_SECS
) -> ExtractedDocument:
    """Extract in an isolated child process; kill it if it exceeds the timeout."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "rehearse_ai.documents",
        str(path),
        kind,
        role,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise DocumentError(
            "timeout", "Reading this document took too long, so it was not used."
        ) from None
    try:
        result = json.loads(out.decode("utf-8"))
        if "error" in result:
            raise DocumentError(str(result["error"]), str(result["message"]))
        return ExtractedDocument(str(result["text"]), int(result["pages"]))
    except (ValueError, KeyError, UnicodeDecodeError):
        raise DocumentError("unreadable", "This document could not be read.") from None


def store_document(directory: Path, data: bytes, kind: str) -> tuple[str, str]:
    """Write an upload privately (0600, inside a 0700 directory) and return name, sha256."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"{uuid.uuid4().hex}.{kind}"
    descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return name, hashlib.sha256(data).hexdigest()


def _child_main(argv: list[str]) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (EXTRACT_TIMEOUT_SECS, EXTRACT_TIMEOUT_SECS + 5))
    path, kind, role = Path(argv[0]), argv[1], argv[2]
    try:
        document = extract_file(path, kind, role)
        output = {"text": document.text, "pages": document.pages}
    except DocumentError as exc:
        output = {"error": exc.code, "message": exc.message}
    except Exception:  # noqa: BLE001
        output = {"error": "unreadable", "message": "This document could not be read."}
    sys.stdout.write(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    _child_main(sys.argv[1:])
