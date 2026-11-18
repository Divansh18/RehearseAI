"""Deterministic recognition of conversational controls in an assigned interview.

A control is something the candidate says *about* the interview rather than as an answer: ending
it, asking for a repeat, asking to go back, asking for feedback, or saying they cannot answer.
Clear controls are recognised here, clause by clause, with explicit priority, so they never depend
on a model's mood. The model is still used for ambiguity (what to ask next, off-topic chat).

Everything is conservative on purpose: a clause must match as a whole, negations are preserved
("don't end the interview" is not a request to end), and "I don't know if you heard me" is not
a statement that the candidate does not know the answer.
"""

import re
from dataclasses import dataclass

# --- ending ---------------------------------------------------------------------------------

END_CLAUSE = re.compile(
    r"(?:please\s+)?(?:"
    r"(?:can|could|may|would)\s+(?:i|we|you)\s+"
    r"|(?:i|we)\s+(?:want|would like|need)\s+(?:you\s+)?to\s+"
    r"|(?:i'd|we'd)\s+like\s+to\s+"
    r"|want\s+you\s+to\s+"
    r"|let's\s+"
    r")?"
    r"(?:end|stop|finish|quit|exit|wrap up)"
    r"(?:\s+(?:(?:the|this|our)\s+)?(?:interview|session|practice)"
    r"|\s+(?:now|here|it))?"
    r"(?:\s+right\s+now|\s+please)?",
    re.I,
)
WITHDRAW_CLAUSE = re.compile(
    r"(?:i|we)\s+(?:don't|do not|dont)\s+want\s+to\s+"
    r"(?:proceed|continue|go on|carry on|keep going)"
    r"(?:\s+(?:further|anymore|now|with (?:this|the) (?:interview|session|practice)))?",
    re.I,
)
# "Done with the interview" is a clear request; a bare "I'm done" or "that's all" is not: it can
# just as well mean "I have finished answering".
_DONE_WITH = re.compile(
    r"(?:i'm|i am|we're|we are|i'm all|i am all)\s+(?:done|finished|through)\s+with\s+"
    r"(?:the|this|our)\s+(?:interview|session)(?:\s+(?:now|here|today|for\s+today))?",
    re.I,
)
_DONE_BARE = re.compile(
    r"(?:i'm|i am|we're|we are|i think i'm|i think we're)\s+"
    r"(?:all\s+)?(?:done|finished)(?:\s+now)?",
    re.I,
)

# --- repeating ------------------------------------------------------------------------------

_LEAD = (
    r"(?:(?:sorry|pardon|excuse me|okay|ok|yes|yeah|so|um|uh|well|actually),?\s+)*(?:please\s+)?"
)
_MODAL = r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
_REPEAT_CORE = (
    r"(?:repeat(?:\s+(?:that|it|this|that\s+question|what\s+you\s+said"
    r"|the\s+(?:last\s+|same\s+|whole\s+|original\s+)?question))?"
    r"|say\s+(?:that|it|the\s+question)(?:\s+again)?"
    r"|(?:what|what's|what\s+was|what\s+is)\s+(?:the\s+question|that|this)"
    r"|come\s+again)"
)
REPEAT_CLAUSE = re.compile(
    rf"{_LEAD}{_MODAL}{_REPEAT_CORE}(?:\s+(?:again|one more time|please|for me|once more))*",
    re.I,
)

# --- going back or restarting ---------------------------------------------------------------

# Going back or starting over is recognised only when it is framed as a request ("can we", "let's",
# "I want to", or an imperative), so "we restart the job again" inside an answer is left alone.
_ASK = (
    r"(?:(?:no|so|okay|ok|well|yeah|actually)\s+)*(?:i'?m\s+saying\s+)?(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+(?:we|you|i)\s+|let's\s+|i\s+(?:want|would\s+like|need)\s+to\s+)"
)
_RESTART = re.compile(
    rf"(?:{_ASK})?(?:please\s+)?(?:re)?start\s+(?:the\s+|this\s+|our\s+)?(?:whole\s+|entire\s+)?"
    r"(?:interview|session)(?:\s+(?:over|again|from\s+(?:the\s+)?(?:beginning|start|top)))?\b.*"
    rf"|{_ASK}(?:re)?start\s+(?:over|again)\b.*"
    rf"|{_ASK}(?:go|start|begin)\s+(?:back\s+)?(?:to|from|with)\s+(?:the\s+)?(?:very\s+)?"
    r"(?:start|starting|beginning|top)\b.*"
    rf"|{_ASK}(?:start|begin)\s+(?:the\s+)?(?:interview|session)\s+(?:again|over)\b.*",
    re.I,
)
_GO_BACK = re.compile(
    rf"{_ASK}(?:go|roll|get|move|jump)\s+(?:back|backwards?)\b.*"
    r"|(?:please\s+)?(?:go|roll|get|move|jump)\s+back\s+to\s+(?:the\s+)?(?:\w+\s+)?question\b.*"
    rf"|{_ASK}(?:answer|redo|revisit|go\s+over)\s+(?:the\s+)?(?:(?:first|previous|last|earlier)\s+)?"
    r"question\s+again\b.*",
    re.I,
)

