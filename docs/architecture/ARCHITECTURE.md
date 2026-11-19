# Architecture

How RehearseAI is organised, who owns what, and where a change belongs. For product behaviour,
setup and privacy limits see [README.md](../../README.md); for how to work on it see
[CONTRIBUTING.md](../../CONTRIBUTING.md).

Module paths below, such as `plans/schema.py`, are relative to `backend/src/rehearse_ai/` unless they
start with `backend/`, `frontend/` or `docs/`.

## Vocabulary

Use these terms in code, docs and commit messages.

| Term | Meaning | Where it lives |
| --- | --- | --- |
| **Interview plan** | The questions and execution guidance for one interview. Prepared automatically from the résumé and job description, stored as immutable versions. | `plans/schema.py` (`InterviewPlanContent`, `PlanQuestion`), table `interview_blueprint_version` |
| **Assignment** | A scheduled interview for one candidate: window, duration, invitation, preparation status. | `database.models.InterviewAssignment` |
| **Attempt** | One execution of an assignment. Starting it freezes the plan version. Single use. | `database.models.InterviewRecord` |
| **Candidate session** | Restricted, expiring invitation access (an HttpOnly cookie). Scoped to one assignment. Not an administrator login. | `invitations/access.py`, `database.models.CandidateSession` |
| **Conversation record** | What an attempt persisted: transcript rows, coverage, the one room image, the Conversation PDF. | `records/`, `database.models.InterviewRecord`, `database.models.TranscriptEntry` |

Three different things are called a session. They are never interchangeable:

* **Candidate session** — invitation access (above).
* **Voice session** — one live WebRTC interview connection (`voice.pipeline.LiveSession`, tracked by
  `voice.registry.LiveSessionRegistry`). Its interview logic is `interview.session.InterviewSession`.
* **Database session** — a SQLAlchemy `AsyncSession` (`db`, `db_sessions`). Not a user concept.

An **administrator account** is `database.models.Account` (table `candidate`, a historical name).

## Folder map

The repository has three parts that change for different reasons, plus evaluation and docs:

```
RehearseAI/
  backend/               the Python project: one pyproject.toml, one lockfile, one test suite
    pyproject.toml, uv.lock, setup.py, alembic.ini, .python-version
    migrations/            Alembic (additive; 0001 .. 0007)
    src/rehearse_ai/       the package (below)
    tests/                 pytest by capability, plus architecture and schema-agreement tests
  frontend/              browser pages, scripts and styles; plain HTML/CSS/JavaScript, no build step
    pages/                 one HTML shell per page
    scripts/admin/         sign_in, records, assignments, schedule
    scripts/candidate/     invitation, interview_room, media_setup (device lifecycle), room_capture
    styles/                theme (shared tokens), pages, interview_room
    tests/                 node tests of the browser scripts
  docs/                  architecture/, product/, testing/ (checklists and dated reports), historical/
  evaluation/            checks that call real providers or browsers; not part of pytest
  docker/, compose.yaml  PostgreSQL (development and test databases) and Mailpit
  Makefile               the documented commands, run from the repository root
  README.md, CONTRIBUTING.md
```

