# Assigned interview workflow

## Agreed flow

An administrator signs in, drafts and previews a human-authored interview blueprint, publishes an immutable version, assigns that version to one candidate and a bounded joining window, and sends a private invitation. The candidate opens the link, explicitly redeems it, confirms a self-reported full name and the existing snapshot disclosure, checks devices, and clicks Begin. The interviewer follows the assigned questions and bounded probes. The administrator reviews the persisted attempt and its private Conversation PDF.

Candidates have no account, password, dashboard, transcript viewer, or PDF access. This is an interview, not a practice coach: the assistant does not score, advise, promise an outcome, or provide feedback. A feedback request receives a brief refusal and leaves the current question pending.

## Existing foundations

Keep admin password/session/CSRF protection, PostgreSQL migrations, Pipecat's separate Deepgram/OpenAI/Cartesia services, media setup and pause, local previews, snapshot acknowledgement and private storage, correction memory, delivery and stale-output guards, the bounded event writer, and admin-only PDFs. Replace public sign-up and account-owned interview entry with assignments and restricted candidate sessions. Retire fixed-question execution, coaching prompts, candidate dashboard/history routes, and practice wording once replacement tests pass. Historical account-owned records and PDFs stay readable to admins.

## Data and lifecycle

Add blueprint drafts and immutable published versions; assignments reference a version and contain UTC start/latest-start, an IANA timezone, candidate-facing fields, private adjustments, invitation digest/expiry and mail status; a small audit table records create/edit/reschedule/cancel/send events without transcript text. New attempts reference an assignment; the legacy account reference remains nullable for historical compatibility. A unique assignment-attempt constraint and locked start transition prevent duplicate attempts across tabs. Existing interview statuses remain `starting`, `in_progress`, `completed`, `ended_early`, `interrupted`, and `failed`; `cancelled` and `expired` apply to assignments before start. Page views and device checks create no attempt. The joining window gates Begin only; it does not end an active attempt. Restart/disconnect conservatively leaves a partial interrupted attempt rather than silently starting anew.

## Invitation access

Use a random opaque token and store only its SHA-256 digest. A GET is read-only and never consumes a link. Explicit POST redemption is rate-limited and exchanges the link for an HttpOnly, expiring, revocable candidate-session cookie scoped to one assignment; unsafe actions require CSRF and an Origin check. The token is removed from browser history after redemption. Token pages load only same-origin assets and send `Referrer-Policy: no-referrer`; application/access logs must omit tokens. Candidate sessions have no account/admin/history/PDF permissions. A link can be forwarded: possession does not establish identity. Email ownership, camera snapshots, and entered names do not establish identity either.

## Exclusions and verification

No résumé upload, job description parsing, generated questions, scores, feedback, identity checks, email OTP, Google login, scheduling marketplace, video recording, or provider change. Verify PostgreSQL uniqueness and locking, version isolation, expiry/revocation, scanner-safe GET, candidate/admin isolation, joining-window edges, transcript/PDF historical compatibility, bounded probes and required coverage, media/interruption behavior, and a local Mailpit/browser flow with invented candidate details. Synthetic model/media checks are not microphone evidence. Production SMTP remains separately configured and unverified locally.

## Implementation status and recovery

Migration `0005_assigned_interviews` is additive. The deployed Pipecat `LiveSession` requires a published assignment plan; the fixed four-question controller default and old practice prompts remain solely for historical unit-test compatibility and cannot be selected through the web entry path. The admin can still read legacy account-owned attempts. A single accepted offer reserves the only attempt under a row lock and unique constraint. A failed offer records `failed`; an interrupted connection or server restart reconciles a partial attempt to `interrupted`. Neither creates an automatic replacement attempt or resumes controller state. The administrator must issue a fresh assignment when another attempt is appropriate. Expired joining windows are marked `expired` on startup and reported as closed immediately during a running process. An active session is not terminated by its latest-start time.

The local browser walkthrough verified admin sign-in, blueprint preview/save/publish, assignment creation and Mailpit capture, invitation GET and explicit redemption, self-reported name confirmation, candidate device-check rendering, admin record detail and a synthetic PDF download. It did **not** grant real camera/microphone/screen permissions or conduct a live assigned voice interview. Synthetic signaling, media lifecycle, interruption and persistence paths are covered by automated tests. Production delivery, real speech quality and device behavior remain to be verified.
