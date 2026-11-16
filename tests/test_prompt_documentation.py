"""Keep the submitted full prompt text aligned with what the app loads."""

from pathlib import Path

from rehearse_ai.prompts import load_prompt


def test_documented_runtime_prompts_match_loaded_files() -> None:
    document = Path("PROMPTS.md").read_text(encoding="utf-8")
    for name in ("assigned_decision", "assigned_closing"):
        assert f"src/rehearse_ai/prompts/{name}.txt" in document
        assert load_prompt(name).strip()
    assert "cannot be selected through the invitation WebRTC entry" in document
