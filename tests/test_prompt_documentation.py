"""Keep the submitted full prompt text aligned with what the app loads."""

from pathlib import Path

import pytest

from rehearse_ai.prompts import load_prompt


def test_documented_runtime_prompts_match_loaded_files() -> None:
    document = Path("PROMPTS.md").read_text(encoding="utf-8")
    for name in ("assigned_decision",):
        assert f"src/rehearse_ai/prompts/{name}.txt" in document
        assert load_prompt(name).strip()
    for removed in ("decision", "feedback", "early_feedback"):
        assert not Path(f"src/rehearse_ai/prompts/{removed}.txt").exists()
        with pytest.raises(ValueError):
            load_prompt(removed)
    assert "were removed in the structural cleanup" in document
