"""One JSON object per line, with the same envelope for every operational event."""

import json
import logging
import traceback
from datetime import UTC, datetime
from typing import Any

from rehearse_ai.operational_log.redaction import clean_fields, scrub_string

SERVICE = "rehearse-ai"
# Keys of the envelope; an event field with one of these names cannot overwrite it. (The correlation
# ids are not reserved: a call site may state its own ``record_id`` or ``job_id`` explicitly.)
RESERVED = frozenset({"ts", "level", "event", "category", "logger", "service", "build_id", "error"})
MAX_FRAMES = 12


def safe_error(exc_info: tuple[Any, Any, Any]) -> dict[str, Any]:
    """The exception class and where it was raised: never its message.

    Exception messages routinely contain the data that caused the failure (a validation error echoes
    the submitted value, a database error echoes a row), so they are not logged.
    """
    exc_type, _exc, tb = exc_info
    frames = [
        f"{frame.filename.rsplit('/', 1)[-1]}:{frame.name}:{frame.lineno}"
        for frame in traceback.extract_tb(tb)[-MAX_FRAMES:]
    ]
    return {"type": getattr(exc_type, "__name__", "Exception"), "frames": frames}


class JsonFormatter(logging.Formatter):
    def __init__(self, build_id: str | None = None) -> None:
        super().__init__()
        self.build_id = build_id

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format(record)
        except Exception:  # a formatting bug must never lose the fact that something happened
            return json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "level": "ERROR",
                    "event": "log.format_failed",
                    "category": "log",
                    "service": SERVICE,
                }
            )

    def _format(self, record: logging.LogRecord) -> str:
        name = getattr(record, "event", None)
        fields = getattr(record, "fields", None)
        if name is None:  # a plain logging call: keep it, scrubbed, rather than lose it
            name = "log.unstructured"
            fields = {"message": scrub_string(record.getMessage())}
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": name,
            "category": name.split(".", 1)[0],
            "logger": record.name,
            "service": SERVICE,
        }
        if self.build_id:
            entry["build_id"] = self.build_id
        entry.update(getattr(record, "correlation", {}))
        for key, value in clean_fields(fields or {}).items():
            if key not in RESERVED:
                entry[key] = value
        error = getattr(record, "error", None)
        if error is None and record.exc_info:
            error = safe_error(record.exc_info)
        if error:
            entry["error"] = error
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)
