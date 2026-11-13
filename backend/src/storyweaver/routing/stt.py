"""Quota-aware speech-to-text: Groq Whisper while quota lasts, faster-whisper otherwise (plan §4.1, §6.3).

Groq limits Whisper by requests and by *audio seconds* (per hour and per day), and bills at least 10 s per
request, so each call reserves max(10, duration) audio-seconds before it is made.
"""

import logging
import math

from storyweaver.routing.router import ModelRouter, QuotaExhaustedError
from storyweaver.routing.store import Usage
from storyweaver.speech.audio_utils import NormalizedAudio
from storyweaver.speech.stt import (
    STTEngine,
    STTRateLimitedError,
    STTUnavailableError,
    Transcript,
)

logger = logging.getLogger(__name__)

GROQ_MIN_BILLED_SECONDS = 10
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
UNAVAILABLE_COOLDOWN = 30.0


def billed_audio_seconds(audio: NormalizedAudio) -> int:
    return max(GROQ_MIN_BILLED_SECONDS, math.ceil(audio.duration_seconds))


class RoutedSTT:
    """`primary` (Groq, metered by `router`) with `fallback` (local). Either may be None:
    STT_ENGINE=local has no primary; a missing faster-whisper install or STT_LOCAL_FALLBACK=false has no
    fallback."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        primary: STTEngine | None,
        primary_model: str,
        fallback: STTEngine | None,
    ) -> None:
        if primary is None and fallback is None:
            raise ValueError("RoutedSTT needs a primary or a fallback engine.")
        self._router = router
        self._primary = primary
        self._primary_model = primary_model
        self._fallback = fallback

    async def transcribe(self, audio: NormalizedAudio) -> Transcript:
        busy: QuotaExhaustedError | None = None
        if self._primary is not None:
            try:
                await self._router.acquire(
                    [self._primary_model], Usage(requests=1, audio_seconds=billed_audio_seconds(audio))
                )
            except QuotaExhaustedError as exc:
                logger.warning("STT quota low for %s; using the local fallback.", self._primary_model)
                busy = exc
            else:
                try:
                    return await self._primary.transcribe(audio)
                except STTRateLimitedError as exc:
                    await self._router.cooldown(
                        self._primary_model, exc.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN
                    )
                    logger.warning("Groq 429 on %s; using the local fallback.", self._primary_model)
                    busy = QuotaExhaustedError(exc.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN)
                except STTUnavailableError as exc:
                    await self._router.cooldown(self._primary_model, UNAVAILABLE_COOLDOWN)
                    logger.warning("%s unavailable (%s); using the local fallback.", self._primary_model, exc)

        if self._fallback is None:
            if busy is not None:
                raise busy  # the API turns this into 429 + Retry-After
            raise STTUnavailableError("Speech-to-text is unavailable and no fallback is configured.")
        return await self._fallback.transcribe(audio)
