# RehearseAI

RehearseAI is a company-facing AI interview platform. An administrator fills in **one Schedule interview form** (candidate, résumé, job description, optional private instructions, date/time/timezone and target duration). The system saves the interview, emails the candidate a private invitation and prepares candidate-specific questions in the background. The candidate joins from the link without an account, confirms their name, acknowledges that one image of the interview room (including their camera) will be saved, checks devices and takes a conversational voice interview. The AI gives no coaching, feedback, scores or hiring decisions. Administrators download an admin-only Conversation PDF (the full conversation plus that one acknowledged interview-room image; see [below](#conversation-pdf-and-interview-room-image)).

There is no manual question builder. Questions are generated from the documents; an administrator may *optionally* review them and ask for changes in plain language.

## Local setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker Compose, and a desktop browser with camera, microphone and screen capture on localhost or HTTPS.

Run every command from the **repository root** (`.env`, `sessions/`, `private_*/` are resolved relative
to it). The Python project is in `backend/` and the browser code in `frontend/`; `uv --project backend`
selects the project without changing directory. `make setup`, `make migrate` and `make run` are
shorthand for the commands below.

```sh
docker compose up -d --wait
uv sync --frozen --project backend --group dev
test -f .env || cp .env.example .env
```

Fill `.env` without committing it: `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `CARTESIA_API_KEY` for the default voice, and a random `REHEARSE_AUTH_SECRET` of at least 32 characters. `DATABASE_URL` selects PostgreSQL; keep development and test databases separate. `REHEARSE_PUBLIC_BASE_URL` must match invitation links and `REHEARSE_INTERVIEWER_NAME` is the company/interviewer display name. `REHEARSE_LLM_MODEL` selects the OpenAI text model used both for plan preparation and the live interview. `.env.example` lists every voice, turn, storage, SMTP, job and server setting. Keys stay server-side.

```sh
uv run --project backend alembic -c backend/alembic.ini upgrade head   # 0001-0007 in order; rerun after pulling new migrations
uv run --project backend python -m rehearse_ai.accounts.cli --create admin@example.com
uv run --project backend python -m rehearse_ai
```

The admin command asks for a password; there is no default admin. For a preserved active account, `uv run --project backend python -m rehearse_ai.accounts.cli admin@example.com` promotes it. Open [http://127.0.0.1:7860/](http://127.0.0.1:7860/) and sign in. [Mailpit](http://127.0.0.1:8025/) captures all local invitation and password-reset email; nothing is delivered externally. Production requires HTTPS, secure cookies, a production database and real SMTP credentials. **Production email delivery and deployment are unverified.**

## How it works

### Administrator

1. **Schedule interview** (`/admin/schedule`): candidate name and email; résumé (PDF or DOCX); job description (pasted or PDF/DOCX); optional *private* interviewer instructions; local date/time with a visible timezone; target duration (5–120 min). The latest permitted start defaults to 30 minutes after the scheduled time and can be changed under **Advanced settings**.
2. Pressing **Schedule** validates everything, extracts the documents, then saves the assignment, the private inputs and two background jobs in one short transaction. It returns immediately.
3. Two independent jobs then run: **invitation delivery** (emailed straight away, without waiting for question preparation) and **question preparation** (an OpenAI call that produces a validated, versioned plan). Their statuses are tracked and shown separately on **Assignments**.
4. Review is optional. **Review questions** shows the plan read-only, including private notes on why each question was chosen. **Request changes** takes a natural-language request and produces a *new version*; a failed or late revision never alters the plan in use. **Retry preparation**, **Resend invitation** and **Cancel** are available for unstarted interviews.
5. **Records** lists every attempt, including a number-only coverage account (questions asked, covered, skipped for time, required questions not reached, active time). Only administrators can open records or download/regenerate PDFs.

A submission carries an idempotency key generated when the form loads; double clicks and retries return the same interview. A conflicting reuse of a key is rejected.

### Candidate

Candidates open a private link (a GET never redeems it), explicitly continue, and receive a restricted, expiring, revocable HttpOnly session scoped to one assignment. They confirm a self-reported full name, acknowledge the interview-room image disclosure, complete camera/microphone/speaker/screen checks and press **Begin**. While preparation is pending the welcome and setup pages work, but **Begin stays unavailable until the server confirms a valid plan is ready**; the page re-checks on its own. The server enforces this when the attempt starts; there is no fallback to generic practice questions.

The entered name, email, invitation possession and interview-room image do not establish identity, and a link can be forwarded. Candidate APIs expose only the title, window, duration and readiness state: never the questions, guidance, résumé, job description or instructions.

## Architecture

The layout, dependency direction, state ownership and where a change belongs are in [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md); how to work on it is in [CONTRIBUTING.md](CONTRIBUTING.md).

| Area | Where |
| --- | --- |
| Application startup, wiring and HTTP routes | `backend/src/rehearse_ai/server/` (`app.py` composes; `routes/` has one router per capability), `config.py` |
| Administrator authentication | `accounts/` |
| Interview plan schema and AI preparation | `plans/` (`prompts/plan_prepare.txt`, `prompts/plan_revise.txt`) |
| Scheduling, document extraction, assignments | `scheduling/` |
| Durable job runner | `jobs/` |
| Invitations and restricted candidate access | `invitations/` |
| Interview progression, controls, memory, time budget | `interview/` (provider-independent) |
| Voice pipeline, providers, interruption handling | `voice/` (Deepgram -> OpenAI -> Cartesia via Pipecat) |
| Records, private images, Conversation PDFs | `records/` |
| Database connections and ORM models | `database/`; schema history in `backend/migrations/` |
| Frontend | `frontend/pages/`, `frontend/scripts/{admin,candidate}/`, `frontend/styles/` |
| Documentation | `docs/` |

Package names are under `backend/src/rehearse_ai/`.

**Jobs.** `interview_job` rows are claimed with `FOR UPDATE SKIP LOCKED` under a per-claim token; every state change is fenced by that token, so a worker that lost its lease cannot apply a stale result. Concurrency is bounded (2), model calls and SMTP have timeouts, failures retry with backoff up to 3 attempts, and the last failure is stored as a short code (never document or model text). On restart, jobs a previous process left running are re-queued. This assumes **one server process** (the host is restricted to loopback). A job can run more than once after a crash, so an invitation email may in rare cases be sent twice; each send rotates the link, so only the latest email's link works.

**Plan lifecycle.** Preparation creates version 1; each successful revision adds a version. Starting an attempt takes the assignment lock, checks readiness, validates the stored plan against the *current* schema, and freezes that version on the interview record, all before anything is consumed. If validation fails the attempt is not used up. Revisions need the same lock and an unstarted assignment, so the version that starts is the version that runs.

**Conversation.** An assigned interview opens with a short greeting using the name the candidate confirmed ("Hi Drake, how's your day going?"), one brief acknowledgement and a transition into question 1. The opening is one exchange, bounded in time, counted in active time (`opening_seconds` in the coverage summary) and can never become an answer or spend question 1's probes. Transcripts are placed by when they were created, not when they arrived, because Pipecat's turn boundary can overtake the transcript that caused it (`backend/src/rehearse_ai/voice/pipeline.py`). Speech over audio that was barely heard is not treated as a reply to it, and speech over a closing ends the interview instead of resuming it. Clear conversational controls are deterministic (`backend/src/rehearse_ai/interview/controls.py`): the controller keeps an authoritative pending prompt that a repeat, a detour or a refusal always returns to; repeating is bounded and says so when exhausted; ending outranks feedback; restart and go-back are declined honestly (a full restart is unavailable within an attempt); "I don't know" and refusals are recorded as the answer and not probed again; and the closing is fixed, neutral text with no promise about hiring. Brief background sound while the interviewer speaks is only a *tentative* detection (`backend/src/rehearse_ai/voice/interruption.py`): it commits an interruption once STT hears a word or the sound lasts `REHEARSE_INTERRUPT_CONFIRM_SECS` (default 0.5 s), so genuine barge-in is slightly slower; this is duration- and transcript-based, not speaker identification, and sustained noise or other voices still interrupt. Findings, measurements and limits are in [CONVERSATION_QUALITY.md](docs/testing/CONVERSATION_QUALITY.md); the real-microphone checklist is [MANUAL_VOICE_TEST.md](docs/testing/MANUAL_VOICE_TEST.md); `evaluation/session_timings.py` reports per-stage timings from a session log without printing any transcript.

**Execution.** Python owns order, required coverage and probe limits; the model proposes actions that Python validates. Each decision receives a *bounded* context: the current question and its own follow-up guidance, short topic labels for later questions (so earlier coverage can be recognised), clipped prior answers, corrected session memory and the last delivered prompt. It does not receive the full plan, future question text or guidance, grounding evidence or the administrator's private instructions. The browser receives only allowlisted events with minimal payloads (`backend/src/rehearse_ai/interview/browser_events.py`); raw model output, prompts, plan data, memory facts and errors stay in the server log. A decision is logged as validated only after validation succeeds.

**Duration.** The target is a *soft* budget measured on a monotonic clock that excludes explicit media pauses. After 80% of the budget, or when required questions no longer fit, optional probing stops and optional questions are dropped. When the budget is used up the interview moves to its closing **after the current candidate turn finishes**; speech is never cut off. Possible overrun is therefore one candidate turn plus the closing, and a long silence is not timed out. Required questions that were not reached are recorded in the interview's coverage summary rather than hidden. Closing speech contains no feedback, scores or hiring promises (prompt plus a server check on the spoken text).

## Documents and privacy

PDF and DOCX only, up to 5 MB, PDFs up to 10 pages, text up to 30,000 (résumé) or 20,000 (job description) characters. Extraction runs in an isolated Python subprocess with a 20-second wall-clock limit and CPU limit. Encrypted, damaged, empty, scanned/image-only (no OCR), garbled, over-long and DOCX-bomb files are rejected with an explanation; incomplete text is never used silently. Résumé and job-description text, the private instructions and revision requests are sent to OpenAI to prepare questions. They are treated as untrusted data in the prompts, and the model's output is validated in Python (question count for the duration, no duplicated substance, every résumé-based question backed by a verbatim span of the résumé, normalised time budgets).

### Storage and retention limitations (this milestone)

- Uploaded originals are kept in `private_documents/` (mode 0600), and extracted résumé/job-description text, private instructions and generated plans are kept in PostgreSQL **indefinitely**. There is **no automatic deletion, retention schedule, redaction, export or erase-on-request** in this milestone.
- Interview-room images (`private_snapshots/`; older records may hold a camera-only still), PDFs (`private_pdfs/`), raw session logs (`sessions/`) and the database are local, unencrypted at rest and not backed up by the application.
- Contents are never written to application logs, but they are stored and sent to the OpenAI, Deepgram and Cartesia services as described. Review those providers' terms and your own legal obligations before using real candidate data.

### Administrator scope

There is a single organisation. **Every administrator can see and act on every interview, record, plan and PDF.** It is **not safe to onboard unrelated companies** until organisation isolation is added; multi-tenancy is deliberately out of scope.

## Verification

```sh
make migrate-test    # migrations on the isolated *_test database (DATABASE_URL pointing at it)
make test            # backend: pytest backend/tests, run serially (tests share one database)
make test-frontend   # node tests and syntax checks of frontend/scripts
make lint            # ruff check and format --check
make check build     # lockfile check; sdist + a wheel built from it (into dist/)
uv run --project backend python evaluation/plan_preparation_check.py   # real model, invented documents (uses your OPENAI_API_KEY)
```

PostgreSQL tests need `REHEARSE_TEST_DATABASE_URL` ending in `_test` (Compose creates it) and the migrations applied to it. Mocked tests, the real-model plan check and real microphone sessions are different kinds of evidence; see [PLAN_PREPARATION_CHECKS.md](docs/testing/PLAN_PREPARATION_CHECKS.md) for the real-model results. Synthetic checks do not verify microphone recognition, browser playout, interview naturalness or external SMTP.

### Local acceptance steps

1. `docker compose up -d --wait`, then apply migrations and start the app as above (from the repository root).
2. In one browser profile sign in at `/login` as an admin and open **Schedule interview**. Submit a candidate (use your own address), an invented résumé and job description, and a start time a few minutes ahead. Expect to land on **Assignments** with statuses moving from *queued* to *ready* / *sent*.
3. Open Mailpit at `http://127.0.0.1:8025` and find the invitation. Confirm it contains the link, the window and duration but no questions or private instructions.
4. In a **second, separate browser profile** open the link, press Continue, confirm your full name and check devices. If the plan is still preparing, Begin stays disabled until it is ready.
5. Back in the admin profile choose **Review questions**, then try **Request changes**.
6. When the window opens, tick the interview-room image acknowledgement and press **Begin interview**; answer, interrupt, ask to repeat, ask for feedback (it is refused) and finish or end early.
7. In **Records** open the interview, check the coverage summary and download the PDF.

Real devices and provider keys are required for step 6. Screen capture may be unavailable on mobile.

## Documentation map

**Current** (kept in step with the code):

| Document | Covers |
| --- | --- |
| [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md), [CONTRIBUTING.md](CONTRIBUTING.md) | Code layout, dependency direction, state ownership, how to work on it and verify |
| [PRODUCT_WORKFLOW.md](docs/product/PRODUCT_WORKFLOW.md), [PLAN.md](docs/product/PLAN.md), [PRODUCT_CLEANUP.md](docs/product/PRODUCT_CLEANUP.md) | Product behaviour, current state and known limits, what was retired |
| [PROMPTS.md](docs/product/PROMPTS.md) | The live prompts and what the model is given |
| [MODELS_AND_MIGRATIONS.md](docs/architecture/MODELS_AND_MIGRATIONS.md) | How the ORM models and the migrations are kept in agreement |
| [MANUAL_VOICE_TEST.md](docs/testing/MANUAL_VOICE_TEST.md) | The real-microphone and real-device checklists (not yet run against the current build) |

**Dated reports and measurements** (accurate for the build and date they describe; module names inside
them are updated, their numbers are not): [CONVERSATION_QUALITY.md](docs/testing/CONVERSATION_QUALITY.md),
[STT_EVALUATION.md](docs/testing/STT_EVALUATION.md), [PLAN_PREPARATION_CHECKS.md](docs/testing/PLAN_PREPARATION_CHECKS.md),
[TTS_COMPARISON.md](docs/historical/TTS_COMPARISON.md), [TTS_DIAGNOSIS.md](docs/historical/TTS_DIAGNOSIS.md).

**Historical prototype** (the earlier take-home; not verification of the current product): the
[recording](https://www.loom.com/share/928bba7d68ec4174856488a0ed2e1bb7), [RECORDING.md](docs/historical/RECORDING.md),
[recorded evidence](docs/historical/recorded-session) and [SUBMISSION_CHECKLIST.md](docs/historical/SUBMISSION_CHECKLIST.md).
The practice interview and its prompts it describes were removed from the code and remain only in Git
history; the invitation entry requires an assigned plan.

## Conversation PDF and interview-room image

Each new interview saves **one image of the interview room** (candidate camera on the left, the RehearseAI panel, current question/status and room controls on the right, at the room's real proportions) and an admin-only **Conversation PDF** that contains it. Admins use **Download Conversation PDF** and, after a failure, **Regenerate Conversation PDF**. Candidates cannot download PDFs or read any image.

**How the image is made, and its limits.** It is *not* an operating-system screenshot. After Begin, once the room is rendered and the already-running camera has a non-blank frame, `room_capture.js` clones the live room, swaps the camera video for its current frame, replaces the shared-screen preview with a neutral "Screen sharing active" label (screen pixels are never read or uploaded), and has the browser rasterise it through an SVG `foreignObject`. A real pixel-exact screenshot would need a second screen-sharing permission prompt, so it is deliberately not used. Consequences: fonts or effects the browser cannot rasterise may differ slightly from the live page; browsers that block canvas readback of the rasterised room fall back to a **camera-only still**, which the PDF labels as such and never presents as the room. One capture is attempted per interview (a few retries only for a blank camera frame); a failed capture never interrupts the voice interview and the PDF is still produced, stating that no image was saved.

**Server rules.** Upload is accepted only from the authenticated candidate of that attempt, only while it is in progress and awaiting its single image; a second upload returns `already_saved` and never replaces the first; End or cancellation wins races (409). The server decodes and validates the JPEG (size, dimensions, pixel count per kind), strips metadata by re-encoding, and stores it privately; filenames, ids and kind claims from the browser are not trusted. The image is browser-submitted: it is not identity verification and not tamper-proof evidence.

**Historical records.** Existing records, transcripts, camera-only stills and PDFs are untouched. A still from before this feature is labelled "camera snapshot from before interview-room images were introduced". Regeneration uses only stored evidence, and is offered only after a generation failure; PDFs already generated keep the layout they were built with. Migration `0007` adds nullable `snapshot_kind`, `utterance_kind` and `question_number` columns (additive; no backfill).
