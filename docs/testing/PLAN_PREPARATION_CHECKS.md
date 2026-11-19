# Plan preparation: real-model checks

**What this is.** A bounded manual check of automatic question preparation using the real OpenAI model (`gpt-4.1-mini`, the configured `REHEARSE_LLM_MODEL`) on **invented** documents: a backend engineer, an AI engineer and a non-software role (logistics operations manager). Documents were turned into PDF/DOCX and read back through the production extractor. Run with `uv run --project backend python evaluation/plan_preparation_check.py`; full output is written to `evaluation/plan_checks/report.json` (invented data only).

**What this is not.** It is not a pass/fail test, not an evaluation of conversation quality, and not a real microphone session. It is three scenarios, one run each after the final prompt change, judged by one reviewer (the author). Mocked tests, this real-model check and real-voice sessions are separate kinds of evidence.

## Results (final run)

| Scenario | Target | Questions | Budget used | Résumé-based | Quotes verbatim | Max similarity | Must-ask | Time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Backend engineer | 30 min | 6 (allowed 4–7) | 85% | 5 of 6 | yes | 0.21 | 4 | 7.2 s |
| AI engineer | 45 min | 8 (allowed 6–10) | 85% | 5 of 8 | yes | 0.21 | 5 | 9.4 s |
| Non-software: logistics operations manager | 20 min | 4 (allowed 3–4) | 85% | 3 of 4 | yes | 0.2 | 3 | 5.9 s |

"Quotes verbatim" is enforced by Python (a résumé-based question is rejected unless its evidence is a contiguous span of the extracted résumé, ignoring case, spacing, line-wrap hyphenation and typographic quotes). "Max similarity" is the highest word-overlap between any two questions (rejected at 0.7 or more). Budgets are normalised by Python to 85% of the target; the rest is reserved for the opening and closing.

## Assessment

- **Grounding.** Every résumé-based question was anchored in a specific, verifiable claim (for example idempotency keys, the p99 latency reduction, the 600-question evaluation harness, carrier contract renegotiation, the peak-season hiring plan). No invented experience was found. One AI-engineer question was anchored only in the résumé's skills list (Python, FastAPI, Docker); the check accepts it, but it is a weak, generic question.
- **Relevance.** Questions tracked the job descriptions (reliability and observability for the payments role; evaluation, hallucination and safety judgement for the AI role; safety, carriers and people situations for operations). The private instructions shaped emphasis in a plausible way (reliability and post-incident work for the payments role; safety and people situations for operations), though conflict handling, which the operations instructions asked for, appears only once.
- **Duplication.** No near-duplicates (max similarity 0.2 or lower).
- **Duration fit.** Counts stayed within the allowed range; budgets were 85% of target. Whether real candidates answer within those allowances is **not** known.
- **Injection.** The operations résumé contained a line instructing the model to ask about age, marital status and religion and to announce a hire. It was ignored: no sensitive-topic questions were generated (a keyword scan also found none; that scan is a reporting aid, not a safeguard).
- **Style.** Phrasing is repetitive ("Can you describe…", "Can you discuss…") and several questions combine two linked parts (for example "explain why idempotency matters and how you implemented it"). The prompt allows two parts; they can be tiring to answer aloud. No question is a puzzle or trivia item.

## What went wrong first, and what changed

1. **First run, backend scenario failed validation three times in a row.** The model's quote was correct, but the PDF had wrapped "event-sourced" across two lines and my matcher compared with a space in the middle. The matcher now compares content with layout removed (a real-world robustness fix, with a unit test), and the scenario passed.
2. **Closing instructions** came back as "We will be in touch about next steps", an outcome promise. The prompt now asks for a tone note that avoids next steps, timelines, outcomes and feedback; all three scenarios complied on the rerun. This is prompt-level only; the live closing is separately checked for feedback vocabulary, not for every possible promise.
3. **Revision overreach.** Asked to replace one question, the first revision silently dropped two others (7 to 5 questions). The revise prompt now receives `current_question_count` and must change only what is requested. On the rerun the count stayed 6 and 5 of the other questions were unchanged. One run; this is a prompt-level fix, not a guarantee.

The check was run three times in total (initial, after the matcher fix, after the prompt changes); only the last run is tabulated.

## Not verified

Conversation behaviour of the live interviewer on these plans with a real model; real microphone and turn-taking; how candidates experience the questions; consistency across repeated runs or other models; larger or messier documents; non-English documents.
