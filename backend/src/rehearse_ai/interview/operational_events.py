"""Which saved-conversation events are also written to the operational log, and with what.

The per-attempt file (``session_log``) is the protected diagnostic record: it holds what was said.
The operational log must never hold that. So nothing is mirrored by default: an event appears here
only if it is explicitly listed, and then only the listed fields are copied (durations, counts,
ids and fixed codes). Free-text fields (``text``, ``candidate_text``, memory ``value`` ...) are not
in any list, and an event missing from this table is simply not mirrored.
"""

import logging
from typing import Any

from rehearse_ai.operational_log import event

LOGGER = logging.getLogger(__name__)

ALWAYS = ("sequence", "session_elapsed_ms", "utterance_id")
INFO, WARNING, ERROR = logging.INFO, logging.WARNING, logging.ERROR

LATENCY_FIELDS = (
    "utterance_kind",
    "last_stt_to_turn_complete_ms",
    "model_done_to_prepared_ms",
    "model_ms",
    "prepared_to_tts_first_audio_ms",
    "speech_end_to_output_started_ms",
    "speech_end_to_turn_complete_ms",
    "stt_final_after_speech_end_ms",
    "tts_first_audio_to_output_started_ms",
    "turn_complete_to_model_start_ms",
    "turn_complete_to_output_started_ms",
)

# session-log event -> (operational event, level, copied fields)
MIRRORED: dict[str, tuple[str, int, tuple[str, ...]]] = {
    "session_started": ("interview.session_started", INFO, ("build_id",)),
    "session_ended": (
        "interview.session_ended",
        INFO,
        ("interview_stage", "session_end_kind"),
    ),
    "interview_complete": ("interview.completed", INFO, ()),
    "interview_ended_early": ("interview.ended_early", INFO, ()),
    "response_latency": ("interview.response_latency", INFO, LATENCY_FIELDS),
    "inference_started": ("interview.inference_started", INFO, ("generation", "purpose")),
    "inference_completed": (
        "interview.inference_completed",
        INFO,
        ("action", "duration_ms", "generation", "purpose", "validated"),
    ),
    "inference_cancel_requested": ("interview.inference_cancelled", INFO, ("generation",)),
    "inference_stale_discarded": ("interview.inference_stale_discarded", INFO, ("generation",)),
    "decision_rejected": ("interview.decision_rejected", WARNING, ("generation", "reason")),
    "agent_cancellation": (
        "interview.response_cancelled",
        INFO,
        ("cancellation_reason", "generation"),
    ),
    "agent_interrupted": (
        "interview.agent_interrupted",
        INFO,
        ("cancellation_reason", "generation", "heard_enough", "heard_ms", "output_started"),
    ),
    "interruption_tentative": ("interview.interruption_tentative", INFO, ("bot_speaking",)),
    "interruption_tentative_ended": (
        "interview.interruption_tentative_ended",
        INFO,
        ("vad_active_ms",),
    ),
    "interruption_committed": (
        "interview.interruption_committed",
        INFO,
        ("source", "bot_speaking", "waited_ms", "words"),
    ),
    "interruption_rejected": (
        "interview.interruption_rejected",
        INFO,
        ("reason", "bot_speaking", "tentative_ms", "vad_active_at_decision"),
    ),
    "unheard_prompt_recovery": ("interview.prompt_replayed", INFO, ()),
    "closing_interrupted_not_resumed": ("interview.closing_interrupted", INFO, ("heard",)),
    "media_paused": ("interview.media_paused", INFO, ("reason",)),
    "media_resumed": ("interview.media_resumed", INFO, ()),
    "response_error": (
        "provider.error",
        ERROR,
        (
            "failure_stage",
            "processor",
            "category",
            "error_code",
            "error_type",
            "exception_type",
            "inference_purpose",
            "provider_status",
            "recoverable",
            "tts_audio_bytes",
            "tts_audio_chunks",
            "tts_context_elapsed_ms",
        ),
    ),
}
# Conversational controls: one operational event, naming which control, with no wording.
CONTROLS = frozenset(
    {
        "repeat_requested",
        "repeat_limit_reached",
        "restart_declined",
        "feedback_request_declined",
        "control_detour_limit",
        "candidate_skipped_question",
        "required_question_skipped",
        "floor_control_replay",
    }
)


def mirror(entry: dict[str, Any]) -> None:
    """Write the operational counterpart of one saved-conversation event, if it has one."""
    name = entry.get("event")
    if name in CONTROLS:
        event(
            LOGGER,
            "interview.control",
            record_id=entry.get("session_id"),
            control=name,
            **{key: entry.get(key) for key in ALWAYS},
        )
        return
    rule = MIRRORED.get(name)
    if rule is None:
        return
    operational_name, level, fields = rule
    event(
        LOGGER,
        operational_name,
        level=level,
        record_id=entry.get("session_id"),
        **{key: entry.get(key) for key in (*ALWAYS, *fields) if entry.get(key) is not None},
    )
