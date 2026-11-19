"""Server-side stage timings of one response, from the end of the candidate's speech.

Every value is a difference between two readings of the server's monotonic clock. "Output started"
is Pipecat's transport output, not browser playback; "vad_stop" is a Silero estimate of the end of
speech, not the last audible phoneme. Stages that were not observed are ``None``. Nothing here
holds text: only names and times.
"""

from collections.abc import Callable
from typing import Any

# Marks that belong to one response and are cleared once it has been reported.
RESPONSE_MARKS = ("turn_complete", "decision_start", "decision_done", "prepared", "first_audio")


class ResponseTimeline:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._marks: dict[str, float] = {}

    def mark(self, name: str, *, first: bool = False) -> None:
        """Note that a stage happened now. With ``first`` an earlier reading is kept."""
        if not first or name not in self._marks:
            self._marks[name] = self._now()

    def clear(self) -> None:
        self._marks.clear()

    def gap_ms(self, later: str, earlier: str) -> int | None:
        """Milliseconds from ``earlier`` to ``later``; negative when they happened the other way."""
        if later in self._marks and earlier in self._marks:
            return round((self._marks[later] - self._marks[earlier]) * 1000)
        return None

    def latency_details(self) -> dict[str, Any] | None:
        """The breakdown to log for the response that just started, or None without a turn end."""
        marks = self._marks
        if "turn_complete" not in marks:
            return None

        gap = self.gap_ms

        details = {
            "stt_final_after_speech_end_ms": gap("last_stt", "vad_stop"),
            "speech_end_to_turn_complete_ms": gap("turn_complete", "vad_stop"),
            "last_stt_to_turn_complete_ms": gap("turn_complete", "last_stt"),
            "turn_complete_to_model_start_ms": gap("decision_start", "turn_complete"),
            "model_ms": gap("decision_done", "decision_start"),
            "model_done_to_prepared_ms": gap("prepared", "decision_done"),
            "prepared_to_tts_first_audio_ms": gap("first_audio", "prepared"),
            "tts_first_audio_to_output_started_ms": gap("output_started", "first_audio"),
            "turn_complete_to_output_started_ms": gap("output_started", "turn_complete"),
            "speech_end_to_output_started_ms": gap("output_started", "vad_stop"),
            "basis": (
                "server monotonic clock; output_started is server transport output, not browser "
                "playback; vad_stop is an estimate of the end of speech"
            ),
        }
        for name in RESPONSE_MARKS:
            marks.pop(name, None)
        return details