Private runtime data is **not** in the tree: `sessions/`, `private_documents/`, `private_pdfs/` and
`private_snapshots/` are created next to where the server runs (the repository root) and are
git-ignored. Operational logs go to `logs/` (resolved explicitly; see [LOGGING.md](LOGGING.md)). See [Packaging and private storage](#packaging-and-private-storage).

```
backend/src/rehearse_ai/
  __main__.py          python -m rehearse_ai  ->  server.main
  config.py            Settings: validated runtime configuration
  database/            PostgreSQL access (imports nothing else from the application)
    models.py            every SQLAlchemy model and the one ORM metadata registry
    connection.py        open_database(): how engines and session factories are made
  operational_log/     structured JSON operational logging (imports only config)
    events.py            event(): the one way to write an operational event
    context.py           correlation ids (request, assignment, record, job)
    redaction.py         field-name and value scrubbing
    formatter.py         the JSON envelope; exception class and location, never the message
    handlers.py          bounded non-blocking queue, rotating files with age pruning
    configuration.py     configure_logging(): path resolution, one configuration, clean shutdown
  jobs/                durable background jobs shared by scheduling and plans
    runner.py            PostgreSQL-backed runner: leases, fenced results, retries
  prompts/             versioned LLM prompts (*.txt) and load_prompt()
  accounts/            administrator authentication
    service.py           accounts, sessions, rate limits, password reset, mailer
    cli.py               python -m rehearse_ai.accounts.cli  (create / promote an administrator)
  plans/               the interview plan
    schema.py            InterviewPlanContent, PlanQuestion (validated shape of a plan)
    preparation.py       AI preparation and revision of a plan, with Python-side validation
  scheduling/          scheduling an interview and managing assignments
    service.py           validate the form, persist atomically, enqueue jobs, send invitations
    assignments.py       administrator commands and read models (revise, retry, cancel, board)
    documents.py         bounded extraction of résumé / job description uploads (subprocess)
  invitations/         restricted candidate access
    access.py            invitation tokens, candidate sessions, what a candidate may see
  interview/           interview progression. No framework, no vendor SDK, no database.
    controller.py        question order, probe/repeat budgets, delivery state machine
    session.py           turn routing, cancellation, the model decision loop, one per voice session
    controls.py          deterministic recognition of conversational controls + fixed replies
    phrases.py           pure speech-pattern classifiers (repeat, corrections, name spelling)
    memory.py            candidate-supplied facts and corrections
    budget.py            soft time budget on an unpaused monotonic clock
    speech_output.py     provider speech contexts <-> utterances, audio counts
    timeline.py          server-side stage timings behind the response_latency event
    browser_events.py    allowlist of events the candidate's browser may see
    session_log.py       ordered JSONL evidence per attempt (the protected conversation file)
    operational_events.py  allowlist of session events mirrored, text-free, to the operational log
  voice/               Pipecat pipeline and providers
    pipeline.py          LiveSession: assembles STT -> LLM -> TTS and adapts frames to the interview
    interruption.py      when sound becomes a committed interruption (tentative vs committed)
    tts.py               provider TTS setup and provider-side audio observations
    registry.py          the running voice sessions of this process
    stt_evaluation.py    offline STT keyterm comparison tool
  records/             conversation records, private images, Conversation PDFs
    store.py             ordered persistence of interview events, PDF generation queue
    queries.py           administrator read models
    service.py           the one image, PDF download/regeneration, failed attempt starts
    images.py            validate, re-encode and privately store the image
    conversation_pdf.py  the PDF (fonts in records/fonts/)
    session_export.py    offline export of one recorded session
  server/              HTTP boundaries and application composition
    app.py               create_app(): builds services, wires routers, owns the lifespan
    context.py           ServerContext: the shared services
    dependencies.py      Ctx / Db / require_admin / rate limiting
    pages.py             HTML shells, /assets allowlist, home, health
    frontend.py          where the frontend files are read from (installed copy or frontend/)
    build.py             source and prompt fingerprints
    request_logging.py   request ids and logging of failed HTTP requests
    routes/              one router per capability: auth, candidate, assignments, records, attempts
```

## Dependency direction

Enforced by [`backend/tests/test_architecture.py`](../../backend/tests/test_architecture.py). A package may import only
the packages listed for it.

```
database, config, prompts                import nothing from rehearse_ai
operational_log . config                 (a leaf: any package may log through it)
jobs ............ database, operational_log
accounts ........ config, database
plans ........... jobs, database, prompts
invitations ..... database, plans
records ......... database, operational_log, plans
scheduling ...... accounts, invitations, jobs, database, plans
interview ....... operational_log, plans (a constant); no framework or vendor SDK at all
voice ........... config, interview, operational_log, prompts
server .......... everything above; nothing else imports server (only __main__ does)
```

The rule that matters most: **`interview` knows nothing about providers, HTTP or the database.**
`voice` adapts Pipecat frames into calls on an `InterviewSession`; the session talks back only
through callables it is given (`infer`, `speak`, `publish`, `cancel_audio`). Swapping STT, LLM or TTS
is a change in `voice/` and `prompts/`, never in `interview/`.

## State ownership

| State | Owner | Notes |
| --- | --- | --- |
| Assignments, plans, jobs, records, transcript, accounts | PostgreSQL via `database/models.py` | Changed only inside short transactions that lock the row first (`scheduling/assignments.py`, `invitations/access.py`, `records/service.py`, `records/store.py`). |
| Job claims | `jobs/runner.py` | `FOR UPDATE SKIP LOCKED` under a per-claim token; every state change is fenced by that token. Assumes one server process. |
| Uploaded documents | `private_documents/` | Written by `scheduling/service.py`, mode 0600, never served. |
| Raw event log per attempt | `sessions/<id>.jsonl` | Written by `interview/session_log.py` before anything else; the database copy is rebuilt from it on restart. |
| Conversation PDF, room image | `private_pdfs/`, `private_snapshots/` | Written by `records/`; only administrators can read them. |
| One attempt's interview state | `interview.session.InterviewSession` | Owns the controller, the response task, the opening timer and the `_generation` counter. **It is the only place that cancels its tasks.** |
| Speech-context mapping | `interview.speech_output.SpeechOutput` | Owned by the session; the session passes four callables, not itself. |
| Tentative/committed interruption | `voice.interruption.ConfirmedSpeechStartStrategy` | Holds its own timer; cleaned up in `cleanup()` and when a turn starts. |
| Running voice sessions | `voice.registry.LiveSessionRegistry` | Created in `server.app`, reached through `ServerContext.live`. |

**Access lifecycle.** A voice session is authorised by the candidate session that opened it and is
keyed by its assignment. The candidate leaving (`/api/candidate/logout`) revokes that candidate
session and stops the assignment's voice sessions (`LiveSessionRegistry.stop_for_assignment`).
Resending or cancelling an invitation requires an unstarted assignment, so no voice session can exist
then. Administrator sign-out and password reset end only administrator sessions: administrators never
own a voice session, and an administrator's access change must not interrupt a candidate. Server
shutdown stops every voice session. Covered by `backend/tests/server/test_access_lifecycle.py`.

**Cancellation.** Every model response runs as a task tagged with a generation number. Anything that
invalidates a pending response (the candidate speaking, a correction, a stop, a media pause, close)
increments the generation and cancels the task, so a stale result can neither speak nor advance the
interview. `InterviewSession.close()` cancels the response task and the opening timer and records the
end of the attempt exactly once.

## Workflows

**Schedule and prepare.** An administrator submits the schedule form → `server/routes/assignments.py`
→ `scheduling.service.schedule_interview` validates it, extracts the documents and, in one
transaction, writes the assignment, the private inputs and two jobs. `jobs.runner.JobRunner` then runs
`plans.preparation.PrepareHandler` (an OpenAI call whose output is validated in Python and stored
as plan version 1) and `scheduling.service.InviteHandler` (the invitation email) independently.
Optional revisions add versions; a failed revision never alters the plan in use.

**Candidate access.** `/join/<token>` renders a shell only. An explicit POST redeems the token
(`invitations.access.redeem_invitation`), which revokes earlier candidate sessions and sets the
cookie. The candidate confirms a self-reported name and checks devices; Begin stays unavailable until
the server confirms a valid plan is ready.

**An attempt.** `POST /api/offer` (`server/routes/attempts.py`) →
`records.store.create_assignment_attempt` locks the assignment, validates the stored plan against the
current schema and freezes that version on the record → a `LiveSession` is built from the frozen plan
and registered → the Pipecat pipeline runs. Events flow `InterviewSession` → `SessionLog` →
`InterviewStore.apply_event` (ordered, bounded). `room_capture.js` uploads one room image to
`POST /api/interviews/<id>/snapshot` (`records.service.accept_interview_image`). On End or
disconnect the record becomes terminal and the Conversation PDF is generated from persisted
evidence only.

**Administrator review.** `/admin/interviews` lists records; the detail page offers
**Download Conversation PDF** and, after a failure, **Regenerate Conversation PDF**
(`records/queries.py`, `records/service.py`). Candidates have no route to a PDF or an image.

## Where a change belongs

| I want to... | Go to |
| --- | --- |
| Change what a plan may contain, or how it is validated | `plans/schema.py`, `plans/preparation.py`, `prompts/plan_*.txt` |
| Change the schedule form or its validation | `scheduling/service.py`, `frontend/scripts/admin/schedule.js` |
| Add an administrator action on an assignment | `scheduling/assignments.py` (rule) and `server/routes/assignments.py` (route) |
| Change invitation or candidate access rules | `invitations/access.py` |
| Add a conversational control or fixed reply | `interview/controls.py` (and `interview/session.py` for what it does) |
| Change question order, probe or repeat limits | `interview/controller.py` |
| Change what the model is told or allowed to do | `interview/session.py` (`_assigned_payload`, validation), `prompts/assigned_decision.txt` |
| Change time budgeting | `interview/budget.py` |
| Change what the browser may see | `interview/browser_events.py` |
| Change VAD, turn timing, interruption thresholds | `voice/interruption.py`, `voice/pipeline.py`, `config.py` |
| Change a provider or voice | `voice/pipeline.py`, `voice/tts.py`, `config.py` |
| Change the PDF | `records/conversation_pdf.py` |
| Change image validation or storage | `records/images.py`, `records/service.py` |
| Add a route | the router for its capability in `server/routes/`; register it in `server/app.py` |
| Add a table or column | `database/models.py` **and** a new additive migration in `backend/migrations/versions/` |
| Change a page | `frontend/pages/`, `frontend/scripts/<audience>/`, `frontend/styles/` (and `server/pages.py` `ASSETS`); `backend/setup.py` only if a new *kind* of file is added |
| Add or change a log event, or what is logged | `operational_log/` (`event()`), `interview/operational_events.py` for saved-conversation events; rules in [LOGGING.md](LOGGING.md) |
| Change a runtime prompt | `prompts/*.txt` (documented in `docs/product/PROMPTS.md`) |
| Change a command or how the project is built | `Makefile`, `backend/pyproject.toml`, `backend/setup.py`, `CONTRIBUTING.md` |
| Document a decision | `docs/architecture/` (structure), `docs/product/` (behaviour), `docs/testing/` (checks); dated measurements go in `docs/historical/` or a clearly dated report |

## Compatibility that is deliberately kept

Renaming was done in code, not in contracts. These keep their original names:

* **Tables and columns**: `interview_blueprint`, `interview_blueprint_version`, `blueprint_id`,
  `blueprint_version_id`, `candidate` (administrator accounts), `candidate_session` (login tokens)
  and `candidate_interview_session` (candidate sessions). The ORM attributes are `plan_id`,
  `plan_version_id` and so on, mapped to those columns.
* **Routes and JSON keys**: `/api/admin/blueprint-versions/{id}`, `blueprint_version_id` in the
  administrator JSON and in the plan dict handed to a voice session.
* **Stored values**: event names and `utterance_kind` values in logs and the database, including
  kinds the controller no longer creates, because historical records contain them.
* **Browser contract**: the `snapshot_acknowledged` field of the offer and the `/snapshot` upload
  route, even though the stored object is now a room image.

The models and the migrations describe the same schema (indexes, server defaults, CHECK constraints);
see [`MODELS_AND_MIGRATIONS.md`](MODELS_AND_MIGRATIONS.md). `backend/tests/test_schema_drift.py` keeps them in agreement.

## Known structural debt

Honest list of what is *not* yet clean. None of it changes behaviour.

* `interview/session.py` is still 1.7k lines. Remaining seams, in order of value: the opening flow
  (greeting, acknowledgement, timer), correction and name routing (shares the "interrupted output"
  state with turn routing), and the model decision loop with its payload and validation.
* `interview/controller.py` still carries `UtteranceKind` and `Stage` members and a few branches
  that only the removed practice flow created (name prompt, readiness, no-project). They are inert;
  they remain because stored records may name those kinds. They can be retired together with a
  migration note once nothing reads them.
* Capability services raise `fastapi.HTTPException` for refusals (`invitations`, `scheduling.assignments`,
  `records`). A domain error type mapped in `server/` would let those packages drop FastAPI.
* `jobs/` is shared infrastructure that both `plans` and `scheduling` depend on, so it sits beside
  them rather than inside either.

## Packaging and private storage

**Frontend packaging.** There is one copy of the frontend: `frontend/`. `backend/setup.py` is the
single link between it and the backend package. It copies `pages/`, `scripts/` and `styles/` into
`rehearse_ai/web/` in the *build output* of a wheel, and into `frontend/` in the sdist, so a wheel
built from the sdist (which has no sibling `frontend/`) is complete. Nothing is written to the source
tree, and `server/frontend.py` reads the installed `rehearse_ai/web` when present and `frontend/` in a
checkout. Prompts (`prompts/*.txt`) and the PDF font and licence (`records/fonts/`) are ordinary
package data in `backend/pyproject.toml`. Public asset URLs (`/assets/scripts/...`,
`/assets/styles/...`) and the allowlist in `server/pages.py` do not depend on any of this.

**Logs.** Operational logs are the exception to the working-directory rule: `REHEARSE_LOG_DIR` is
resolved to an absolute path (repository root in a checkout, else the working directory) so the
location does not depend on where the server was started. See [LOGGING.md](LOGGING.md).

**Where relative paths resolve.** Configuration and the other storage paths are relative to the *working
directory*, not to the package or to `backend/`: `.env`, `sessions/`, `private_documents/`,
`private_pdfs/` and `private_snapshots/` (and the Alembic `DATABASE_URL` read from `.env`). Run every
command from the repository root, as the `Makefile` and the documented commands do, so those
locations stay where they always were. Moving the Python project into `backend/` moved no data.
Set the `REHEARSE_*_DIR` variables to absolute paths to make them independent of where you start.
Tests use temporary directories and a separate `*_test` database and never read the development
database.
