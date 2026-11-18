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

| 9 | **Repeat**: after any question say **"Can you repeat the question again?"**, then **"Repeat it"** twice more. | The same question each time; the third time a plain "I've already repeated that" with options. It never moves to a new question. | Which phrasings worked; `repeat_requested`, `repeat_limit_reached`. |
| 10 | **Detour then repeat**: say **"Can you give me feedback?"**, then **"Can you repeat it?"** | A short refusal with no promise about the hiring team, then the *same* question. | The refusal wording; `feedback_request_declined`. |
| 11 | **Finish with feedback**: say **"I'm done with the interview. Can you give me feedback?"** | A brief closing, no feedback and no further question. Say **"I'm done."** alone in another run: it should ask whether you want to end or are finished with the question, and "yes" should end it. Say **"Don't end the interview."**: it carries on. | Closing text; `control_recognised`. |
| 12 | **Restart / go back**: say **"Can we go back to the first question?"** and **"Let's start the interview again."** | It says it cannot restart or go back, offers to repeat the question or end, and stays on the same question. | `restart_declined`; the question did not change. |
| 13 | **Not knowing**: on a question about a tool, say **"I haven't used that."**, then in another run **"I haven't used Kafka, but I used Airflow for jobs."**, **"I don't know."**, **"I'd rather not answer."** | It records your words and moves on; it does not ask again about the same tool; the Airflow part is kept. **"I don't understand the question"** repeats it; **"I don't know if you heard me"** does *not* skip. | `candidate_gap_recorded`; the admin coverage line "answered by not knowing or declining". |
| 14 | **Process questions**: ask **"Who is the hiring person?"** and **"How many questions are left?"** | It says it has no hiring information (no invented names), and gives the question count and planned minutes. | Whether anything was invented. |
| 15 | **Normal ending**: answer every question. | "That concludes the interview. Thank you for your time, {your first name}." with no promise that anyone will contact you. | The exact closing. |

**Also check, every run:** no transcript fragment appears in the *wrong* turn (compare `turn_correlation`; there should be no long waits for `pre_start_segment`); the PDF shows the conversation in order; the admin coverage summary reports `opening_seconds`.

**Report back:** per-stage times from `response_latency` for a few responses (STT after speech end, wait to turn complete, model, TTS, output start), any misrecognitions with their `stt_confidence`, and anything that felt like a wrong interruption.

## Real-device acceptance: Conversation PDF and room image

Use a real laptop/phone with a real camera and screen share. Do not rely on synthetic-camera runs.
1. Open the invitation; confirm the setup text says one interview-room image, including your camera, is saved for the admin-only Conversation PDF, and Begin stays disabled until acknowledged.
2. Share a screen showing something recognisable (e.g. a coloured window). Begin; answer one question, then End.
3. As admin, click **Download Conversation PDF**. Confirm the room image shows your camera, the RehearseAI panel, current question/status and controls, and the shared screen appears only as "Screen sharing active" (none of its pixels).
4. Confirm the caption says browser-rendered, not a screenshot; name, email, date/timezone, duration and status are correct; the conversation is in order with relative times; no scores or recommendations.
5. End immediately after Begin (before the first question) and check the PDF still builds, stating no image was saved if none was captured.
6. In Safari and a phone browser, check the image or the labelled camera-only fallback; the interview must never stall.
7. Signed in as the candidate, confirm there is no way to download the PDF or image.
