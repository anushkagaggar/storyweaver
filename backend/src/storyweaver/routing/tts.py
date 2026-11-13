"""Quota-aware text-to-speech: the engine is chosen once per story (plan §6.3, §6.4).

Orpheus's free tier is about one story a day: Groq counts one token per input character, with 3,600 a day
and 1,200 a minute (9 and 1,080 with the 0.9 margin), and 10 requests a minute. So:

1. `choose()` books the *whole* story's daily Orpheus budget (requests and characters) in one atomic
   reservation, or picks Kokoro. A story never starts on Orpheus unless it can finish on Orpheus, so its
   voices stay consistent.
2. `synthesize()` then paces each Orpheus request against the per-minute limits, waiting when they're
   full (the daily budget is already booked). A whole story takes about two minutes on Orpheus.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from storyweaver.routing.router import ModelRouter, QuotaExhaustedError, daily_limits, short_window_limits
from storyweaver.routing.store import Usage
from storyweaver.speech.tts import (
    EngineName,
    Speech,
    TTSEngine,
    TTSRateLimitedError,
    TTSUnavailableError,
    split_for_tts,
)

logger = logging.getLogger(__name__)

TTSMode = Literal["auto", "orpheus", "kokoro"]
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
UNAVAILABLE_COOLDOWN = 30.0


def orpheus_usage(texts: Sequence[str]) -> Usage:
    """Requests and tokens (= characters) Orpheus needs to speak `texts`, chunked as synthesis will be."""
    chunks = [chunk for text in texts for chunk in split_for_tts(text)]
    return Usage(requests=len(chunks), tokens=sum(len(chunk) for chunk in chunks))


class TTSRouter:
    def __init__(
        self,
        *,
        router: ModelRouter,
        orpheus: TTSEngine | None,
        orpheus_model: str,
        kokoro: TTSEngine | None,
        mode: TTSMode = "auto",
        max_wait_seconds: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if orpheus is None and kokoro is None:
            raise ValueError("TTSRouter needs at least one engine.")
        self._router = router
        self._orpheus = orpheus
        self._orpheus_model = orpheus_model
        self._kokoro = kokoro
        self._mode = mode
        self._max_wait = max_wait_seconds
        self._sleep = sleep

    async def choose(self, texts: Sequence[str], *, require: EngineName | None = None) -> EngineName:
        """Pick the engine for a whole story (or one narration request). Books Orpheus's daily budget.

        `require` forces an engine (e.g. a request that named an Orpheus voice). Raises QuotaExhaustedError
        only when Orpheus is required (by `require`, TTS_ENGINE=orpheus, or no Kokoro) and has no budget.
        """
        if require == "kokoro":
            if self._kokoro is None:
                raise TTSUnavailableError("Kokoro is required but not configured.")
            return "kokoro"
        if require == "orpheus" and self._orpheus is None:
            raise TTSUnavailableError("Orpheus is required but not configured.")
        orpheus_only = require == "orpheus" or self._mode == "orpheus" or self._kokoro is None
        if require is None and (self._mode == "kokoro" or self._orpheus is None):
            if self._kokoro is None:
                raise TTSUnavailableError("Kokoro is required but not configured.")
            return "kokoro"
        try:
            await self._router.acquire([self._orpheus_model], orpheus_usage(texts), windows=daily_limits)
        except QuotaExhaustedError:
            if orpheus_only:
                raise
            logger.info("Orpheus can't cover this narration today; using Kokoro.")
            return "kokoro"
        return "orpheus"

    async def synthesize(self, engine: EngineName, text: str, voice: str) -> Speech:
        """Speak one chunk on `engine` (chosen by `choose`). Orpheus requests are paced per minute."""
        if engine == "kokoro":
            if self._kokoro is None:
                raise TTSUnavailableError("Kokoro is not configured.")
            return await self._kokoro.synthesize(text, voice)
        if self._orpheus is None:
            raise TTSUnavailableError("Orpheus is not configured.")
        await self._pace(Usage(requests=1, tokens=len(text)))
        try:
            return await self._orpheus.synthesize(text, voice)
        except TTSRateLimitedError as exc:
            await self._router.cooldown(
                self._orpheus_model, exc.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN
            )
            raise
        except TTSUnavailableError:
            await self._router.cooldown(self._orpheus_model, UNAVAILABLE_COOLDOWN)
            raise

    async def _pace(self, usage: Usage) -> None:
        waited = 0.0
        while True:
            try:
                await self._router.acquire([self._orpheus_model], usage, windows=short_window_limits)
                return
            except QuotaExhaustedError as exc:
                if waited + exc.retry_after_seconds > self._max_wait:
                    raise
                logger.info(
                    "Pacing Orpheus: waiting %.0f s for the per-minute limit.", exc.retry_after_seconds
                )
                await self._sleep(exc.retry_after_seconds)
                waited += exc.retry_after_seconds
