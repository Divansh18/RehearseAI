# Manual microphone test script

For a real person with a real microphone. Automated tests cannot verify recognition, echo, voice-activity detection or what the browser actually plays. Run this after any change to turns, the opening or interruptions.

**Setup.** Local app and Mailpit running; schedule an interview to your own address with an *invented* résumé and job description; start time now. Use **headphones** (speakers can echo into the microphone and cause false interruptions) in a quiet room, and a second profile for the candidate. Note the browser and microphone.

After the run, analyse the newest file: `uv run python evaluation/session_timings.py sessions/<newest>.jsonl` (prints no transcript text), and note `response_latency` events in that log.

| # | Do this | Expect | Record |
| --- | --- | --- | --- |
| 1 | On the welcome page confirm your full name (e.g. "Drake Example"), finish setup, press Begin. | First words: "Hi Drake, how's your day going?" It does not ask for your name or use your email. | Exact greeting; time to first audio. |
| 2 | Reply: **"I'm good, let's start."** | "Great, let's get started." then question 1, with no small talk. | Gap before question 1. |
| 2b | Repeat the session; reply **"I'm good, thanks, a bit tired."** | A short neutral acknowledgement (no praise, no question) plus "Let's get started.", then question 1. | The acknowledgement; whether it felt natural. |
| 3 | Answer question 1 but **pause mid-sentence for about two seconds** ("the main thing I did was… *(pause)* …use queues"). | One combined answer. The pause may end your turn early; you should not get a reply that ignores the rest of your sentence. | Did it reply during the pause? Log: `inference_cancel_requested`, number of turns. |
| 4 | Say a term the recogniser might mishear (e.g. "webhooks", a project name). If it is transcribed wrongly, say **"I said webhooks, not web books."** | A short acknowledgement, the corrected term used afterwards, the raw transcript unchanged in the PDF. | What was misheard; `stt_confidence` for that segment. |
| 5 | **Interrupt during preparation**: finish an answer, then speak again within about a second, before any reply starts. | No stale reply; your continuation is processed once (one decision for the combined answer). | Log: `inference_cancel_requested`, `late_transcript_continuation`. |
| 6a | **Interrupt during playback**: let a question play for 3 s or more, then give a short answer. | The answer is accepted; the question does not resume. | Log: `agent_interrupted` with `heard_enough: true`. |
| 6b | Talk over the **first half second** of a question. | The question is asked again; your words are not treated as an answer; no probe is used. | `heard_enough: false`, `unheard_prompt_recovery`. |
| 6c | Over a barely heard question say **"go on"** or **"yes, please cut"**. | The question is replayed; nothing is recorded as your answer. | `floor_control_replay`. |
| 7 | Reach the closing (or let the time budget run out) and say **"thanks, bye"** over it. | The interview ends; the closing does not repeat. | `closing_interrupted_not_resumed`; the browser ends the session. |
| 8 | In another run say **"Please end the interview."** | A short polite closing, then the interview ends and a PDF is produced. | Time from your words to the closing. |

**Also check, every run:** no transcript fragment appears in the *wrong* turn (compare `turn_correlation`; there should be no long waits for `pre_start_segment`); the PDF shows the conversation in order; the admin coverage summary reports `opening_seconds`.

**Report back:** per-stage times from `response_latency` for a few responses (STT after speech end, wait to turn complete, model, TTS, output start), any misrecognitions with their `stt_confidence`, and anything that felt like a wrong interruption.
