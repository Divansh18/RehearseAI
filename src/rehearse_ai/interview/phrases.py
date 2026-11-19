"""How candidate speech is recognised: patterns and small classifiers for the interview rules.

Pure functions and compiled patterns only: no state, no I/O, no model calls. The session decides
what to *do* with a recognition; this module only says what the words are. Candidate speech is
untrusted text, so every pattern here is anchored and bounded.
"""

import re
from dataclasses import dataclass

from rehearse_ai.interview.controller import Readiness

# Short floor-control phrases ("go on", "yes please cut") are not answers.
FLOOR_CONTROL = re.compile(
    r"^(?:(?:yes|yeah|yep|okay|ok|sure|sorry|please|thanks|thank you|go on|go ahead|continue|"
    r"carry on|hold on|wait|one second|just a second|never mind|cut|cut in|cut it)[,.!?\s]*){1,5}$",
    re.I,
)

# A plain "yes" is only taken as agreement when it answers the end-of-interview clarification.
AFFIRM = re.compile(
    r"(?:yes|yeah|yep|sure|please do|yes please|end it|that's right|correct)[\s.!,]*", re.I
)

NAME_TOKEN = re.compile(r"[^\W\d_][^\W\d_'’.\-]*(?:['’.\-][^\W\d_]+)*")

REPEAT = re.compile(
    r"^(?:(?:sorry|pardon|excuse me),?\s+)?(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?"
    r"(?:repeat(?: that| the (?:question|last question))?"
    r"|say (?:that|the question) again)(?:\s+one more time)?\??[.!]?$",
    re.IGNORECASE,
)

SKIP = re.compile(r"^(?:please\s+)?(?:skip(?: this question)?|next question)\??[.!]?$", re.I)

CORRECTION = re.compile(
    r"^\s*(?:(?:actually|sorry)\s*[, ]\s*)?"
    r"(?P<cue>it's|it is|i said|i meant|i used|my name is)\s+"
    r"(?P<replacement>.+?)\s*,?\s+not\s+(?P<superseded>[^.!?,]+)"
    r"(?P<remainder>[.!?,].*)?$",
    re.I,
)


@dataclass(frozen=True)
class CorrectionRequest:
    cue: str
    replacement: str
    superseded: str
    remainder: str


def parse_correction(text: str) -> CorrectionRequest | None:
    clauses = re.split(r"(?<=[.!?])\s+", text.replace("’", "'").strip())
    for index, clause in enumerate(clauses):
        match = CORRECTION.fullmatch(clause)
        if match:
            remainder = [*clauses[:index]]
            remainder.extend(
                part for part in [(match["remainder"] or "").lstrip(".!?, ").strip()] if part
            )
            remainder.extend(clauses[index + 1 :])
            return CorrectionRequest(
                match["cue"].lower(),
                match["replacement"].strip(" ,"),
                match["superseded"].strip(),
                " ".join(remainder).strip(),
            )
    return None


DEFER_READINESS = re.compile(
    r"\b(?:not (?:ready|yet|now)|do not (?:start|begin)|do not want to"
    r"|give me (?:a |one )?(?:moment|minute|second)|need (?:a |one )?(?:moment|minute|second)"
    r"|hold on|wait|maybe later|not quite|later)\b"
)

AFFIRM_READINESS = re.compile(
    r"\b(?:i am ready(?: to (?:start|begin|go))?|i am good to go"
    r"|let us (?:get|go) into it|let us (?:start|begin|go|do it)"
    r"|we can (?:start|begin)|go ahead|ready to (?:start|begin|go))"
    r"(?: now| please)?$"
)

BARE_AFFIRM_READINESS = re.compile(
    r"(?:yes|yeah|yep|sure|okay|ok|absolutely|of course|sounds good|ready"
    r"|start|begin|let us go|let us do it)(?: now| please)?"
)

FINAL_START = re.compile(r"\b(?:start|begin)(?: now| please)?$")

