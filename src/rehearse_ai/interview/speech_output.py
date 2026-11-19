"""Which provider speech context belongs to which utterance, and what audio it produced.

The voice pipeline reports speech by *context id*; the interview reasons about *utterances*. This
class owns the mapping and the per-context audio counts, so a late chunk or stop from a cancelled
context can never be mistaken for the current utterance. It records what it observes as events
and decides nothing about the interview: delivery (what the candidate heard) stays with the
session. It receives only the few things it needs, not the session.
"""

import re
import time
from collections.abc import Callable
from typing import Any

from rehearse_ai.interview.controller import Utterance


class SpeechOutput:
    def __init__(
        self,
        *,
        pending: Callable[[], Utterance | None],
        closed: Callable[[], bool],
        paused: Callable[[], bool],
        record: Callable[..., Any],
        mark: Callable[..., None],
    ) -> None:
        self._pending = pending
        self._closed = closed
        self._paused = paused
        self._record = record
        self._mark = mark
        self._contexts: dict[str, int] = {}  # live contexts: context id -> utterance id
        self._history: dict[str, int] = {}  # every context ever bound, for late events
        self._started_at: dict[str, float] = {}
        self._audio_stats: dict[str, list[int]] = {}  # [chunks, bytes] reaching the output
        self._provider_stats: dict[str, list[float | int]] = {}  # [requested at, chunks, bytes]
        self._first_audio_ids: set[int] = set()
        self._completed_ids: set[int] = set()

    # -- what the session needs to ask ----------------------------------------------------------

    def utterance_for(self, context_id: str | None) -> int | None:
        return self._history.get(context_id or "")

    def has_first_audio(self, utterance_id: int) -> bool:
        return utterance_id in self._first_audio_ids

    def generation_completed(self, utterance_id: int) -> bool:
        return utterance_id in self._completed_ids

    def forget_utterance(self, utterance_id: int) -> None:
        """The utterance was cancelled: its live contexts no longer accept audio or a stop."""
        self._contexts = {
            context: identifier
            for context, identifier in self._contexts.items()
            if identifier != utterance_id
        }

    def context_summary(self, context_id: str | None) -> dict[str, Any]:
        """What a failing context had produced, for the pipeline error record."""
        stats = self._audio_stats.get(context_id, [0, 0]) if context_id else [0, 0]
        started = self._started_at.get(context_id) if context_id else None
        return {
            "tts_audio_chunks": stats[0],
            "tts_audio_bytes": stats[1],
            "tts_context_elapsed_ms": (
                round((time.perf_counter() - started) * 1000, 1) if started is not None else None
            ),
        }

    # -- transport-side observations ------------------------------------------------------------

    def context_assigned(self, utterance_id: int, context_id: str) -> None:
        pending = self._pending()
        if not self._closed() and pending is not None and pending.id == utterance_id:
            self._history[context_id] = utterance_id

    def started(self, utterance_id: int, context_id: str | None) -> None:
        pending = self._pending()
        if pending is None or pending.id != utterance_id or context_id is None:
            return
        assigned = self._history.get(context_id)
        if assigned is not None and assigned != utterance_id:
            return
        self._contexts[context_id] = utterance_id
        self._history[context_id] = utterance_id
        self._started_at[context_id] = time.perf_counter()
        self._audio_stats[context_id] = [0, 0]
        self._record(
            "tts_started", speaker=None, utterance_id=utterance_id, tts_context_id=context_id
        )

    def audio_chunk(self, context_id: str | None, byte_count: int) -> bool:
        """True when the chunk belongs to the utterance being spoken and may be forwarded."""
        if self._closed() or self._paused():
            return False
        pending = self._pending()
        if (
            not context_id
            or context_id not in self._contexts
            or pending is None
            or pending.id != self._contexts[context_id]
        ):
            self._record(
                "stale_tts_audio_discarded",
                speaker=None,
                tts_context_id=context_id,
                audio_bytes=byte_count,
            )
            return False
        stats = self._audio_stats[context_id]
        stats[0] += 1
        stats[1] += byte_count
        self.first_audio(context_id)
        return True

    def first_audio(self, context_id: str | None) -> None:
        utterance_id = self._contexts.get(context_id or "")
        pending = self._pending()
        if (
            utterance_id is not None
            and pending is not None
            and pending.id == utterance_id
            and utterance_id not in self._first_audio_ids
        ):
            self._first_audio_ids.add(utterance_id)
            self._mark("first_audio", first=True)
            self._record(
                "tts_first_audio_generated",
                speaker=None,
                utterance_id=utterance_id,
                tts_context_id=context_id,
                browser_playback_onset_ms=None,
            )

    def finished(self, context_id: str | None) -> bool:
        """True when this was the live context's stop; a stale stop is recorded and refused."""
        utterance_id = self._contexts.pop(context_id or "", None)
        if utterance_id is None:
            if not self._closed():
                self._record(
                    "stale_tts_stop_discarded",
                    speaker=None,
                    tts_context_id=context_id,
                    utterance_id=self._history.get(context_id or ""),
                )
            return False
        self._completed_ids.add(utterance_id)
        stats = self._audio_stats.get(context_id or "", [0, 0])
        self._record(
            "tts_generation_finished",
            speaker=None,
            utterance_id=utterance_id,
            tts_context_id=context_id,
            audio_chunks=stats[0],
            audio_bytes=stats[1],
        )
        return True

    # -- provider-side observations (what the TTS service itself reported) ----------------------

    def provider_requested(self, context_id: str, text_length: int) -> None:
        if self._closed():
            return
        self._provider_stats[context_id] = [time.perf_counter(), 0, 0]
        self._record(
            "provider_tts_requested",
            speaker=None,
            tts_context_id=context_id,
            utterance_id=self._history.get(context_id),
            text_length=text_length,
        )

    def provider_audio_chunk(self, context_id: str, byte_count: int) -> None:
        if self._closed() or context_id not in self._provider_stats:
            return
        stats = self._provider_stats[context_id]
        stats[1] += 1
        stats[2] += byte_count
        if stats[1] == 1:
            self._record(
                "provider_first_audio",
                speaker=None,
                tts_context_id=context_id,
                utterance_id=self._history.get(context_id),
                first_audio_ms=round((time.perf_counter() - stats[0]) * 1000, 1),
                audio_bytes=byte_count,
            )

    def provider_error(self, context_id: str, error: str, status: int | None) -> None:
        if self._closed():
            return
        match = re.search(r"status:\s*(\d{3})", error)
        self._record(
            "provider_tts_error",
            speaker=None,
            tts_context_id=context_id,
            utterance_id=self._history.get(context_id),
            error_type=error if error.isidentifier() else "ErrorFrame",
            provider_status=status or (int(match.group(1)) if match else None),
        )

    def provider_cancelled(self, context_id: str) -> None:
        if not self._closed():
            self._record(
                "provider_tts_cancelled",
                speaker=None,
                tts_context_id=context_id,
                utterance_id=self._history.get(context_id),
            )

    def provider_finished(self, context_id: str) -> None:
        if self._closed() or context_id not in self._provider_stats:
            return
        started, chunks, byte_count = self._provider_stats.pop(context_id)
        self._record(
            "provider_tts_finished",
            speaker=None,
            tts_context_id=context_id,
            utterance_id=self._history.get(context_id),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            audio_chunks=chunks,
            audio_bytes=byte_count,
        )
