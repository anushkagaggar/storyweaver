"""Provider-neutral speech-to-text interface (plan §4.1): `GroqWhisper` in production, `FasterWhisper` as the
local fallback. Engines take already-normalised audio (speech/audio_utils.py) and never store it."""

from dataclasses import dataclass
from typing import Protocol

from storyweaver.speech.audio_utils import NormalizedAudio


@dataclass(frozen=True)
class Transcript:
    text: str
    engine: str  # "groq" or "local"
    model: str
    latency_ms: float


class STTError(Exception):
    """Transcription failed in a way a retry or fallback won't fix (e.g. the provider rejected it)."""


class STTRateLimitedError(STTError):
    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("Speech-to-text provider rate limit reached.")
        self.retry_after_seconds = retry_after_seconds


class STTUnavailableError(STTError):
    """Provider unreachable, 5xx, model withdrawn (404), or the local engine can't load."""


class STTEngine(Protocol):
    async def transcribe(self, audio: NormalizedAudio) -> Transcript:
        """Return the words spoken in `audio` (stripped; may be empty)."""
        ...
