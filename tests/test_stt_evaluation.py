import json
import wave

import pytest

from rehearse_ai.config import ConfigurationError, parse_stt_keyterms
from rehearse_ai.voice.stt_evaluation import compare, contains_phrase, load_clips, word_errors


def test_keyterms_are_bounded_and_can_be_disabled():
    assert parse_stt_keyterms("") == ()
    assert parse_stt_keyterms(" GrepAI, GitHub ") == ("GrepAI", "GitHub")
    with pytest.raises(ConfigurationError):
        parse_stt_keyterms("GrepAI,grepai")
    with pytest.raises(ConfigurationError):
        parse_stt_keyterms(",".join(str(i) for i in range(13)))
    with pytest.raises(ConfigurationError):
        parse_stt_keyterms("GrepAI:1.5")


def test_word_error_and_term_scoring_keep_graph_ai_distinct():
    assert word_errors("GrepAI uses GitHub", "Graph AI uses GitHub") == (2, 3)
    assert contains_phrase("We tested Graph AI yesterday", "Graph AI")
    assert not contains_phrase("We tested Graph AI yesterday", "GrepAI")
    assert contains_phrase("I built webhooks.", "webhooks")


def test_same_audio_is_reused_for_alternating_comparison(tmp_path):
    clips = []
    for index in range(2):
        path = tmp_path / f"sample{index}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 160)
        clips.append(
            {
                "id": f"clip-{index}",
                "file": path.name,
                "reference": "GrepAI uses webhooks",
                "expected_terms": ["GrepAI", "webhooks"],
                "negative_terms": ["Graph AI"],
            }
        )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"clips": clips}), encoding="utf-8")
    loaded = load_clips(manifest)
    calls = []

    def fake(audio, _key, _model, keyterms):
        calls.append((audio, keyterms))
        return "GrepAI uses webhooks" if keyterms else "Graph AI uses webhooks"

    result = compare(loaded, "unused", "nova-3-general", ("GrepAI",), fake)
    assert [row["condition"] for row in result["results"]] == [
        "baseline",
        "hints",
        "hints",
        "baseline",
    ]
    assert calls[0][0] == calls[1][0]
    assert calls[2][0] == calls[3][0]
    assert result["results"][0]["negative_term_matches"]["Graph AI"]
    assert result["results"][1]["expected_term_matches"]["GrepAI"]
    assert result["request_count"] == 4


def test_manifest_rejects_nonmatching_audio_format(tmp_path):
    path = tmp_path / "wrong.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"clips": [{"id": "one", "file": path.name, "reference": "hello"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mono 16-kHz"):
        load_clips(manifest)
