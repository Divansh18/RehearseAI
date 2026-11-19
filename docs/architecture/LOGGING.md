# Logging and diagnostics

RehearseAI keeps three different kinds of record. They have different contents, owners and
retention, and are never merged.

| Record | Where | Holds | Who may read it |
| --- | --- | --- | --- |
| **Operational log** | `logs/application.jsonl`, `logs/interview.jsonl` (and the console) | What the system did and how long it took: lifecycle, failures, job attempts, interview transitions, interruption and latency measurements. **No candidate or model text.** | Operators of the host |
| **Protected session files** | `sessions/<attempt id>.jsonl` | The saved conversation's full event stream, including transcript, model and memory text. Evidence for the Conversation PDF and the database rebuild. | Administrators and operators; treat as candidate data |
| **Audit trail** | PostgreSQL `interview_assignment_audit` | Who did what to an assignment (scheduled, revised, cancelled ...) | Administrators |

The operational log is a new, separate sink. It does not replace or rewrite the other two:
`SessionLog` still writes its file exactly as before, and the audit table is unchanged.

## Locations and files

`REHEARSE_LOG_DIR` (default `logs`) is **resolved to an absolute path at startup** and logged in
`log.configured` and `app.started`. A relative value resolves against the repository root when
running from a checkout, otherwise against the working directory, so starting the server from the
root or from `backend/` writes to the same folder. The directory is created `0700`, files `0600`,
and `logs/` is Git-ignored (and hidden in the VS Code Explorer).

| File | Events |
| --- | --- |
| `logs/application.jsonl` | `app.*`, `http.*`, `job.*`, `persistence.*`, `pdf.*`, `log.*` |
| `logs/interview.jsonl` | `interview.*`, `voice.*`, `provider.*` (from the interview and voice packages) |

Rotated files are `<name>.1` (newest) to `<name>.N`.

## Record format

One JSON object per line, UTF-8, the same envelope everywhere:

```json
{"ts":"2026-11-19T14:30:01.123+00:00","level":"INFO","event":"job.succeeded","category":"job",
 "logger":"rehearse_ai.jobs.runner","service":"rehearse-ai","build_id":"0.1.0+2fa733ae94c7",
 "job_id":"…","assignment_id":"…","job_kind":"prepare","duration_ms":4120}
```

* `ts` is UTC with milliseconds. `level` is DEBUG, INFO, WARNING, ERROR or CRITICAL.
* `event` is `<category>.<what_happened>`, lower-case and stable; `category` is its first part.
* Correlation ids, when known: `request_id` (one per HTTP request, also returned as the
  `X-Request-ID` response header), `assignment_id`, `record_id` (the attempt, which is also the
  saved-conversation file name) and `job_id`. They are random UUIDs; nothing else identifies a
  person. Candidate names and email addresses are not logged.
* `error` appears on failures: `{"type": "<exception class>", "frames": ["file.py:function:line", ...]}`.
  The exception *message* is never logged, because messages often echo the data that caused them.

## Events

