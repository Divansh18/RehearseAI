"""Load the versioned prompts that are sent to the text LLM."""

from importlib.resources import files


def load_prompt(name: str) -> str:
    if name not in {
        "opening_ack",
        "tts_style",
        "assigned_decision",
        "plan_prepare",
        "plan_revise",
    }:
        raise ValueError("Unknown prompt")
    return files("rehearse_ai").joinpath("prompts", f"{name}.txt").read_text(encoding="utf-8")
