# RehearseAI

RehearseAI is becoming a candidate interview platform. This milestone adds verified email/password accounts and a protected candidate dashboard. The existing four-question Pipecat voice interview remains available at `/interview` for verified candidates. Assignment scheduling, saved interview records, and PDFs are **not implemented**; the dashboard shows honest empty states. The earlier take-home [recording](https://www.loom.com/share/928bba7d68ec4174856488a0ed2e1bb7), [evidence](evidence/recorded-session/), and [checklist](SUBMISSION_CHECKLIST.md) describe the prototype, not the new account flow.

## Run locally

Python **3.11+**, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Docker Compose are required for local development. Start persistent PostgreSQL and a local Mailpit inbox:

```sh
docker compose up -d --wait
uv sync --frozen --group dev
test -f .env || cp .env.example .env
```

If `.env` already exists, keep it and add the new account variables from `.env.example` rather than overwriting it. Set `REHEARSE_AUTH_SECRET` to a locally generated value of at least 32 characters (`python -c 'import secrets; print(secrets.token_urlsafe(48))'`). The local `DATABASE_URL` in `.env.example` points to the Compose development database. The test database is separate. Fill in server-side `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, and `CARTESIA_API_KEY` from [Deepgram](https://developers.deepgram.com/docs/create-additional-api-keys), [OpenAI](https://platform.openai.com/api-keys), and [Cartesia](https://play.cartesia.ai/keys). Cartesia is not needed if `REHEARSE_TTS_PROVIDER=openai`. Keys never belong in browser assets.

Apply the explicit migration, then start the app:

```sh
uv run alembic upgrade head
uv run python -m rehearse_ai
```

Open **http://127.0.0.1:7860/**. Create an account, open the verification link in the local inbox at **http://127.0.0.1:8025/**, then sign in. `/dashboard` is protected. The existing voice prototype is at `/interview` after verification; it is not an assigned interview or a saved record. `docker compose down` stops local services without deleting the database volume; do not use `down -v` unless you deliberately want to remove local data. The app binds to loopback.

For the dedicated test database, first run:

```sh
DATABASE_URL=postgresql+asyncpg://rehearse:rehearse_local_only@127.0.0.1:5433/rehearse_test uv run alembic upgrade head
uv run pytest -q
```

The account integration tests use this test database and mocked email delivery; they never delete development data. A separate local end-to-end check used real SMTP capture in Mailpit. Mailpit is an inbox for development, **not production email delivery**. `/healthz` and the voice page's collapsed Diagnostics area show the running build ID. **End session** releases the microphone and peer connection immediately. Normal and spoken early closing use a short buffered-audio drain, which is not a browser playout acknowledgment.

## Candidate account architecture

`server.py` gates `/dashboard`, `/interview`, and `/api/offer` using the candidate obtained from the server session cookie. The browser never supplies a trusted candidate ID. `accounts.py` uses [FastAPI Users 15](https://fastapi-users.github.io/fastapi-users/latest/) for Argon2 password hashing, database-backed session strategy, and signed verification/reset tokens. The package is in maintenance mode but receives security and dependency updates; its database strategy permits logout revocation and a 12-hour session lifetime. `accounts_db.py` defines PostgreSQL account, session, single-use link, and rate-limit records; `migrations/` changes schema explicitly. `Mailer` uses SMTP; local settings point to Mailpit. No database code enters the interview controller.

Verification and reset links expire after one hour and are accepted once using a stored token digest. Resending invalidates earlier verification links. Login and account actions have database-backed rate limits (login: 10 per email per 10 minutes; resend/reset requests: 3 per email per hour, with IP limits too). The session cookie is HttpOnly and SameSite=Lax; production sets Secure. Unsafe account and WebRTC setup requests require a signed, one-hour CSRF token in both a cookie and request header. The login flow uses fixed internal destinations and ignores external return URLs. Token-bearing URLs are removed from browser history on page load; Uvicorn access logs are disabled so they are not recorded there. A future reverse proxy must also redact query strings. Email verification confirms inbox control, not personal identity.

For production configuration, use a private PostgreSQL `DATABASE_URL`, a new `REHEARSE_AUTH_SECRET`, `REHEARSE_ENV=production`, an HTTPS `REHEARSE_PUBLIC_BASE_URL`, and a real SMTP service through `REHEARSE_SMTP_HOST`, `REHEARSE_SMTP_PORT`, `REHEARSE_SMTP_FROM`, `REHEARSE_SMTP_USERNAME`, `REHEARSE_SMTP_PASSWORD`, and `REHEARSE_SMTP_STARTTLS=true`. Production email credentials and delivery have **not** been configured or tested. HTTPS termination, operational monitoring, assigned interviews, durable interview storage, and PDFs remain future work. Existing ignored JSONL voice logs are not exposed by static routes or shown as saved candidate interviews. Logging out or resetting the password stops that candidate’s active voice peer. A peer that remains connected across the 12-hour cookie expiry is not yet rechecked mid-call; new setup requests and HTTP routes require a live session.

## Architecture and provider choices

`SmallWebRTC` browser audio → Pipecat `VADProcessor` (Silero) → Deepgram streaming STT (`nova-3-general`) → Pipecat `UserTurnProcessor` → `InterviewInput` → OpenAI text LLM (`gpt-4.1-mini`, structured `run_inference`) → Cartesia streaming TTS (`sonic-3.6`, Skylar `db6b0ed5-d5d3-463d-ae85-518a07d3c2b4`) → `AudioObserver` → SmallWebRTC output → `DeliveryObserver`.

The STT, text LLM, and TTS are separate services; there is no realtime speech-to-speech model. The text LLM runs as a structured Pipecat service call outside the audio frame chain, and its validated JSON is never spoken directly. `interview.py` owns exact question order, state, limits, and the provider-independent `memory.py` session facts. `session.py` routes completed turns, validates decisions and literal fact extracts, cancels stale generations, and correlates utterance IDs. In `pipeline.py`, `InterviewInput` merges final STT chunks until turn completion; `AudioObserver` correlates TTS contexts and drops stale audio; `DeliveryObserver` observes transport speaking frames. `tts.py` chooses a provider and observes provider chunks independently of transport output. `session_log.py` writes raw ordered events. `config.py` validates provider credentials and account/email settings. Versioned prompt files in `src/rehearse_ai/prompts/` are the runtime authority; [PROMPTS.md](PROMPTS.md) includes their exact text and iteration record.

The current session remembers a tentative or confirmed candidate name and small project facts with source turn references. The Interview card lets the candidate confirm or edit the name; manual confirmation takes priority over later STT variants. A clear spoken name correction is acknowledged once with the corrected name. A spoken term correction such as “I said webhooks, not web books” updates the matching fact and preserves the old value in correction history. Correction-only turns neither answer a question nor use its probe allowance; an ambiguous target gets one short clarification. The structured decision request includes current facts and asks for exact spans from the latest answer, which Python checks before storing as tentative. Raw STT is never rewritten. This memory stays in one session; it is not a durable profile or proof that a tentative extraction is accurate. The decision still uses one buffered model request, with a larger token cap for the optional fact list; latency impact has not been measured.

Deepgram offers streaming recognition but can mishear project names. The OpenAI text model supplies contextual proposals while Python enforces transitions; structured inference is fully buffered before TTS and can add delay. Cartesia uses Pipecat's streaming WebSocket service and was selected after a **six-request direct API diagnostic**: median request-to-first-PCM was **166.3 ms** for Cartesia and **1,277.8 ms** for OpenAI `gpt-4o-mini-tts`/`shimmer`; all six succeeded. These are three short utterances per provider, not reliability, voice-quality, browser, or end-to-end latency estimates. See [TTS_COMPARISON.md](TTS_COMPARISON.md) and its [tracked measurements](samples/tts-comparison-2026-10-26/measurements.json). Audio samples are local and ignored by Git. OpenAI TTS remains manually selectable through `REHEARSE_TTS_PROVIDER=openai`, with no automatic mid-response switch; its separate configuration and context-timeout diagnosis are in [TTS_DIAGNOSIS.md](TTS_DIAGNOSIS.md). Provider pricing, accents, and perceived voice quality were not benchmarked.

The configured Deepgram Nova-3 model supports optional [keyterm prompting](https://developers.deepgram.com/docs/keyterm), but hints are off for now. Candidate terms emerge during the session; a hint is not confirmation that transcription is correct, and dynamic terms may appear in provider usage records. No provider or acoustic settings changed in this milestone.

## Turn-taking, interruptions, and evidence

Defaults are VAD confidence **0.7**, speech start **0.2 s**, VAD stop **0.35 s**, and one Pipecat `SpeechTimeoutUserTurnStopStrategy` with **1.1 s** speech timeout. The turn processor also has a **30 s failure watchdog**. The timeout was raised from **0.8 s** after a real follow-up answer split at “So the idea”; a separate orchestration fallback also advanced after a rejected extra probe. The 1.1 s value is a tuning starting point, not proven to cover every mid-answer pause. Final STT segments accumulate until Pipecat completes one user turn. Speech onset triggers Pipecat interruption and cancels pending LLM/TTS immediately, independently of the longer completion wait. Python keeps one substantive probe per main question, separates explicit repeat/skip/stop, and never treats an unheard generated question as delivered.

Agent text is prepared with an utterance ID before TTS, but enters the visible transcript only on server output start. A matching `BotStoppedSpeakingFrame` after TTS completion and without cancellation is the delivery acknowledgment; Pipecat can also emit stop on interruption, so stale and cancelled stops are ignored. A cancelled TTS context's late stop is filtered before transport output, and a fresh output start retires unmatched cancellation-stop accounting. This is **server output evidence**, not proof of browser playback or human hearing. Raw `sessions/<id>.jsonl` events use a common server monotonic session clock and retain final STT chunks, VAD stops, inference, provider audio, output, cancellation, and delivery. Final STT segments arriving before Pipecat's turn-start frame are tagged with the upcoming candidate turn at ingestion. The structured LLM call exposes no first-token timestamp and the browser supplies no reliable per-utterance playback onset. These remain null in the export; `tts_first_audio_ms` is explicitly labeled as a server-output proxy. Provider first-audio is a separate observation. Historical development sessions are not the future recording's evidence.

The author-confirmed [Loom recording](https://www.loom.com/share/928bba7d68ec4174856488a0ed2e1bb7) matches session `d564949d-6780-414f-8192-22bae9e9345d`. Its tracked [transcript](evidence/recorded-session/transcript.txt), [per-turn latency](evidence/recorded-session/latency.jsonl), and [measurement notes](evidence/recorded-session/latency_summary.md) come from that session. The recording reaches question four, includes requested provisional feedback, and ends at the candidate's spoken request; it does **not** demonstrate automatic final completion. A normal project answer starts around **0:46** in Loom's visible transcript. A mid-answer pause and an interruption while the agent speaks are not yet verified in the video; the exported rows do not mark an interrupted response. See [RECORDING.md](RECORDING.md) for scenario status.

For **12 of 14** candidate turns with a correlated server output start, estimated speech-end to output-start p50 is **2798.9 ms** and p95 is **5007.3 ms** (nearest rank); the mean is **3004.9 ms**. Available stage means are speech-end → last STT final **443.2 ms** (n=14), last STT final → turn completion **1008.6 ms** (n=14), and Cartesia request → first provider audio **102.9 ms** (n=12). These are observed intervals, not additive processing costs; two turns lack a correlated output. `user_stopped_ms` is estimated from Silero VAD stop minus its silence threshold. `tts_first_audio_ms` is explicitly a **server output start proxy**, while provider first audio is a separate field; browser playback onset and human hearing are unmeasured. The structured LLM call exposes no first-token timestamp, so `llm_first_token_ms` is null. Missing or ambiguous measurements remain null. The direct-provider medians above are a separate diagnostic, not end-to-end latency.

## Limits and next day

Session memory is limited to one current value per fact field. Text-model extraction may miss details even when the raw answer contains them; the prompt can still choose an unhelpful follow-up. Spoken correction parsing covers explicit “replacement, not earlier wording” forms and one target clarification, not arbitrary paraphrases. Acoustic confidence is unavailable, so a heard name remains tentative until the candidate confirms or corrects it. The tracked recording and its latency figures predate this milestone and do not verify these new interactions.

The supplied video still needs scenario-level review for a natural pause and barge-in; the matching log ends through spoken early termination rather than normal final feedback. STT wording errors can still affect follow-ups; structured decisions may choose a poor detour or wait action despite Python guards. The single silence threshold trades fewer premature boundaries for slower turn endings. Browser playback onset, true human audibility, and LLM first token are unmeasured. With another day, I would add correlated browser playout telemetry, verify the recording's pause/interruption scenarios, tune turn timing from that evidence, and review more real answers for follow-up relevance before changing prompts.

**AI tools used:** Codex and ChatGPT assisted with implementation, debugging, testing, and documentation. **Approximate active time spent:** 4–5 hours across two days, including those activities, as estimated by the author.

## Five-minute microphone check for session memory

Start with the command above. Give a name and describe a GitHub pull-request project; check that the name is marked as heard from speech. Edit and confirm the spelling in the Interview card. Deliberately correct a term aloud, for example “I said webhooks, not web books,” then continue the answer. Check that the same question remains active, the correction does not consume the one probe, and the next follow-up asks for a detail you have not already given. Interrupt a prepared reply once, then say “stop the interview.” Afterward, inspect the session's ignored `sessions/<id>.jsonl` for separate `stt_final_segment`, `correction_applied`, `memory_updated`, cancellation, and delivery events. This is a manual acceptance script; the offline tests do not establish microphone recognition or audible behavior.

## Checks

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
node --check src/rehearse_ai/web/app.js
node --test tests/browser_ui.test.mjs
uv lock --check
uv build
```
