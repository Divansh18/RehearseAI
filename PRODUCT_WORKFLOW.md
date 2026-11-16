# Product workflow and transition

## Target product

An admin uses one **Schedule interview** form: candidate name and email, résumé, job description, optional interviewer instructions, schedule/timezone and estimated duration. AI prepares ordered questions and follow-up guidance. The admin may request tweaks but does not author questions manually. The candidate joins through a private invitation without an account, confirms a self-reported full name, checks devices and attends. The AI interviews without coaching, scores or feedback. Admins review saved records and download Conversation PDFs.

The résumé parser, AI preparation, tweak request and new scheduling form are **not implemented**. New interview scheduling is being updated. This cleanup removes the old authoring flow rather than presenting a fake replacement. [PRODUCT_CLEANUP.md](PRODUCT_CLEANUP.md) records the keep/remove inventory.

## Existing invitations and records

Published plan versions, assignments, invitation digests, candidate sessions, audits, accounts, transcripts, snapshots and PDFs remain in PostgreSQL/private storage. Existing assignments keep their immutable plan and joining window. Admins can inspect a frozen plan read-only, resend an unstarted invitation, cancel an unstarted assignment, and review all historical records. No migration or data deletion is part of this transition. Candidate invitation redemption, name confirmation, media setup and assigned-plan execution remain available.

## Access and lifecycle

Admin login/password reset, CSRF and permissions are server-enforced. GET of an opaque invitation is scanner-safe; explicit rate-limited redemption creates a restricted HttpOnly candidate session scoped to one assignment. A forwarded link can be used by its holder; neither the link, name, email nor snapshot proves identity. Candidate sessions cannot access admin records or PDFs. The joining window gates Begin, not an active interview. A locked start and unique assignment-attempt constraint prevent duplicate attempts. Disconnect/restart preserves a partial interrupted record; no controller-state resume is claimed.

The assigned plan is the execution contract. Python owns required coverage, ordering, bounded probing and delivery-aware transitions. A future preparation service must produce a `BlueprintContent` value, persist an immutable version, then create an assignment through a server-owned scheduling operation. It must not trust candidate speech as plan instructions.

## Exclusions and verification

No candidate accounts, coaching, feedback, scores, hiring decisions, résumé parsing or AI plan generation in this cleanup. No provider, media, PDF or persistence changes. Synthetic tests can check security and lifecycle, not real microphone quality. Production SMTP and real assigned-interview model relevance remain unverified. The historical take-home recording is not evidence for this workflow.
