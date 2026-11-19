"""Ordered local JSONL evidence for actual session attempts."""

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rehearse_ai.interview.operational_events import mirror


class SessionLog:
    def __init__(
        self,
        directory: Path,
        session_id: str,
        event_sink: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{session_id}.jsonl"
        self.session_id = session_id
        self.event_sink = event_sink
        self.persistence_failed = False
        self.storage_warning_sent = False
        self._sequence = 0
        self._clock_start_ns = time.monotonic_ns()
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(descriptor, "w", encoding="utf-8")

    def record(
        self,
        event: str,
        *,
        speaker: str | None = None,
        utterance_id: int | None = None,
        text: str | None = None,
        interrupted: bool | None = None,
        latency_ms: float | None = None,
        latency_basis: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        self._sequence += 1
        entry = {
            "session_id": self.session_id,
            "sequence": self._sequence,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "session_elapsed_ms": round((time.monotonic_ns() - self._clock_start_ns) / 1e6, 1),
            "clock_basis": "server monotonic clock; UTC timestamp is wall time",
            "event": event,
            "speaker": speaker,
            "utterance_id": utterance_id,
            "text": text,
            "interrupted": interrupted,
            "latency_ms": latency_ms,
            "latency_basis": latency_basis,
            **details,
        }
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()
        try:
            mirror(entry)  # the allowlisted, text-free subset only; never affects the evidence file
        except Exception:
            pass
        if (
            self.event_sink is not None
            and not self.event_sink(entry)
            and not self.storage_warning_sent
        ):
            self.persistence_failed = True
            self.storage_warning_sent = True
        return entry

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
