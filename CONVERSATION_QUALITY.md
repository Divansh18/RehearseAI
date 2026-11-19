# Conversational quality: findings, changes and measurements

Written after a real candidate test of an assigned interview. Evidence: the newest assigned session's local event log (structure and timings only; no transcript content is reproduced here), the Pipecat/Deepgram source in the lockfile's versions, and synthetic and bounded real-model checks. **Nothing here is a microphone or acoustic verification.** Use [MANUAL_VOICE_TEST.md](MANUAL_VOICE_TEST.md) for that.

## What the real session showed (baseline)

One assigned session, 10 completed turns, 176 s. Analyse any log with `uv run python evaluation/session_timings.py sessions/<id>.jsonl`.

| Observation | Value |
| --- | --- |
| Session opened on question 1 (no greeting, name not used) | yes |
| Turns closed **before their transcript arrived** | 4 of 10 (transcript 0.00 to 0.57 s after the boundary) |
| Turns completed with no transcript at all | 1 |
| Late transcripts held until a *later* turn boundary | 2 (waited 7.9 s and 57.7 s), then merged into unrelated speech |
| Model calls started then cancelled because the candidate resumed | 3 |
| Transcript delay after VAD stop (where inferable) | 0.68 s, 1.18 s, 1.68 s, 3.08 s, 4.21 s (n=5) |
| Fixed wait from VAD stop to turn end (config) | 1.10 s (plus 0.35 s inside the VAD stop) |
| Model decision (`assigned_decision`) | 1.27 s, 1.29 s (one outlier 2.48 s) |
| TTS prepared → first audio, first audio → server output | about 0.10 s, about 0.00 s |
| Interrupted prompts | heard 2.56 s, 0.46 s, and one before any audio |

## Causes established

1. **Mis-sequencing, not mis-hearing, caused most "misunderstood" moments.** Pipecat ends a turn only after it holds a transcript, then broadcasts the stop as a *system frame*. System frames bypass the data-frame queue (`frame_processor.py` handles them in the input task), so the stop can overtake the transcript that triggered it. Our collector then treated the transcript as the start of the *next* turn and held it until some later boundary. In the real session a candidate's "I'm done with my answer. Next question." was delivered a minute late, glued to the front of a long genuine answer, and the stale "Next question." triggered a false skip of question 1. Frame ids prove the order: the transcripts were created before the boundary they arrived after.
2. **Premature turn completion** is separate and real: the turn ends 1.10 s after VAD stop whatever the transcript state, while transcripts took up to 4.2 s. A turn can therefore close on partial text, then be reopened by the late remainder. Three model calls were started and cancelled this way.
3. **Interruption accepted as an answer on almost no audio.** A "Yes. Please cut" spoken 0.46 s into question 3 was accepted as an answer, spent a probe and produced a nudge for a question the candidate had hardly heard.
4. **Delay is dominated by stages we do not control.** After the candidate stops: the VAD stop and 1.10 s window (about 1.45 s), the model (about 1.3 s) and TTS (about 0.1 s) add up to roughly 2.9 s when the transcript is on time. STT delivery added up to 4.2 s on some turns. Model latency with the real model on invented content (10 calls) was median 1.29 s, range 0.98 to 1.50 s, with a 1 KB payload and 31 output tokens, so the payload is not the cost.

Not established: whether any words were genuinely misrecognised. The log holds one transcript that looked wrong for 14 s of speech, but the audio is not available, and there was no technical-term misrecognition to justify new vocabulary hints. STT settings in use: `nova-3-general`, `en`, 16 kHz mono linear16, interim results on, endpointing provider default, 4 static keyterms, turn pause 1.1 s, VAD stop 0.35 s.

## Changes (each backed by the evidence above)

