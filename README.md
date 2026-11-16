# RehearseAI

RehearseAI conducts a private, company interview from an immutable, structured interview plan. An administrator signs in to review existing assignments and saved interviews. An invited candidate confirms a self-reported name, checks devices, acknowledges one camera snapshot for the admin PDF, and explicitly begins. The interview is conversational and stores its transcript incrementally. It does not coach, score, suggest answers, or provide candidate feedback.

**Transition:** New interview scheduling is being updated. The manual question editor, publication flow, and assignment creation form are retired. Existing invitations and frozen plans remain usable. The next milestone will offer one Schedule interview form with candidate details, résumé, job description, optional interviewer instructions, timezone/window, and estimated duration. It will prepare and validate the interview plan automatically; none of that preparation is implemented yet. See [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md) and [PRODUCT_CLEANUP.md](PRODUCT_CLEANUP.md).

## Local setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker Compose, and a desktop browser with camera, microphone, and screen capture on localhost or HTTPS.

```sh
docker compose up -d --wait
uv sync --frozen --group dev
test -f .env || cp .env.example .env
```

Fill `.env` without committing it: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `CARTESIA_API_KEY` for the default voice, and a random `REHEARSE_AUTH_SECRET` of at least 32 characters. `DATABASE_URL` selects PostgreSQL; keep development and test databases separate. `REHEARSE_PUBLIC_BASE_URL` must match invitation links, and `REHEARSE_INTERVIEWER_NAME` is the company/interviewer display name. OpenAI TTS remains an explicit fallback via `REHEARSE_TTS_PROVIDER=openai`. `.env.example` lists all voice, turn, storage, SMTP and server settings. Keys stay server-side.

```sh
uv run alembic upgrade head
uv run python -m rehearse_ai.admin --create admin@example.com
uv run python -m rehearse_ai
```

The admin command asks for a password; there is no default admin. For a preserved active account, `uv run python -m rehearse_ai.admin admin@example.com` promotes it. Open [http://127.0.0.1:7860/](http://127.0.0.1:7860/) and sign in. [Mailpit](http://127.0.0.1:8025/) captures local invitation and password-reset email; it is not external delivery. Production requires HTTPS, secure cookies, a production database and real SMTP credentials. Production email delivery and deployment are unverified.

## Existing interview workflow

- **Assignments** shows existing invitations, statuses, a read-only frozen-plan inspection, resend, and cancel for unstarted assignments. New creation and manual editing are unavailable during this transition.
- Candidates open an existing private link and explicitly redeem it. They confirm a self-reported full name, complete camera/microphone/speaker/screen checks, acknowledge snapshot disclosure, and click **Begin interview**. A page visit or device check creates no attempt. The joining window gates start only.
- **Records** shows historical and assigned attempts. Only admins can view, download or regenerate private Conversation PDFs. Candidates have no account, password, dashboard, transcript or PDF access.

The entered name, email, invitation possession and camera snapshot do not establish identity. A link can be forwarded. GET/link preview does not redeem it; a CSRF-protected POST exchanges it for a restricted, expiring, revocable HttpOnly cookie. Token-bearing pages use same-origin assets and `Referrer-Policy: no-referrer`; server access logging omits tokens.

## Architecture and lifecycle

`server.py` owns authenticated HTTP, admin read/recovery APIs, invitation redemption and WebRTC setup. `assigned.py` validates the structured plan and joining window and contains token/session helpers. Its `BlueprintContent` is the future AI-preparation output boundary; no generated-plan service exists yet. `accounts.py` uses maintained FastAPI Users password hashing, server sessions, CSRF and SMTP. `accounts_db.py` and `migrations/` preserve plan versions, assignments, attempts and historical records. `interview.py` owns question progression, required coverage, bounded probes, utterance IDs and delivery state. `session.py` routes completed turns and spoken controls, cancels stale work, and maintains corrected session memory. `pipeline.py` assembles separate Deepgram streaming STT → OpenAI text LLM → Cartesia streaming TTS via Pipecat SmallWebRTC. Runtime prompts are in `src/rehearse_ai/prompts/` and documented in [PROMPTS.md](PROMPTS.md).

Each existing assignment references an immutable plan version. Database locking and a unique constraint prevent duplicate starts. `starting` is reserved only after an authorised WebRTC offer; `session_started` moves it to `in_progress`. Controller completion records `completed`; spoken/end-button stop records `ended_early`; disconnect/restart records `interrupted`; signalling/provider failure records `failed`. A restart reconciles partial records rather than silently creating another attempt. Started attempts cannot resume in this version. Historical account-owned records, transcripts, snapshots and PDFs remain admin-readable. Migration `0005_assigned_interviews` is additive; no data is dropped by this cleanup.

A bounded ordered writer persists server-owned transcript events. Raw STT is not overwritten by memory corrections. Assistant prepared, output-started, completed and interrupted states stay distinct. Server transport output-stop approximates delivery; it does not prove browser playback or the exact words heard before interruption. Barge-in cancels pending output immediately while Pipecat turn completion remains authoritative. Existing defaults are VAD confidence 0.7, start 0.2 s, stop 0.35 s and turn pause 1.1 s; these are tuning values, not measured optima. Camera and screen video remain local except for one consented camera still uploaded after session start. Video and screen sharing are not recorded.

An assigned-plan decision may mark a later question covered only with an exact cited span from a collected answer; Python validates the citation and preserves its source turn. Its quality still needs real assigned-interview review. A feedback request receives a short refusal and leaves the question pending. The professional closing has no assessment or hiring promise.

PDFs are generated from persisted transcript rows after terminal outcomes, with the optional still. Files are private; admin download/regeneration requires permission and CSRF for regeneration. Database/PDF/snapshot storage is local and needs backups. Persistence and PDF failures remain visible without erasing partial conversations.

## Verification and limitations

Run `uv run --offline pytest -q`, `node --test tests/browser_ui.test.mjs`, `uv run --offline ruff check .`, `uv run --offline ruff format --check .`, `uv lock --check`, and `uv build`. PostgreSQL tests use `REHEARSE_TEST_DATABASE_URL` ending in `_test`; Compose creates a separate test database. Synthetic model/media checks do not verify microphone recognition, model relevance, browser playout or external SMTP.

To check an existing invitation: sign in as admin, inspect its assignment, read the local Mailpit invitation if previously sent or resend it, open the link in another browser context, confirm name, complete device checks, Begin, answer and interrupt, then inspect the admin record/PDF. Real devices and provider keys are needed for live voice verification. Screen capture may be unavailable on mobile. There is currently no supported UI to create a new interview.

The earlier take-home [recording](https://www.loom.com/share/928bba7d68ec4174856488a0ed2e1bb7), [recorded evidence](evidence/recorded-session/), and [submission checklist](SUBMISSION_CHECKLIST.md) describe the **historical prototype**, not this invitation workflow. They are not verification of the current product.
