"""Speech -> picture (plan V3): what to draw, from words like "can you draw me a purple dragon, please?".

Deterministic on purpose: no LLM call, so a picture uses no planner tokens (the free tier's bottleneck).
The words have already passed the input safety checks; the drawing itself still goes through the
negative prompt, the safety checker and the picture self-check.
"""

import re

MIN_SUBJECT_CHARS = 3
MAX_SUBJECT_CHARS = 200  # leaves room in CharacterRow.visual_desc (300) and the title (80, cut below)
MAX_TITLE_CHARS = 80

# Polite openings and the request itself, in any order: "please can you draw me", "I want a picture of",
# "let's paint", "show me", "make a drawing of". Applied repeatedly from the start.
_LEADING = [
    r"(?:hey|hi|hello|ok(?:ay)?|um+|uh+|so|now)\b",
    r"please\b",
    r"(?:can|could|will|would)\s+(?:you|we)\b",
    r"(?:i\s+(?:want|would\s+like|'d\s+like|wanna)|i'd\s+like|let'?s|lets)\b(?:\s+to)?",
    r"(?:draw|paint|sketch|colou?r|make|create|show|give)\b(?:\s+(?:me|us|for\s+me|for\s+us))?",
    r"(?:an?|the|some)?\s*(?:picture|drawing|painting|image|illustration|sketch)s?\s+of\b",
]
_LEADING_RE = re.compile(r"^\s*(?:" + "|".join(_LEADING) + r")[\s,]*", re.IGNORECASE)
_SENTENCE_STARTS = {"a", "an", "the", "some", "one", "two", "three", "four", "five", "lots", "many", "my"}
_TRAILING_RE = re.compile(r"[\s,]*(?:\bplease\b|\bthanks?\b|\bthank\s+you\b|[.!?,;:])+\s*$", re.IGNORECASE)


def picture_subject(text: str) -> str | None:
    """The thing to draw ("a purple dragon"), or None when nothing usable is left."""
    subject = " ".join(text.split())
    while True:
        stripped = _TRAILING_RE.sub("", _LEADING_RE.sub("", subject, count=1))
        if stripped == subject:
            break
        subject = stripped
    if len(subject) < MIN_SUBJECT_CHARS or len(subject) > MAX_SUBJECT_CHARS:
        return None
    if not re.search(r"[A-Za-z]", subject):
        return None
    first, _, rest = subject.partition(" ")
    if first.lower() in _SENTENCE_STARTS:  # "A purple dragon" -> "a purple dragon"; names keep their case
        subject = f"{first.lower()} {rest}".strip()
    return subject


def picture_title(subject: str) -> str:
    """ "A purple dragon", cut on a word boundary to fit the title column."""
    title = subject[0].upper() + subject[1:]
    if len(title) <= MAX_TITLE_CHARS:
        return title
    return title[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def picture_caption(subject: str) -> str:
    """The words shown under the picture."""
    title = subject[0].upper() + subject[1:]
    return title if title.endswith((".", "!", "?")) else f"{title}."