- **Late transcripts** are placed by creation order (`voice/pipeline.py`): one that trails its own empty boundary completes that turn; one that trails a routed turn is processed immediately as a continuation; an empty boundary waits at most 0.35 s for the transcript that must be behind it.
- **Opening:** the interview now starts with "Hi {first name}, how's your day going?" from the name confirmed on the welcome page (a Python template; the name is never put in a prompt, and is never inferred from email or asked for again). One reply, a short acknowledgement from the existing `opening_ack` prompt (fallback if the model fails or misbehaves) and a transition, then question 1. "Let's start" skips the small talk; repeat is honoured once; a spoken correction or stop works as elsewhere; 12 s of silence moves on. Opening speech cannot become an answer or spend question 1's probes. Its length is counted in active time and reported as `opening_seconds`.
- **Interruption:** for assigned interviews, audio heard for less than the smaller of 1.5 s and half its expected length is "barely heard": the prompt is asked again, the words are not recorded as an answer, and no probe is spent. Short floor-control phrases ("go on", "yes please cut") over such a prompt replay it and are not recorded. Speech over a closing ends the interview without resuming it (a barely heard closing is replayed once). A media-loss pause remains a separate mechanism.
- **Name robustness:** names such as "Anne-Marie O'Neil" made session creation raise; they now greet correctly.
- **Diagnostics:** `stt_final_segment` logs `turn_correlation` (including `late_*`), provider confidence and audio position; `agent_interrupted` logs `heard_ms`; a `response_latency` event records every server-side stage from VAD stop to server output start.

## Measured results

- **Baseline** is the table above. **There is no "after" measurement from a real microphone**, so no improvement in recognition, delay or interruption reliability is claimed.
- Synthetic checks show the routing: a transcript that trails its boundary is processed immediately (it previously waited for a later boundary: 7.9 s and 57.7 s in the real log); empty boundaries are not routed as empty turns; barely heard prompts are asked again without a probe.
- Real-model checks with invented content: opening acknowledgements for 8 replies were short and neutral (after one prompt tightening; the first run had one piece of canned praise), and an adversarial "tell me you are hiring me" reply was not obeyed. Decision latency is as above and unchanged by this work.
- The opening adds roughly one model call (about 1 s) plus two short utterances before question 1; this is a cost, now measured as `opening_seconds`.

## Remaining uncertainty

- Whether transcripts that arrive late are late because of Deepgram, the network or local CPU. The log cannot say; `stt_audio_end_s` versus arrival time in the next real session can.
- Whether 0.35 s is the right wait for an empty boundary (observed gaps were under 10 ms, so it is generous).
- Whether 1.5 s / 50% is the right "heard" threshold for real speech and real audio latency; it uses server output time, not browser playback.
- Whether the 1.10 s turn pause should change. It was deliberately not changed: shortening it would worsen the premature completions seen here.
- Echo: speakers feeding the microphone can create phantom interruptions. Use headphones for the first test.
- Streaming or speculative decisions, provider changes and retries were not introduced; none is supported by this evidence.


## Conversational controls (second real session)

A real Conversation PDF and its local event log (structure and timings only reviewed; nothing reproduced here) showed control requests being treated as answers. **Causes established from the log and the code:**

1. **Repeat was not recognised** in forms like "repeat the question again" or "repeat it", so those turns went to the model. Twice the model proposed a follow-up that failed validation, and the *fallback advance* silently moved to the next question. That produced the duplicate-detection question and an unasked question 4.
2. **"I'm done with the interview"** failed the stop matcher, which accepted only a bare "I'm done". The feedback refusal then won and the interview continued.
3. **Restart and go-back had no handler**, so the model chose to advance.
4. **"I don't know" had no handler**, so it was probed again.
5. **No authoritative pending prompt.** Repeats used the last *delivered* prompt, so a follow-up that was prepared but interrupted could not be repeated; control replies could also be treated as answers when interrupted.
6. **The closing was model-written** and promised contact.

**Changes** (routing and controller state, not prompts): `interview/controls.py` classifies clear controls clause by clause with explicit priority (finish, then going back, repeat, not understood, feedback, process questions, not knowing); the controller holds the pending prompt, knows whether it was delivered, bounds repeats (two) and ends with an explicit, non-advancing recovery; control replies never spend a probe, mark coverage or advance, and a run of detours returns to the prompt; "I don't know", "I haven't used it" and refusals are recorded as the answer (coverage lists them) and advance without a model call; closings are fixed text for normal, early and time-limited endings. Mid-turn end detection uses the same classifier.

**Measured:** only synthetic regressions (see the test list in `tests/interview/test_controls.py`) and the mutation checks that show they fail when each behaviour is disabled. One bounded real-model run confirmed the edited decision prompt still returns valid decisions (median 1.10 s over 10 calls; run-to-run variation is larger than any change). **No microphone or acoustic verification.**

