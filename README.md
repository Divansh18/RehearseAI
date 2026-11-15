# RehearseAI

RehearseAI runs a company-authored voice interview for one invited candidate. An administrator drafts and previews an interview blueprint, publishes an immutable version, assigns it to a candidate and a joining window, and sends a private link. The candidate confirms a self-reported full name, checks devices, acknowledges the camera snapshot disclosure, and explicitly begins. RehearseAI asks the assigned questions conversationally, saves the conversation incrementally, and closes professionally. It gives no feedback, score, answer suggestions, or hiring decision.

## Local setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker Compose, and a desktop browser with camera, microphone, and screen capture on localhost or HTTPS.

```sh
docker compose up -d --wait
uv sync --frozen --group dev
test -f .env || cp .env.example .env
```

Fill in `.env` without committing it: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `CARTESIA_API_KEY` for the default voice, and a random `REHEARSE_AUTH_SECRET` of at least 32 characters. `DATABASE_URL` selects PostgreSQL; keep the development and test databases separate. `REHEARSE_PUBLIC_BASE_URL` must match the address used in invitation email. Set `REHEARSE_INTERVIEWER_NAME` to the company/interviewer display name. Cartesia can be replaced explicitly with `REHEARSE_TTS_PROVIDER=openai`, which does not require a Cartesia key. `.env.example` lists all voice, turn, storage, SMTP, and local server settings. Keys stay server-side.

```sh
uv run alembic upgrade head
uv run python -m rehearse_ai.admin --create admin@example.com
uv run python -m rehearse_ai
```

The admin command asks for a password interactively; no default admin exists. For a preserved active account, `uv run python -m rehearse_ai.admin admin@example.com` promotes it. Open [http://127.0.0.1:7860/](http://127.0.0.1:7860/) and sign in as admin. Open [Mailpit](http://127.0.0.1:8025/) to inspect locally captured invitations or password resets. Mailpit is not external delivery. Production needs HTTPS, secure cookies, a production database and configured real SMTP credentials. No production email delivery or deployment was verified here.

## Admin and candidate workflow

1. In **Blueprints**, write the title, purpose, ordered required/optional questions, topic guidance, probe limits (0–2 per question), focus areas, estimated duration, style and closing instructions. Preview, save and publish. Editing a draft does not change a published version or an existing assignment.
2. In **Assignments**, select a published version, candidate email and optional known name, candidate focus areas/instructions, an IANA timezone, and start/latest-start timestamps with matching UTC offsets. Save to send an invitation, or resend after a mail failure. An admin may edit/reschedule/cancel only before the attempt starts. Mail status and a small action audit remain separate from interview outcome.
3. The candidate opens the private link, clicks to redeem it, confirms a full name, completes camera/microphone/speaker/screen checks, acknowledges that one camera snapshot is saved for the administrator's PDF, and clicks **Begin interview**. A page visit or device check creates no attempt. The joining window gates the start; it does not end an active interview.
4. Admin **Records** shows historical account-owned attempts and new assignment attempts. Only admins can view/download or regenerate private Conversation PDFs. Candidates have no account, password, dashboard, transcript or PDF route.

The name is **self-reported**; email, invitation possession and camera snapshot do not prove identity. A forwarded link can be used by its holder. GET/link preview does not redeem it; an explicit CSRF-protected POST exchanges it for a restricted, expiring, revocable HttpOnly cookie. The token is removed from the browser URL afterward. Token-bearing pages load only same-origin assets and send `Referrer-Policy: no-referrer`; server access logging is disabled. Do not forward links or server logs casually.

## Architecture and lifecycle

`server.py` owns authenticated HTTP, admin APIs, invitation redemption and WebRTC setup. `assigned.py` validates plans/windows and holds token/session helpers. `accounts.py` uses maintained FastAPI Users password hashing, server-side sessions, CSRF checks, reset flow and SMTP. `accounts_db.py` plus `migrations/` hold PostgreSQL schema. `interview.py` owns question progression, required coverage, probes, pending utterance IDs and delivery state. `session.py` routes completed candidate turns and spoken controls, cancels stale model/speech work, and supplies compact corrected session memory. `pipeline.py` assembles separate Deepgram streaming STT → OpenAI text LLM → Cartesia streaming TTS through Pipecat SmallWebRTC. Runtime prompts are in `src/rehearse_ai/prompts/` and described in [PROMPTS.md](PROMPTS.md).

A new assignment references an immutable blueprint version; one `interview_record` may reference that assignment. Database locking and a unique constraint prevent duplicate attempts. `starting` is reserved only after an authorised WebRTC offer; `session_started` moves it to `in_progress`. Controller completion alone records `completed`; spoken/end-button stop records `ended_early`; disconnect/restart records `interrupted`; signalling/provider failure records `failed`. Cancellation before start stays an assignment state. A restart reconciles partial records rather than silently creating another attempt. The assignment and attempt are not resumable in this milestone. Historical account-owned records, transcripts, snapshots and PDFs remain in place and admin-readable. Migration `0005_assigned_interviews` is additive and has no destructive downgrade.

The bounded ordered writer persists server-owned transcript events during the interview; raw STT is not overwritten by later memory corrections. Assistant prepared/output-started/completed/interrupted states remain distinct. A server transport output-stop acknowledgment approximates delivery; it does not prove browser playback or exact words heard before interruption. Candidate barge-in cancels pending output immediately while Pipecat turn completion remains authoritative. VAD confidence is 0.7, start 0.2 s, stop 0.35 s, and turn pause 1.1 s by default. These are existing tuning values, not a measured optimal setting. The browser keeps camera and screen preview local, sends only microphone audio to the voice pipeline, and uploads one consented camera still after actual session start. It does not record video or screen share.

If the model proposes that an answer already covers a later authored question, it must cite an exact span of that collected answer. Python validates the quote, records its source turn, and skips asking that question again; the raw transcript remains unchanged. This is a structured interpretation, not proof that every important detail was covered. Its quality needs real assigned-interview review.

PDF generation uses persisted transcript rows after a terminal outcome, with the optional still. Files live outside public assets and require admin permission to download; regeneration is CSRF-protected. Storage is local and requires database/PDF/snapshot backups. Persistence or PDF failures remain visible and do not erase partial transcripts.

## Verification and limitations

Run `uv run --offline pytest -q`, `node --test tests/browser_ui.test.mjs`, `uv run --offline ruff check .`, `uv run --offline ruff format --check .`, `uv lock --check`, and `uv build`. PostgreSQL integration tests use `REHEARSE_TEST_DATABASE_URL` ending in `_test`; the Compose test database is separate. Tests use invented candidate data and synthetic media/model events. They do not verify microphone recognition, natural model interviewing, browser audio playout or external SMTP delivery.

Manual check: sign in as admin; create/publish a short blueprint; assign an invented candidate for a near-term window; read the local invitation in Mailpit; open the link in a separate browser context; confirm the name; check devices and disclosure; Begin; answer, pause mid-answer, interrupt, correct a term, and ask for feedback (expect a refusal); complete or stop; as admin open the saved record and download its PDF. A real device and voice-service keys are required for a real interview. The candidate's link may be too early, expired or revoked; the interface explains these states. Screen capture support varies by browser and is often unavailable on mobile.

The earlier take-home [recording](https://www.loom.com/share/928bba7d68ec4174856488a0ed2e1bb7), [recorded evidence](evidence/recorded-session/), and [submission checklist](SUBMISSION_CHECKLIST.md) document the **historical prototype**, not this invitation workflow. They must not be treated as verification of this milestone. [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md) records the agreed product boundaries and migration decisions.
