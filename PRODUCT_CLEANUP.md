# Product cleanup inventory

The next admin flow is one **Schedule interview** form. It will accept candidate
details, a résumé, a job description, optional interviewer instructions, a
joining window and an estimated duration. A future preparation service will
produce a validated, versioned `BlueprintContent` plan before assignment. This
cleanup does not create that form or service.

| Decision | Existing parts | Reason |
| --- | --- | --- |
| Keep | Admin authentication, password reset/provisioning, CSRF and roles | Admin access remains account based. |
| Keep | `BlueprintContent`, published versions, assignments, invitation sessions, and migrations | Existing invitations must execute their frozen plans; the plan schema is the output boundary for future AI preparation. |
| Keep | Assignment list/detail, resend and cancel; candidate welcome and media setup | Existing invitations need operational recovery and their restricted candidate access. |
| Keep | Pipecat execution, corrections/memory, delivery guards, transcript/snapshot persistence and admin PDFs | These are independent of how a plan is prepared. |
| Remove | Manual blueprint editor, preview/publish UI and write APIs; manual assignment create/edit UI and write APIs | New interviews must not require admin-authored questions or expose a hidden legacy creation route. |
| Remove | Obsolete candidate account/history pages and manual session-name controls | Candidate access is invitation scoped; name confirmation on the invitation stays. |
| Retain internally for now | Historical fixed-question controller tests and practice prompt branches | They are not reachable through the web or `LiveSession`. Removing them safely requires a separate tested runtime refactor; they cannot create a new assigned interview. Historical evidence remains labelled historical. |

No schema migration or data deletion is needed. Published assignments and
historical records stay readable; new interview scheduling is temporarily
unavailable. The future preparation service should validate its structured
output with `BlueprintContent`, persist an immutable version, and create the
assignment through a server-owned scheduling operation. Candidate speech must
never become plan instructions.
