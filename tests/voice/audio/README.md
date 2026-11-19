# Synthetic audio fixtures

Used only by `tests/test_interruption.py`. None of these contain a human voice or any recording of a
person: every file is 16 kHz mono 16-bit speech generated with the macOS `say` command (default
system voice).

| File | Text | Used as |
| --- | --- | --- |
| `stop.wav` | "Stop." (0.5 s) | short genuine interruption |
| `yes.wav` | "Yes." (0.4 s) | short genuine answer |
| `answer.wav` | a one-sentence project answer (3.7 s) | sustained genuine speech; its first 0.45 s is the "brief speech-like burst" |
| `interviewer.wav` | an interviewer question (3.6 s) | echo-like input when attenuated |

Regenerate with, for example, `say -o stop.wav --data-format=LEI16@16000 "Stop."`. The text of the
interviewer fixture is the only content that resembles an interview; it is invented.

Limits: synthetic claps, thumps, hiss and key clicks never trigger Silero VAD (checked), so they
cannot exercise this code; truncated speech stands in for speech-like noise. This is not evidence
about any real room or microphone.
