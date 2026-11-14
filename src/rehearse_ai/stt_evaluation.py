"""Bounded, same-audio Deepgram keyterm comparison for consented WAV clips.

This uses Deepgram's prerecorded endpoint, so request time is not live-stream
latency. No clip is sent unless --run is explicitly supplied.
"""

import argparse
import json
import re
import time
import wave
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from rehearse_ai.config import DEFAULT_STT_KEYTERMS, parse_stt_keyterms

MAX_CLIPS = 12
MAX_BYTES = 5_000_000
MAX_SECONDS = 30


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text.casefold(), flags=re.UNICODE)


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    expected, actual = words(reference), words(hypothesis)
    previous = list(range(len(actual) + 1))
    for index, target in enumerate(expected, 1):
        current = [index]
        for offset, candidate in enumerate(actual, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[offset] + 1,
                    previous[offset - 1] + (target != candidate),
                )
            )
        previous = current
    return previous[-1], len(expected)


def contains_phrase(text: str, phrase: str) -> bool:
    target = words(phrase)
    source = words(text)
    return bool(target) and any(source[i : i + len(target)] == target for i in range(len(source)))


def load_clips(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        raise ValueError(f"manifest needs 1–{MAX_CLIPS} clips")
    seen: set[str] = set()
    result = []
    for clip in clips:
        clip_id = clip.get("id")
        reference = clip.get("reference")
        path = manifest_path.parent / clip.get("file", "")
        if not isinstance(clip_id, str) or not clip_id or clip_id in seen:
            raise ValueError("clip IDs must be nonempty and unique")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"clip {clip_id} needs a reference transcript")
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError(f"clip {clip_id} is missing or exceeds {MAX_BYTES} bytes")
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 16000
                or wav.getcomptype() != "NONE"
                or wav.getnframes() > 16000 * MAX_SECONDS
            ):
                raise ValueError(f"clip {clip_id} must be mono 16-kHz PCM16 WAV, ≤30 seconds")
        for field in ("expected_terms", "negative_terms"):
            if not isinstance(clip.get(field, []), list) or not all(
                isinstance(term, str) and term.strip() for term in clip.get(field, [])
            ):
                raise ValueError(f"clip {clip_id} has invalid {field}")
        seen.add(clip_id)
        result.append({**clip, "path": path})
    return result


def transcribe(audio: bytes, api_key: str, model: str, keyterms: tuple[str, ...]) -> str:
    query: list[tuple[str, str]] = [("model", model), ("language", "en"), ("punctuate", "true")]
    query.extend(("keyterm", term) for term in keyterms)
    request = Request(
        "https://api.deepgram.com/v1/listen?" + urlencode(query),
        data=audio,
        headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"},
        method="POST",
    )
    with urlopen(request, timeout=35) as response:
        payload = json.load(response)
    return payload["results"]["channels"][0]["alternatives"][0]["transcript"]


def compare(
    clips: list[dict[str, Any]],
    api_key: str,
    model: str,
    keyterms: tuple[str, ...],
    transcriber: Any = transcribe,
) -> dict[str, Any]:
    """One request per clip and configuration, alternating the first configuration."""
    results: list[dict[str, Any]] = []
    for index, clip in enumerate(clips):
        audio = clip["path"].read_bytes()
        conditions = ("baseline", "hints") if index % 2 == 0 else ("hints", "baseline")
        for condition in conditions:
            start = time.perf_counter()
            try:
                hypothesis = transcriber(
                    audio, api_key, model, keyterms if condition == "hints" else ()
                )
                error = None
            except (HTTPError, URLError, KeyError, ValueError, TimeoutError) as exc:
                hypothesis = None
                error = {
                    "type": type(exc).__name__,
                    "http_status": exc.code if isinstance(exc, HTTPError) else None,
                }
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            edits, count = (
                word_errors(clip["reference"], hypothesis)
                if hypothesis is not None
                else (None, None)
            )
            results.append(
                {
                    "clip_id": clip["id"],
                    "condition": condition,
                    "reference": clip["reference"],
                    "transcript": hypothesis,
                    "error": error,
                    "request_to_response_ms": elapsed_ms,
                    "word_errors": edits,
                    "reference_words": count,
                    "wer": round(edits / count, 4) if count else None,
                    "expected_term_matches": {
                        term: contains_phrase(hypothesis, term) if hypothesis else False
                        for term in clip.get("expected_terms", [])
                    },
                    "negative_term_matches": {
                        term: contains_phrase(hypothesis, term) if hypothesis else False
                        for term in clip.get("negative_terms", [])
                    },
                }
            )
    return {
        "method": "Deepgram prerecorded API, not live streaming or end-to-end response latency",
        "model": model,
        "language": "en",
        "punctuate": True,
        "hint_keyterms": list(keyterms),
        "clip_count": len(clips),
        "request_count": len(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true", help="send each clip twice to Deepgram")
    args = parser.parse_args()
    clips = load_clips(args.manifest)
    if not args.run:
        print(f"Validated {len(clips)} clips; --run would make {2 * len(clips)} requests.")
        return
    if args.output is None:
        parser.error("--output is required with --run")
    key = dotenv_values(".env").get("DEEPGRAM_API_KEY")
    if not key:
        parser.error("DEEPGRAM_API_KEY is missing from .env")
    settings = dotenv_values(".env")
    model = settings.get("REHEARSE_STT_MODEL") or "nova-3-general"
    if model not in {"nova-3-general", "nova-3"}:
        parser.error("REHEARSE_STT_MODEL must be a Nova-3 model")
    keyterms = parse_stt_keyterms(
        settings.get("REHEARSE_STT_KEYTERMS")
        if settings.get("REHEARSE_STT_KEYTERMS") is not None
        else ",".join(DEFAULT_STT_KEYTERMS)
    )
    result = compare(clips, key, model, keyterms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {result['request_count']} request results to {args.output}")


if __name__ == "__main__":
    main()
