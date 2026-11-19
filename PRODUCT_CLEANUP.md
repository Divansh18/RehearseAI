# Product cleanup inventory

The manual authoring flow was retired earlier and has now been replaced by automatic preparation behind one Schedule interview form (see [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md)). This file records what remains from the earlier inventory.

| Decision | Parts | Status |
| --- | --- | --- |
| Removed | Manual blueprint editor, preview/publish UI and write APIs; manual assignment create/edit forms and APIs; candidate accounts and history pages | Still gone. Tests assert the old routes return 404/405. |
| Replaced | "New interview scheduling is being updated" transition banners | Replaced by the Schedule interview form, assignment board and plan review. |
| Kept | `InterviewPlanContent`, published versions, assignments, invitation sessions, migrations, Pipecat execution, corrections/memory, snapshot and admin-only PDFs | Plan versions are now produced by `plans/preparation.py`; historical frozen plans remain executable. |
| Removed (structural cleanup) | Fixed-question practice controller branches, the practice decision/feedback loop in `session.py`, and the practice prompts `decision`, `feedback`, `early_feedback` | Gone: starting an attempt requires an assigned plan, so nothing could reach them. `opening_ack` is live and stays. Account-owned historical records are still readable. Git history keeps the old code. |
| Dead | Email-verification link path (`/verify`), `create_attempt` for account-owned records (tests only) | Unused by the product. |

No migration drops data; `0006_scheduled_preparation` is additive.

## Structural cleanup

The code was reorganised into capability packages (see [ARCHITECTURE.md](ARCHITECTURE.md)): `server.py` became a composition package with one router per capability and its business rules moved into `scheduling`, `invitations` and `records`; the former `assigned.py` split into `plans/schema.py` and `invitations/access.py`; the ORM and Python names follow the vocabulary *interview plan, assignment, attempt, candidate session, conversation record* while tables, columns, routes and stored values keep their original names; the unreachable practice path was removed from `session.py` and the controller; the frontend is organised by audience. Remaining structural debt is listed at the end of ARCHITECTURE.md. This cleanup changes no product behaviour and does not establish production readiness.
