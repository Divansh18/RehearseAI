# Product workflow

## Target and current behaviour

An administrator uses one **Schedule interview** form: candidate name and email, résumé (PDF/DOCX), job description (pasted or PDF/DOCX), optional private interviewer instructions, local date/time with timezone and target duration. The latest permitted start defaults to 30 minutes after the scheduled time and is adjustable under Advanced settings. Saving schedules the interview and queues both question preparation and invitation delivery; neither waits for the other and neither blocks the form. There is **no mandatory review or confirmation step**. Review and natural-language changes are optional afterwards. There is no manual question builder.

The candidate joins with a private invitation without an account, confirms a self-reported full name, acknowledges the interview-room image disclosure, checks devices and attends. The interviewer AI conducts a conversation without coaching, feedback, scores or hiring decisions. Administrators download a Conversation PDF with one acknowledged camera snapshot.

## Statuses

- **Interview:** assigned → starting → in_progress → completed / ended_early / interrupted / failed (or cancelled / expired).
- **Preparation:** pending → preparing → ready, or failed (retryable by the administrator).
- **Invitation:** pending → sending → sent, or failed (retried automatically, then Resend).
- **Change request:** queued → running → succeeded / failed / cancelled. A failed or cancelled request leaves the previous ready plan in force.

## Access and lifecycle

Admin login/reset, CSRF and permissions are server-enforced. `GET` of an invitation never redeems it; an explicit rate-limited `POST` creates a restricted HttpOnly candidate session scoped to one assignment. Sending or resending an invitation rotates the token and revokes every earlier candidate session in the same transaction, and is refused once the interview has started. A forwarded link works for whoever holds it; the link, name, email and snapshot do not prove identity.

The joining window gates **Begin**, not an interview in progress. Begin additionally requires a valid ready plan, confirmed server-side under the assignment lock; the version used is frozen on the interview record at that moment. A revision that finishes after the interview started or was cancelled is discarded.

## Duration

The target is a soft budget based on active time (monotonic, excluding explicit media pauses). Optional probing stops as it is exhausted and optional questions are dropped; when it is exhausted the interview closes after the current candidate turn finishes. Nothing is cut off mid-speech. Questions not reached (required ones in particular) are recorded in the interview's coverage summary. Overruns of one candidate turn plus the closing are possible.

## Data and scope

Uploaded documents and extracted text are stored without automatic deletion (see README). All administrators see all interviews. Neither is suitable for unrelated companies. Synthetic tests cannot establish microphone quality, and production SMTP is unverified.