| Category | Events |
| --- | --- |
| Lifecycle | `log.configured`, `app.started` (resolved storage paths, build, flags), `app.stopping`, `app.stopped`, `app.startup_failed` |
| HTTP | `http.request_rejected` (4xx; 429 at WARNING), `http.request_failed` (5xx and unhandled exceptions). Route template, method, status and duration only. Successful requests are not logged. |
| Jobs | `job.started`, `job.succeeded`, `job.attempt_failed`, `job.retry_scheduled`, `job.failed`, `job.cancelled`, `job.stale_result_discarded`, `job.requeue_lost`, `job.exhaustion_handler_failed`, `job.claim_loop_failed`, `job.unknown_kind` |
| Interview | `interview.attempt_started`, `interview.attempt_refused`, `interview.attempt_start_failed`, `interview.session_started`, `interview.session_ended`, `interview.completed`, `interview.ended_early`, `interview.control` (which control, no wording), `interview.prompt_replayed`, `interview.closing_interrupted`, `interview.media_paused`, `interview.media_resumed` |
| Interruptions and cancellation | `interview.interruption_tentative`, `..._tentative_ended`, `..._committed`, `..._rejected`, `interview.agent_interrupted`, `interview.response_cancelled`, `interview.inference_cancelled`, `interview.inference_stale_discarded` |
| Latency and decisions | `interview.response_latency` (the per-stage timings), `interview.inference_started`, `interview.inference_completed`, `interview.decision_rejected` |
| Voice | `voice.session_started`, `voice.session_stopped`, `voice.pipeline_failed` |
| Providers | `provider.error` (stage, processor, category, code, provider HTTP status; never the provider's message) |
| Persistence and PDF | `persistence.queue_full`, `persistence.write_failed`, `persistence.notify_failed`, `persistence.flush_timed_out`, `persistence.raw_log_tail_invalid`, `persistence.attempt_interrupted_by_restart`, `pdf.generated`, `pdf.generation_failed`, `pdf.queue_full` |
| The logger itself | `log.records_dropped` (queue overflow), `log.format_failed` |

The latency and interruption events that existed in the protected session file are kept there
unchanged. A fixed subset is *also* written to the operational log by an explicit allowlist
(`interview/operational_events.py`): an event is mirrored only if it is listed, and then only its
listed numeric and code fields are copied.

## Privacy policy

Operational logs never contain: credentials, tokens or cookies (including invitation tokens and
digests), request bodies, query strings, headers, résumé or job-description text, private
instructions, candidate names or addresses, transcript text, model prompts or replies, memory
facts, or exception messages. This is enforced in layers:

1. Call sites pass identifiers, counts, durations and fixed codes (see `operational_log.event`).
2. The session-event mirror is an allowlist per event, so free-text fields such as `text`,
   `candidate_text` and memory `value` have no path into the log.
3. The formatter drops any field whose name says content or credential (`text`, `token`, `email`,
   `name`, `password`, `body`, `prompt`, ...), scrubs bearer tokens, `sk-` keys, email addresses,
   `password=` pairs and long token-like strings from the values it keeps, and truncates strings
   and collections.
4. HTTP logging uses the route *template* (`/api/invitations/{token}`), never the real path.

The redaction rules are a backstop, not a licence to pass content; tests assert that invented
secrets, résumé text, emails and exception messages are absent from every file.

### How the protected session files differ (inspected)

Inspecting recent `sessions/*.jsonl` (field names only) shows they are conversation evidence, not
logs. Free text appears in `text` (`agent_prepared`, `agent_delivery_reported`, `agent_interrupted`,
`candidate_final`, `stt_final_segment`, `decision_proposed`, `barge_in_answer_accepted`,
`early_end_requested`), `candidate_text` (`unheard_prompt_recovery`), memory `value`
(`memory_updated`) and the `coverage_summary` object. They also carry the attempt id, assignment id
and timings. They are created `0600`, one per attempt, written synchronously for ordering and
crash recovery, are **not rotated or expired automatically**, and are never rewritten or moved by the
logging system. Do not copy them into the operational log, a log shipper or a ticket. The existing
tools that read them (`evaluation/session_timings.py`, `records.session_export`, the store's
restart replay) are unchanged.

## Not blocking the interview

Logging happens off the audio path. Application code does a non-blocking `put_nowait` onto one
**bounded queue** (`REHEARSE_LOG_QUEUE_SIZE`, default 10,000). A single background thread formats
records and writes them to the rotating files and the console, so a slow or full disk cannot delay
speech processing.

* **Overflow:** if the queue is full the *new* record is dropped and counted. As soon as a record
  fits again it is followed by one `log.records_dropped` event with the number dropped by level, so
  a gap is always visible. Older queued records are never discarded to make room.
* **Failures never propagate.** A full queue, a broken file or formatter, an unwritable directory
  or a failing mirror is counted and swallowed. An unwritable `REHEARSE_LOG_DIR` disables the files
  (one line on stderr) and keeps the console; the application starts regardless. A file that stops
  being writable reports once on stderr, not per event.
* **Shutdown:** the server stops the writer after the application has stopped, draining what is
  queued. If a writer is stuck it gives up after about five seconds rather than hang the process.
* **No duplicates:** logging is configured once, in `main()`, by replacing any earlier
  configuration; the `rehearse_ai` logger does not propagate to the root logger. Tests build apps
  without configuring logging, and a library `NullHandler` keeps unconfigured runs silent.

## Configuration

Read from `.env` or the environment (see `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `REHEARSE_LOG_DIR` | `logs` | Directory for the files (resolved as above) |
| `REHEARSE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `REHEARSE_LOG_CONSOLE` | `on` | JSON lines on standard output |
| `REHEARSE_LOG_FILES` | `on` | Rotating local files |
| `REHEARSE_LOG_MAX_MB` | `10` | Rotate a file at this size |
| `REHEARSE_LOG_BACKUPS` | `5` | Rotated files kept per log (at least 1) |
| `REHEARSE_LOG_RETENTION_DAYS` | `14` | Delete rotated files older than this |
| `REHEARSE_LOG_QUEUE_SIZE` | `10000` | Bound of the in-memory queue |

Disk use is bounded by `2 × (backups + 1) × max size`; rotated files also expire by age.

## Access expectations

Logs are for the operators of the host. There is no web viewer and no candidate-facing or
administrator-facing log route. Keep the directory `0700`/files `0600`, do not attach raw files to
tickets without review, and ship them (if at all) only to a service you would trust with the
operational metadata. Candidate content belongs in the protected files and the database, under the
retention caveats in the README.

## Adding an event

Use `rehearse_ai.operational_log.event(LOGGER, "category.what_happened", level=..., **fields)` with
ids, counts, durations and codes. Do not pass text a person wrote or said; do not pass an exception
message (pass `exc_info=True` to record its class and location). To mirror a saved-conversation
event, add it to `interview/operational_events.py` with the exact fields to copy.

## Remaining production limitations

* Local rotating files are the only built-in destination. There is no shipping, alerting, metrics
  or tracing; use the console output with a collector for that.
* Correlation ids are per process; there is no cross-service trace.
* `sessions/` files are not rotated, expired or encrypted, and contain candidate text.
* A burst larger than the queue can drop records (counted and reported), and a hard kill loses
  whatever was still queued.
* Provider and framework (Pipecat/loguru, uvicorn) console output is separate and is not routed
  through this logger.
