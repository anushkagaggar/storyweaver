"""Provider-neutral text-to-speech (plan §4.1): `GroqOrpheus` within quota, `Kokoro` otherwise.

Engines synthesise one short chunk at a time and return 24 kHz mono float32 samples. Narration (several
voices, pauses, MP3 encoding) is assembled in orchestrator/narration.py.
"""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

EngineName = Literal["orpheus", "kokoro"]

TTS_SAMPLE_RATE = 24_000
ORPHEUS_MAX_CHARS = 200  # Groq's per-request limit for Orpheus

# After . ! ? or an ellipsis (…), past any closing quotes or brackets (” ’).  # noqa: RUF003
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"'”’)\]])\s+")  # noqa: RUF001
# After , ; : or before a spaced dash (em —, en –, or hyphen).  # noqa: RUF003
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+|\s+(?=[—–-]\s)")  # noqa: RUF001


@dataclass(frozen=True)
class Speech:
    samples: NDArray[np.float32]  # mono, [-1, 1]
    sample_rate: int

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / self.sample_rate


class TTSError(Exception):
    """Synthesis failed in a way a retry or fallback won't fix."""


class TTSRateLimitedError(TTSError):
    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("Text-to-speech provider rate limit reached.")
        self.retry_after_seconds = retry_after_seconds


class TTSUnavailableError(TTSError):
    """Provider unreachable, 5xx, model withdrawn, or the local engine can't load."""


class TTSEngine(Protocol):
    name: EngineName

    async def synthesize(self, text: str, voice: str) -> Speech:
        """Speak one chunk (at most ORPHEUS_MAX_CHARS characters) in `voice`."""
        ...


def split_for_tts(text: str, max_chars: int = ORPHEUS_MAX_CHARS) -> list[str]:
    """Split into chunks of at most `max_chars`, breaking at sentence ends where possible.

    Whole sentences are packed together (fewer requests matter: Orpheus allows 10 a minute). A sentence
    longer than `max_chars` is broken at commas or dashes, then between words.
    """
    text = " ".join(text.split())
    if not text:
        return []
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        for clause in _CLAUSE_END.split(sentence):
            pieces.extend(_split_words(clause, max_chars) if len(clause) > max_chars else [clause])

    chunks: list[str] = []
    for piece in (p.strip() for p in pieces):
        if not piece:
            continue
        if chunks and len(chunks[-1]) + 1 + len(piece) <= max_chars:
            chunks[-1] = f"{chunks[-1]} {piece}"
        else:
            chunks.append(piece)
    return chunks


def _split_words(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > max_chars:  # a pathological "word" (e.g. a long URL); never expected in a story
            if current:
                parts.append(current)
                current = ""
            parts.append(word[:max_chars])
            word = word[max_chars:]
        if current and len(current) + 1 + len(word) > max_chars:
            parts.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        parts.append(current)
    return parts