NAME_SPELLING = re.compile(
    r"^\s*(?:(?:actually|sorry)[, ]+)?"
    r"(?P<cue>my name is spelled|i spell my name(?: as)?|"
    r"i spell it(?: as)?|(?:it's|it is) spelled|spelled(?: as)?)\s+"
    r"(?P<letters>.+?)\s*$",
    re.I,
)

NON_NAMES = {
    "a",
    "an",
    "bit",
    "can",
    "doing",
    "fine",
    "good",
    "great",
    "happy",
    "here",
    "hello",
    "hey",
    "hi",
    "okay",
    "ok",
    "ready",
    "really",
    "sorry",
    "tired",
    "well",
    "nervous",
    "no",
    "nah",
    "not",
    "so",
    "very",
    "yes",
    "yeah",
    "yep",
    "start",
    "begin",
    "sure",
    "spelled",
    "you",
}


def letters_as_name(text: str, *, allow_word: bool = False) -> str | None:
    parts = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    if len(parts) >= 2 and all(len(part) == 1 for part in parts):
        name = "".join(parts)
        return name[0].upper() + name[1:].lower() if len(name) <= 40 else None
    if allow_word and len(parts) == 1 and len(parts[0]) <= 40:
        word = parts[0]
        return word[0].upper() + word[1:]
    return None


def spoken_name_spelling(text: str, *, prompted: bool) -> tuple[str, str] | None:
    # Deepgram may punctuate individually spoken letters as separate sentences.
    joined = re.sub(r"\b([^\W\d_])\.\s+(?=[^\W\d_]\.)", r"\1 ", text)
    clauses = re.split(r"(?<=[.!?])\s+", joined.strip())
    for index, clause in enumerate(clauses):
        match = NAME_SPELLING.fullmatch(clause.strip(" .!?"))
        if match:
            cue = match["cue"].lower()
            if not prompted and "name" not in cue:
                continue
            name = letters_as_name(match["letters"], allow_word=True)
        elif prompted:
            name = letters_as_name(clause.strip(" .!?"))
        else:
            name = None
        if name and name.casefold() not in NON_NAMES:
            remainder = " ".join([*clauses[:index], *clauses[index + 1 :]]).strip()
            return name, remainder
    return None


def is_sentence_request(pattern: re.Pattern[str], text: str) -> bool:
    """Recognize a whole control clause even after a separate preamble."""
    return any(pattern.fullmatch(clause.strip()) for clause in re.split(r"(?<=[.!?])\s+", text))


def classify_readiness(text: str) -> Readiness:
    normalized = text.lower().replace("’", "'")
    for old, new in {"i'm": "i am", "let's": "let us", "don't": "do not"}.items():
        normalized = normalized.replace(old, new)
    words = re.findall(r"[a-z]+", normalized)
    words = [word for index, word in enumerate(words) if index == 0 or word != words[index - 1]]
    normalized = " ".join(words)
    if normalized in {"no", "nah", "nope"}:
        return Readiness.DEFERRED
    if DEFER_READINESS.search(normalized):
        return Readiness.DEFERRED
    if (
        AFFIRM_READINESS.search(normalized)
        or BARE_AFFIRM_READINESS.fullmatch(normalized)
        or FINAL_START.search(normalized)
    ):
        return Readiness.READY
    return Readiness.UNCLEAR


def classify_greeting_readiness(text: str) -> Readiness:
    classification = classify_readiness(text)
    if classification is not Readiness.UNCLEAR:
        return classification
    clauses = [classify_readiness(clause) for clause in re.split(r"[.!?]+", text)]
    if Readiness.DEFERRED in clauses:
        return Readiness.DEFERRED
    if Readiness.READY in clauses:
        return Readiness.READY
    return Readiness.UNCLEAR


def fallback_opening_ack(text: str) -> str:
    if re.search(r"\bhow (?:are|r|about) you\b|\bhow's it going\b", text, re.I):
        return "Doing well, thanks."
    return "Thanks for telling me."
