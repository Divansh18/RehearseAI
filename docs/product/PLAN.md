# RehearseAI plan

The product and its boundaries are described in [README.md](README.md) and [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md).

## Current state

- **Scheduling.** One Schedule interview form saves the assignment, private preparation inputs and two jobs atomically, then returns. `scheduling/service.py` validates the form (timezone-aware, rejecting daylight-saving gaps and overlaps) and is idempotent per form load.
- **Preparation.** `plans/preparation.py` generates a versioned `InterviewPlanContent` from the résumé, job description, private instructions and duration, validates it in Python and stores it as an immutable version. Optional natural-language revisions create new versions. Candidates cannot begin until the server has a valid ready plan.
- **Jobs.** `jobs.py` is a PostgreSQL-backed runner (claiming, leases, fenced results, retries, restart recovery). Preparation and invitation delivery are separate jobs with separate statuses.
- **Execution.** Unchanged providers and media handling. Model context per decision is bounded; browser events are allowlisted; the target duration is a soft budget (`interview/budget.py`); coverage, including required questions not reached, is recorded.
- **Conversation.** Confirmed-name opening, late-transcript placement, heard-enough interruption handling, closing-interruption handling and per-stage latency logging are implemented and tested synthetically. See [CONVERSATION_QUALITY.md](CONVERSATION_QUALITY.md). Conversational controls (repeat, finish, restart/go-back, not knowing, bounded detours, fixed closing) are deterministic and tested synthetically. Not yet verified with a real microphone: run [MANUAL_VOICE_TEST.md](MANUAL_VOICE_TEST.md).
- **Admin scope.** Single organisation, global admins. Unsafe for unrelated companies; no multi-tenancy by design in this milestone.

## Open checks and known limits

- Real microphone sessions with a prepared plan, production SMTP, resume after disconnect (there is none), model relevance at scale and backups/retention of local storage remain unverified or unimplemented. See README "Storage and retention limitations".
- Real-model plan checks (three invented scenarios, one run each) are recorded in [PLAN_PREPARATION_CHECKS.md](PLAN_PREPARATION_CHECKS.md). They are a small sample, not an evaluation.
- Live follow-up quality under the reduced context has been tested with scripted models only.
- The job runner assumes one server process.

## Possible next milestones (not started)

Retention and deletion controls; organisation isolation; OCR for scanned documents; resume-after-disconnect; richer revision history UI; a real-voice evaluation of assigned interviews.
