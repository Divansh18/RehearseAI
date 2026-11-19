"""Keep private text and credentials out of operational logs.

Operational logs describe *what happened and how long it took*, never what anyone said or wrote.
Call sites pass identifiers, counts, durations and fixed codes. This module is the backstop: it
drops fields whose names suggest content or credentials, scrubs strings that look like secrets,
and bounds every value, so a mistake at a call site does not become a leak.
"""

import re
import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any

REDACTED = "[redacted]"
MAX_STRING = 200
MAX_ITEMS = 20

# A field with any of these words in its name is never written, whatever its value.
SENSITIVE_WORDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "csrf",
    "digest",
    "text",
    "transcript",
    "utterance_text",
    "body",
    "content",
    "prompt",
    "wording",
    "value",
    "resume",
    "job_description",
    "instructions",
    "email",
    "name",
    "address",
    "phone",
    "query",
    "payload",
    "answer",
    "response_text",
)
# Names that contain a sensitive word but are safe, stable identifiers or counts.
SAFE_NAMES = frozenset(
    {
        "event",
        "category",
        "logger",
        "build_id",
        "prompt_version",
        "assignment_id",
        "record_id",
        "request_id",
        "job_id",
        "job_kind",
        "utterance_id",
        "session_id",
        "error_type",
        "error_code",
        "text_length",
        "text_chars",
        "word_count",
        "name_confirmed",
        "processor",
        "exception_type",
        "queue_name",
        "tts_context_id",
    }
)

_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*\S+"),
    re.compile(r"[A-Za-z0-9_-]{32,}"),
)
_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def is_sensitive_name(name: str) -> bool:
    """Whether a field name says it holds content or a credential.

    Single words are matched as whole ``_``-separated tokens (so ``tts_context_id`` is not caught by
    ``text``); multi-word entries such as ``api_key`` are matched anywhere in the name.
    """
    lowered = name.lower()
    if lowered in SAFE_NAMES:
        return False
    tokens = set(lowered.split("_"))
    return any((word in lowered) if "_" in word else (word in tokens) for word in SENSITIVE_WORDS)


def scrub_string(value: str) -> str:
    if _UUID.match(value):
        return value
    for pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"


def clean(value: Any, *, depth: int = 0) -> Any:
    """A JSON-safe, bounded, scrubbed copy of a field value."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return clean(value.value, depth=depth)
    if depth >= 2:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_sensitive_name(str(key)) else clean(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [clean(item, depth=depth + 1) for item in list(value)[:MAX_ITEMS]]
    return scrub_string(type(value).__name__)


def clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: REDACTED if is_sensitive_name(key) else clean(value) for key, value in fields.items()
    }
