# ruff: noqa: E501
"""Per-stage timings and sequencing diagnostics from one local session log. Prints no text.

    uv run --project backend python evaluation/session_timings.py sessions/<id>.jsonl

Works on older logs (derived from raw events) and newer ones (``response_latency`` events are
shown as recorded). All times are the server's monotonic clock. "Output started" is server
transport output, NOT browser playback; VAD stop is an estimate of the end of speech.
Not measured by any log: browser playback, Deepgram-side processing, network transit, and the
audio-to-VAD delay before ``vad_started``.
"""

import json
import statistics
import sys
from pathlib import Path


def load(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def t(row):
    return row["session_elapsed_ms"] / 1000


def main(path):
    rows = load(path)
    first = lambda events, after, before=1e9: next(  # noqa: E731
        (r for r in rows if r["event"] in events and after <= t(r) < before), None
    )
    finals = [r for r in rows if r["event"] == "candidate_final"]
    segments = [r for r in rows if r["event"] == "stt_final_segment"]
    outputs = [r for r in rows if r["event"] == "agent_output_started"]
    print(f"session {Path(path).name[:8]}  events={len(rows)}  duration={t(rows[-1]):.0f}s")

    print("\nTURN SEQUENCING")
    empty = [r for r in finals if r.get("segment_count") == 0]
    cancelled = [r for r in rows if r["event"] == "inference_cancel_requested"]
    pre = [r for r in segments if r.get("turn_correlation") == "pre_start_segment"]
    late = [r for r in segments if r.get("turn_correlation", "").startswith("late")]
    print(f"  completed turns: {len(finals)}")
    print(f"  turns completed with NO transcript (segment_count=0): {len(empty)}")
    print(f"  model calls cancelled because the candidate resumed: {len(cancelled)}")
    print(f"  transcripts delivered after their turn closed (pre_start_segment): {len(pre)}")
    print(f"  transcripts placed by creation order (late_*): {len(late)}")
    for seg in pre:
        nxt = next((r for r in finals if t(r) > t(seg)), None)
        print(
            f"    pre-start segment at {t(seg):.1f}s waited {t(nxt) - t(seg):.1f}s for a later turn boundary"
            if nxt
            else f"    pre-start segment at {t(seg):.1f}s never reached a boundary"
        )

    print("\nSTT AND TURN COMPLETION (strictly within each turn's window)")
    print("   turn | last VAD stop -> first transcript after it | VAD stop -> turn complete")
    previous = 0.0
    lags = []
    for fin in finals:
        window = (previous, t(fin))
        previous = t(fin)
        stops = [r for r in rows if r["event"] == "vad_stopped" and window[0] < t(r) <= window[1]]
        if not stops:
            print(
                f"   {fin['turn_number']:4d} | no VAD stop inside this turn's window (speech not delimited)"
            )
            continue
        stop = stops[-1]
        after = [r for r in segments if t(r) >= t(stop) and r["event"] == "stt_final_segment"]
        inside = [r for r in after if t(r) <= t(fin)]
        wait = t(fin) - t(stop)
        if inside:
            lag = t(inside[0]) - t(stop)
            lags.append(lag)
            note = f"{lag:5.2f}s"
        else:
            nxt = after[0] if after else None
            note = (
                f"none before the boundary; arrived {t(nxt) - t(fin):.2f}s after it"
                if nxt
                else "none"
            )
        print(f"   {fin['turn_number']:4d} | {note:<43} | {wait:5.2f}s")
    if lags:
        print(
            f"  transcript after VAD stop: median {statistics.median(lags):.2f}s, max {max(lags):.2f}s "
            f"(n={len(lags)} turns where one arrived before the boundary)"
        )

    print("\nRESPONSES (turn_complete -> server output start)")
    print(
        "   utt | model | model->prepared | prepared->tts_first_audio | tts->output | total from turn_complete"
    )
    stage = []
    for out in outputs:
        uid = out["utterance_id"]
        prep = next(
            (r for r in rows if r["event"] == "agent_prepared" and r["utterance_id"] == uid), None
        )
        fin = [r for r in finals if t(r) <= t(prep)] if prep else []
        fin = fin[-1] if fin else None
        if not prep or not fin:
            continue
        start = first({"inference_started"}, t(fin), t(prep) + 0.001)
        done = first({"decision_proposed", "inference_completed"}, t(fin), t(prep) + 0.001)
        audio = first({"tts_first_audio_generated"}, t(prep), t(out) + 0.001)
        model = f"{t(done) - t(start):5.2f}" if start and done else "  n/a"
        p_to_a = f"{t(audio) - t(prep):5.2f}" if audio else "  n/a"
        a_to_o = f"{t(out) - t(audio):5.2f}" if audio else "  n/a"
        total = t(out) - t(fin)
        stage.append(total)
        print(
            f"   {uid:3d} | {model}s |        {t(prep) - (t(done) if done else t(prep)):5.2f}s |"
            f"                    {p_to_a}s |    {a_to_o}s | {total:5.2f}s"
        )
    if stage:
        print(
            f"  turn_complete->output: median {statistics.median(stage):.2f}s, max {max(stage):.2f}s (n={len(stage)})"
        )
    recorded = [r for r in rows if r["event"] == "response_latency"]
    for r in recorded:
        keys = {k: v for k, v in r.items() if k.endswith("_ms") and v is not None}
        print(f"  recorded response_latency utt {r['utterance_id']}: {keys}")

    print("\nINTERRUPTIONS")
    for r in [r for r in rows if r["event"] == "agent_interrupted"]:
        out = next((o for o in outputs if o["utterance_id"] == r["utterance_id"]), None)
        heard = f"{t(r) - t(out):.2f}s" if out else "no audio had started"
        extra = f" heard_enough={r['heard_enough']}" if "heard_enough" in r else ""
        print(f"  utt {r['utterance_id']} ({r.get('cancellation_reason')}): heard {heard}{extra}")
    for name in (
        "barge_in_answer_accepted",
        "unheard_prompt_recovery",
        "floor_control_replay",
        "candidate_skipped_question",
        "late_transcript_continuation",
    ):
        count = sum(r["event"] == name for r in rows)
        if count:
            print(f"  {name}: {count}")


if __name__ == "__main__":
    main(sys.argv[1])
