"""Keep the submitted full prompt text aligned with what the app loads."""

from pathlib import Path

import pytest

from rehearse_ai.prompts import load_prompt

REPOSITORY = Path(__file__).resolve().parents[2]
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "rehearse_ai" / "prompts"


def test_documented_runtime_prompts_match_loaded_files() -> None:
    document = (REPOSITORY / "PROMPTS.md").read_text(encoding="utf-8")
    for name in ("assigned_decision",):
        assert f"src/rehearse_ai/prompts/{name}.txt" in document
        assert load_prompt(name).strip()
    for removed in ("decision", "feedback", "early_feedback"):
        assert not (PROMPTS_DIR / f"{removed}.txt").exists()
        with pytest.raises(ValueError):
            load_prompt(removed)
    assert "were removed in the structural cleanup" in document
