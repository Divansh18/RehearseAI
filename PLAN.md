# RehearseAI plan

The target and transitional workflow are in [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md). Current admin pages show existing assignments and records. New interview scheduling is being updated; manual blueprint/assignment authoring has been retired. Existing frozen assigned plans remain executable.

## Current boundaries

- `assigned.py` validates a structured interview plan and joining window. Published versions remain immutable. The next milestone's résumé/JD preparation service should produce a validated `BlueprintContent` before a server-owned assignment is created; no parser, generator or scheduler exists yet.
- `server.py` owns admin auth, invitation/session access, read-only plan inspection, existing assignment recovery, WebRTC setup and protected record/PDF routes. PostgreSQL migrations preserve historical data.
- `interview.py` owns plan progression and delivery-aware state; `session.py` routes turns and corrections; `pipeline.py` assembles Deepgram STT, OpenAI text inference and Cartesia TTS. No candidate account or admin question editor is offered.
- Browser media setup and the compact interview room remain unchanged. The bounded writer persists transcript events; terminal records trigger private PDF generation.

## Next milestone and verification gates

Implement the one Schedule interview form and validated AI plan preparation without exposing authored questions to candidates. Preserve version isolation and existing invitations. Before release, verify PostgreSQL security/concurrency, browser and Mailpit flow, migrations, lock/build, and a live device interview. Real microphone behavior, production SMTP, resume after disconnect, model relevance and local storage backups remain open checks.