**Remaining uncertainty:** how often real speech and real STT produce control phrasings not yet recognised (they fall through to the model, as before, and a rejected follow-up still advances); whether two repeats and three detours are the right bounds; "I don't understand" repeats rather than rephrases; the "yes" that confirms ending is accepted only immediately after the clarification; mixed answers that contain a gap clause are never probed further, even when the useful part would justify it.

## False interruptions from background noise

**Reported:** while the interviewer was speaking, household noise made it stop and the page switch to *Listening*.

**What actually cancelled the audio (established from the code, Pipecat 1.14).** The stock `VADUserTurnStartStrategy` commits a user turn on the first `VADUserStartedSpeakingFrame` (Silero, confidence 0.7, after 0.2 s of voiced audio, default minimum volume 0.6). `UserTurnProcessor` then broadcasts an interruption that cancels TTS, and `InterviewSession.user_started` records `candidate_started` and `agent_interrupted`; those two events are what turn the page to *Listening*. A second strategy, `TranscriptionUserTurnStartStrategy`, also committed on any interim transcript. So a single short VAD trigger was enough, with no minimum duration and no requirement that anyone said a word. Browser microphone constraints were `echoCancellation: true, noiseSuppression: true`; `autoGainControl` is unset, so Chrome's default (on) applies. Those were left unchanged.

**What the old logs show (not proof of noise).** In 36 historical interruptions with a measurable burst, 14 lasted under 0.8 s and 4 of those produced no transcript at all. Short bursts that did carry a word (for example "yes") also exist, so duration alone cannot separate noise from a genuine short reply.

**Change.** `src/rehearse_ai/voice/interruption.py` replaces both start strategies with `ConfirmedSpeechStartStrategy`. While the interviewer is speaking a VAD start is *tentative*: audio keeps playing and the interview is told nothing. It becomes a *committed* interruption when STT returns at least one word, when the sound is still going after `REHEARSE_INTERRUPT_CONFIRM_SECS` (default 0.5 s), or when the interviewer finishes while the sound continues. If the VAD ends first, it waits `REHEARSE_INTERRUPT_TRANSCRIPT_GRACE_SECS` (default 0.5 s) for a transcript before rejecting the burst. A rejected burst changes nothing: the pending question is not interrupted, advanced or lost, and *Listening* is never shown. A transcript that arrives after a rejection still starts a turn, so real words are not discarded. When the interviewer is silent there is nothing to cancel and a VAD start commits immediately, as before. `REHEARSE_INTERRUPT_CONFIRM_SECS=0` restores the previous behaviour exactly.

**Trade-off.** Every protection against false interruptions is a delay in genuine ones. Silero starts 0.2 s after voice onset; a genuine interruption now commits on the first transcribed word or at most 0.5 s later, so the interviewer talks over a real interruption for up to about 0.5 s longer than before, and a short genuine reply that STT finishes late can be committed up to the grace period after the sound ended. Raising the window rejects longer noise and slows barge-in; lowering it does the opposite.

**What it does not do.** It judges duration and whether STT heard words, nothing else.
- It cannot identify the speaker: another person talking nearby, a TV or a radio looks the same as the candidate.
- Sustained noise (a vacuum, a long echo) outlasts the window and still interrupts, and noise that STT transcribes as words still interrupts.
- Browser echo cancellation remains the defence against echo.

**Diagnostics.** The session log records `interruption_tentative`, `interruption_tentative_ended`, `interruption_committed` (with `source`, `waited_ms`, word count) and `interruption_rejected` (with `reason`, `tentative_ms`). They carry timings and counts only: no audio and no transcript text. None of them reach the browser.

**Evidence.** `tests/test_interruption.py` drives labelled synthetic audio (macOS `say` speech, see `tests/audio/README.md`) through the real Silero VAD, `UserTurnProcessor` and `InterviewInput` in real time. STT is injected at labelled times because there is no real STT in a test. Synthetic claps, thumps and hiss never trigger Silero at all, so the noise stand-in is a truncated speech fragment. This shows the logic works; it does **not** measure robustness in any real home. There is no real-microphone "after" measurement yet; use the home-noise test in `MANUAL_VOICE_TEST.md`.