# --- feedback and the interview process -----------------------------------------------------

FEEDBACK_REQUEST = re.compile(
    r"\b(?:can|could|may|would|please|give|share|want|like|tell)\b.{0,80}\bfeedback\b"
    r"|\bfeedback\b.{0,40}\?",
    re.I,
)
# Used by the assigned interview: a clause must be framed as a request, so "I like getting feedback
# from users" in an answer is not mistaken for a request for feedback on the interview.
_FEEDBACK_CLAUSE = re.compile(
    r"(?:(?:can|could|may|would|will)\s+(?:i|we|you)\b|please\b|give\b|share\b|tell\b|provide\b"
    r"|i\s+(?:want|would\s+like|need)\b|i'd\s+like\b|why\s+(?:can't|cannot|won't|don't)\s+you\b"
    r"|what\s+(?:is|was|about)\s+my\b).{0,80}\bfeedback\b.*",
    re.I,
)
# Process questions are recognised only as questions, never inside a statement.
_OUTCOME = re.compile(
    r"\b(?:who|which)\b.{0,25}\b(?:hiring|interviewing|reviewing|recruiter|manager)\b"
    r"|\b(?:when|how soon)\b.{0,25}\b(?:hear|get back|know|receive|contact)\b"
    r"|\bnext steps?\b|\bwhat happens next\b|\bdid i (?:pass|do|get)\b|\bhow did i do\b"
    r"|\b(?:am i|will i be|will i get)\b.{0,20}\b(?:hired|selected|chosen|an offer|the job)\b",
    re.I,
)
_PROGRESS = re.compile(
    r"\bhow (?:many|much)\b.{0,30}\b(?:questions?|left|remaining|more|longer|time)\b"
    r"|\bhow long\b.{0,30}\b(?:is|will|does|left|more)\b"
    r"|\bwhich question (?:are we|is this)\b|\bwhat question (?:number|are we)\b",
    re.I,
)

# --- not knowing, not having used, declining, not understanding -----------------------------

_FILL = r"(?:(?:actually|okay|ok|well|so|yeah|yes|no|um|uh|hmm|honestly|sorry|and)[,]?\s+)*"
_NOT_INTERROGATIVE = r"(?!\s+(?:if|whether|how|why|what|where|when|who|which)\b)"
_DONT_KNOW = re.compile(
    rf"{_FILL}(?:i|we)\s+(?:really\s+|honestly\s+|just\s+)?(?:do\s+not|don't|dont|can't|cannot)\s+"
    rf"(?:really\s+)?(?:know|remember|recall){_NOT_INTERROGATIVE}"
    r"(?:\s+(?P<subject>[\w'+#.\- ]{1,50}))?"
    rf"|{_FILL}(?:i\s+(?:have|got)\s+)?no\s+idea(?:\s+(?:about|on)\s+(?P<s2>[\w'+#.\- ]{{1,50}}))?",
    re.I,
)
_NOT_USED = re.compile(
    rf"{_FILL}(?:i|we)(?:'ve|\s+have)?\s+(?:never|not|haven't|havent|hadn't|didn't|didnt|did\s+not|"
    r"have\s+not)\s+(?:really\s+|actually\s+|personally\s+|ever\s+|yet\s+)?"
    r"(?:used|worked\s+(?:with|on|in)|tried|touched|done|had\s+(?:any\s+)?(?:experience|exposure)|"
    r"come\s+across|heard\s+of|learned|learnt|built|implemented)\b(?P<subject>.*)"
    rf"|{_FILL}(?:i|we)\s+(?:have|got)\s+no\s+(?:real\s+|practical\s+|hands[- ]on\s+)?"
    r"(?:experience|exposure|knowledge)\b(?P<s2>.*)"
    rf"|{_FILL}(?:i'm|i am|we're|we are)\s+not\s+(?:really\s+)?(?:familiar|experienced)\b(?P<s3>.*)"
    rf"|{_FILL}no\s+(?:real\s+)?experience\b(?P<s4>.*)",
    re.I,
)
_REFUSE = re.compile(
    rf"{_FILL}(?:i|we)(?:\s+would|'d)\s+(?:rather|prefer)\s+not\b.*"
    rf"|{_FILL}(?:i|we)\s+(?:don't|do\s+not|dont)\s+want\s+to\s+(?:answer|say|talk\s+about|discuss)\b.*"
    rf"|{_FILL}(?:i(?:'ll|\s+will)\s+)?pass(?:\s+on\s+(?:this|that|it))?"
    r"|prefer\s+not\s+to\s+(?:answer|say)\b.*|no\s+comment",
    re.I,
)
_NOT_UNDERSTOOD = re.compile(
    rf"{_FILL}(?:i|we)\s+(?:don't|do\s+not|dont|didn't|did\s+not)\s+(?:really\s+)?"
    r"(?:understand|follow|get\s+(?:it|that|the\s+question|what\s+you))\b.*"
    r"|(?:i|we)\s+(?:don't|do\s+not|dont)\s+know\s+what\s+(?:you\s+mean|that\s+means|you're\s+asking)"
    r"|(?:what|which)\s+do\s+you\s+mean|what\s+does\s+that\s+mean"
    r"|(?:i'm|i\s+am)\s+(?:a\s+bit\s+|so\s+|very\s+)?(?:confused|lost)"
    r"|(?:can|could)\s+you\s+(?:please\s+)?(?:rephrase|clarify|explain)\b.*",
    re.I,
)

