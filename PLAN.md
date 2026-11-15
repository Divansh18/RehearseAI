# RehearseAI plan

The current product workflow and migration decisions are in [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md). This repository now serves administrator-authored, versioned interview blueprints assigned to individual candidates through private invitations. Admins retain account login and review; candidates use a restricted invitation session and have no account/dashboard/PDF access. The earlier four-question practice-coach flow is historical and is not the web entry path.

## Current architecture

- PostgreSQL holds admin accounts, immutable blueprint versions, assignments, invitation-session digests, audit actions, attempts, transcript rows and PDF status. Migration `0005_assigned_interviews` preserves legacy records.
- FastAPI handles admin APIs, invitation redemption/CSRF, candidate welcome/setup, SmallWebRTC signaling and private admin PDF downloads.
- The provider-independent controller owns question order, required coverage, bounded probes, interruption and delivery-aware state. The session router decides controls before model inference and rejects stale responses. Pipecat keeps Deepgram STT, OpenAI text inference and Cartesia/OpenAI TTS separate.
- The browser checks media locally, preserves preview streams through the modal room and uploads one acknowledged camera still after session start. The bounded writer persists transcript events; terminal records trigger local PDF generation.

## Verification gates and open work

Run PostgreSQL tests, browser fixture tests, Ruff, lockfile/build checks, then inspect the local admin/invitation pages in a real browser and a synthetic Mailpit flow. Real microphone interviewing, production SMTP delivery, cross-device screen-capture support and conversational model quality remain separate live checks. Joining a started/interrupted attempt does not resume it; an administrator must create a fresh assignment. Local PDF and snapshot storage need backups before production use. No public deployment is included.
