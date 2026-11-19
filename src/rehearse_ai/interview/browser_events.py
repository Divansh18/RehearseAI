"""Candidate-browser events: an explicit allowlist with minimal payload schemas.

The server log (``SessionLog``) is internal evidence and may contain raw model
output, decision text, memory facts and error details. Nothing from it is sent to
the candidate's browser except what this module reconstructs field by field.
Unknown events are dropped, and unknown fields are never copied.

This does not stop the model from *speaking* something it should not; spoken output
is a separate risk that is only reduced by the bounded context in ``session.py``.
"""

from typing import Any

# Events that carry no data beyond their name.
_SIGNAL_ONLY = frozenset(
    {
        "media_paused",
        "media_resumed",
        "candidate_started",
        "candidate_turn_completed",
        "awaiting_continuation",
        "interview_complete",
        "interview_ended_early",
        "clarification_limit",
        "persistence_error",
        # Historical practice-only guidance signals the browser still understands.
        "opening_waiting_for_readiness",
        "no_project_waiting",
        "no_project_repeat_limit",
        "project_example_required",
    }
)
_UTTERANCE_ONLY = frozenset(
    {"agent_output_started", "agent_interrupted", "agent_delivery_reported"}
)
_UTTERANCE_KINDS = frozenset(
    {
        "greeting",
        "opening_ack",
        "question",
        "follow_up",
        "nudge",
        "clarification",
        "repeat",
        "information",
        "detour_resume",
        "closing",
        "feedback",
        "early_feedback",
    }
)
_RESPONSE_STAGES = frozenset(
    {
        "opening_inference",
        "opening_intent",
        "pipeline",
        "speech_queue",
        "assigned_decision",
        "closing_inference",
    }
)
_RESPONSE_PROCESSORS = frozenset({"OpenAITTSService", "CartesiaTTSService"})
_MAX_SPOKEN_TEXT = 600


def _identifier(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 1_000_000 else None


def candidate_event(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Return the browser-safe form of a log entry, or ``None`` to send nothing."""
    name = entry.get("event")
    if not isinstance(name, str):
        return None
    if name in _SIGNAL_ONLY:
        return {"event": name}
    if name == "opening_repeat_limit":
        phase = entry.get("opening_phase")
        return {
            "event": name,
            "opening_phase": phase if phase in {"greeting", "readiness"} else None,
        }
    if name == "session_started":
        session_id = entry.get("session_id")
        return {"event": name, "session_id": session_id if isinstance(session_id, str) else None}
    if name in _UTTERANCE_ONLY:
        utterance_id = _identifier(entry.get("utterance_id"))
        return {"event": name, "utterance_id": utterance_id} if utterance_id else None
    if name == "agent_prepared":
        utterance_id = _identifier(entry.get("utterance_id"))
        text = entry.get("text")
        kind = entry.get("utterance_kind")
        if not utterance_id or not isinstance(text, str):
            return None
        return {
            "event": name,
            "utterance_id": utterance_id,
            "text": text[:_MAX_SPOKEN_TEXT],
            "utterance_kind": kind if kind in _UTTERANCE_KINDS else "information",
            "question_number": _identifier(entry.get("question_number")),
        }
    if name == "response_error":
        stage = entry.get("failure_stage")
        processor = entry.get("processor")
        return {
            "event": name,
            "failure_stage": stage if stage in _RESPONSE_STAGES else "other",
            "processor": processor if processor in _RESPONSE_PROCESSORS else None,
            "error_code": "tts_context_no_audio"
            if entry.get("error_code") == "tts_context_no_audio"
            else None,
        }
    return None