# Priority when one turn holds several controls: ending beats everything, then going back, then
# repeating, then the rest. Lower number wins.
_PRIORITY = {
    "finish": 0,
    "finish_unclear": 1,
    "restart": 2,
    "back": 2,
    "repeat": 3,
    "not_understood": 4,
    "feedback": 5,
    "outcome": 6,
    "progress": 6,
    "gap": 7,
}


@dataclass(frozen=True)
class Intent:
    kind: str = "none"
    gap: str = ""  # dont_know | not_used | refuse
    subject: str = ""
    residual: str = ""  # words in the turn that are not part of a control clause

    @property
    def is_control(self) -> bool:
        return self.kind != "none"


_LEADING_FILLER = re.compile(
    r"^(?:(?:actually|okay|ok|well|so|yeah|yes|um|uh|hmm|oh|honestly|and)\s+)+", re.I
)
_WH = re.compile(r"^(?:who|which|when|how|what|where|did|will|am|is|are|do|does)\b", re.I)


def _clauses(text: str) -> list[tuple[str, bool]]:
    """Clauses with whether the sentence they came from was a question."""
    normalized = re.sub(r"\bwanna\b", "want to", text.replace("’", "'"), flags=re.I)
    clauses: list[tuple[str, bool]] = []
    for sentence in re.finditer(r"[^.!?]+[.!?]*", normalized):
        body, question = sentence.group(0), "?" in sentence.group(0)
        for part in re.split(r"\b(?:but|because)\b|;", body, flags=re.I):
            part = " ".join(re.sub(r"[,:.!?]", " ", part).split())
            part = _LEADING_FILLER.sub("", part)
            if part:
                clauses.append((part, question or bool(_WH.match(part))))
    return clauses


def _classify(clause: str, question: bool) -> tuple[str, str, str] | None:
    """Kind, gap type and subject for one clause, or None when it is ordinary speech."""
    if (
        END_CLAUSE.fullmatch(clause)
        or WITHDRAW_CLAUSE.fullmatch(clause)
        or _DONE_WITH.fullmatch(clause)
    ):
        return "finish", "", ""
    if _DONE_BARE.fullmatch(clause):
        return "finish_unclear", "", ""
    if _RESTART.fullmatch(clause):
        return "restart", "", ""
    if _GO_BACK.fullmatch(clause):
        return "back", "", ""
    if REPEAT_CLAUSE.fullmatch(clause):
        return "repeat", "", ""
    if _NOT_UNDERSTOOD.fullmatch(clause):
        return "not_understood", "", ""
    if _FEEDBACK_CLAUSE.fullmatch(clause):
        return "feedback", "", ""
    if question and _OUTCOME.search(clause):
        return "outcome", "", ""
    if question and _PROGRESS.search(clause):
        return "progress", "", ""
    for gap, pattern in (("not_used", _NOT_USED), ("dont_know", _DONT_KNOW), ("refuse", _REFUSE)):
        match = pattern.fullmatch(clause)
        if match:
            subject = next((v for k, v in match.groupdict().items() if v and v.strip()), "")
            subject = re.sub(
                r"^(?:with|in|of|on|about|using)\s+", "", " ".join(subject.split()), flags=re.I
            )
            return "gap", gap, subject
    return None


def interpret(text: str) -> Intent:
    """The highest-priority control in a turn, with whatever speech is left over."""
    found: list[tuple[int, str, str, str]] = []
    residual: list[str] = []
    for clause, question in _clauses(text):
        result = _classify(clause, question)
        if result is None:
            residual.append(clause)
        else:
            kind, gap, subject = result
            found.append((_PRIORITY[kind], kind, gap, subject))
    left = " ".join(residual)
    if not found:
        return Intent(residual=left)
    _, kind, gap, subject = min(found, key=lambda item: item[0])
    if kind == "finish_unclear" and len(left.split()) >= 3:
        # "...and that's how it worked. I'm done." is an answer with a sign-off, not a request.
        return Intent(residual=left)
    return Intent(kind=kind, gap=gap, subject=subject, residual=left)
