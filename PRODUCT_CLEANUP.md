# Product cleanup inventory

The manual authoring flow was retired earlier and has now been replaced by automatic preparation behind one Schedule interview form (see [PRODUCT_WORKFLOW.md](PRODUCT_WORKFLOW.md)). This file records what remains from the earlier inventory.

| Decision | Parts | Status |
| --- | --- | --- |
| Removed | Manual blueprint editor, preview/publish UI and write APIs; manual assignment create/edit forms and APIs; candidate accounts and history pages | Still gone. Tests assert the old routes return 404/405. |
| Replaced | "New interview scheduling is being updated" transition banners | Replaced by the Schedule interview form, assignment board and plan review. |
| Kept | `BlueprintContent`, published versions, assignments, invitation sessions, migrations, Pipecat execution, corrections/memory, snapshot and admin-only PDFs | Plan versions are now produced by `preparation.py`; historical frozen plans remain executable. |
| Retained for compatibility | Fixed-question practice controller branches and practice prompts (`decision`, `feedback`, `early_feedback`, `opening_ack`); account-owned historical records | Not reachable from the web or `LiveSession`, which requires an assigned plan. They are kept only so historical tests and records keep working and should be retired in a separate, tested refactor. |
| Dead | Email-verification link path (`/verify`), `create_attempt` for account-owned records (tests only) | Unused by the product. |

No migration drops data; `0006_scheduled_preparation` is additive.
