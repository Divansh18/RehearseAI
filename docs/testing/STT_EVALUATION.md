# Speech recognition and delay evaluation

## Current path and setting

The browser requests a microphone with echo cancellation and noise suppression, without a fixed device, channel count, or capture rate. WebRTC sends **only the microphone track** to SmallWebRTC. Pipecat decodes incoming WebRTC audio and resamples it to mono, signed 16-bit PCM at 16 kHz for Silero VAD and Deepgram. The installed Pipecat 1.14.0 Deepgram adapter uses Nova-3 general, English, punctuation and interim results. It emits `TranscriptionFrame` for a Deepgram `is_final` segment; that is not itself an answer boundary. Pipecat `UserTurnProcessor` makes that decision after Silero VAD (`start_secs=0.2`, `stop_secs=0.35`, confidence `0.7`) and the configured `REHEARSE_TURN_PAUSE_SECS=1.1`. The app accumulates final segments until `UserStoppedSpeakingFrame`, then invokes the interview session once.

Static Nova-3 hints are configured with `REHEARSE_STT_KEYTERMS=RehearseAI,GrepAI,GitHub,webhooks`. An empty value disables them. The installed adapter passes a list as repeated `keyterm` query parameters at connection setup. Its settings update reconnects the stream; it does not use Deepgram's newer mid-stream `Configure` message. We therefore do not apply candidate-specific names or corrections to an active Deepgram stream. Spoken spelling/corrections update session memory and leave original transcription unchanged. Hints may bias a genuine “Graph AI” toward “GrepAI”; the negative clip below is necessary before claiming improvement.

See [Deepgram keyterm prompting](https://developers.deepgram.com/docs/keyterm), [live API](https://developers.deepgram.com/reference/speech-to-text/listen-streaming), and [prerecorded API](https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded). Keyterm prompting supports Nova-3 but does not guarantee recognition. The installed adapter's source in the locked environment was checked for `keyterm` list handling and update behaviour.

## Existing real timing evidence

Session `d51515ab-5d4a-45cc-8376-cf1c66237fc9` (14 November 2026) has two completed candidate turns. No audio was retained for replay. Using its server monotonic event clock:

| Stage | First turn | Second turn |
| --- | ---: | ---: |
| VAD stop → last final STT receipt | 295.6 ms | 111.1 ms |
| Last final STT receipt → turn completion | 807.4 ms | 992.0 ms |
| Turn completion → LLM request | 3.9 ms | N/A; no LLM |
| LLM request → validated decision | 2,273.0 ms | N/A |
| TTS request → first provider audio | 93.2 ms | 107.2 ms |
| First provider audio → server output start | 1.5 ms | 2.5 ms |

These are two observed turns, not a benchmark or proof of the candidate's perceived delay. VAD stop is already later than the estimated speech end by roughly its configured stop window; it is not the last audible phoneme. The first turn's gap is chiefly the buffered structured model decision, while the STT-to-turn wait is also material. Browser playback onset and LLM first token are unavailable. No silence, endpointing, or model setting was changed from this small sample. Deepgram endpointing remains at its provider default; Pipecat turn completion is the only answer boundary.

An older log (`5defd97b-6009-4951-90d5-47e044f4a80c`) contains one raw “Graph AI” final segment, but no corresponding audio or verified reference spelling. That cannot establish a word error rate or prove what the speaker said.

## Same-audio comparison

Copy `evaluation/stt_clips.example.json` to a local manifest in an ignored directory, then record **your own consented** examples using its scripts. Include several names, GrepAI, genuine Graph AI, ordinary sentences, a thinking pause, and a correction. Do not use the historical private session logs as input. Convert each recording to mono 16-kHz PCM16 WAV (for example, `ffmpeg -i input.m4a -ac 1 -ar 16000 -c:a pcm_s16le output.wav`). Files must be at most 30 seconds and 5 MB. Adjust the reference to match what was actually spoken.

```sh
uv run --project backend python -m rehearse_ai.voice.stt_evaluation --manifest sessions/stt-eval/manifest.json
uv run --project backend python -m rehearse_ai.voice.stt_evaluation --manifest sessions/stt-eval/manifest.json --run --output sessions/stt-eval/results.json
```

The first command validates only. The second makes exactly two Deepgram requests per clip, alternating which condition goes first. Both requests use identical audio bytes, model, language, and punctuation; one uses no hints, the other uses the configured hints. The output records transcript, per-clip WER, exact term presence, possible false substitutions, request-to-response time, and errors. The prerecorded API is used for controlled recognition comparison; its request time is **not** streaming finalization, user-turn, LLM, TTS, transport, or browser latency. There are no audio clips in the repository and no real recognition comparison has been run. Text-only correction tests establish routing, not acoustic accuracy.

For a microphone check, say an unfamiliar name, then spell/correct it; describe GrepAI and webhooks; deliberately say genuine “Graph AI” in another project context; pause mid-answer; and correct one term. Inspect `stt_final_segment`, `candidate_turn_completed`, `correction_applied`, `memory_updated`, `inference_started`, `inference_completed`, `provider_tts_requested`, `provider_first_audio`, and `agent_output_started` in that session's ignored JSONL. These events share a session monotonic clock. Do not infer browser playback from `agent_output_started`.
