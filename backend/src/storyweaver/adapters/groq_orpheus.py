"""Groq Orpheus implementation of TTSEngine (`canopylabs/orpheus-v1-english`, plan §2.1).

At most 200 characters per request, WAV only. Groq counts one token per input character (measured from
the rate-limit headers on 13 Nov 2026), so the free tier's 3,600 tokens a day is about one story.
"""

import logging

import groq
from groq import AsyncGroq

from storyweaver.adapters.groq_common import error_detail, retry_after_seconds
from storyweaver.speech.audio_utils import AudioError, decode_mono
from storyweaver.speech.tts import (
    ORPHEUS_MAX_CHARS,
    TTS_SAMPLE_RATE,
    EngineName,
    Speech,
    TTSError,
    TTSRateLimitedError,
    TTSUnavailableError,
)

logger = logging.getLogger(__name__)

MAX_CHUNK_SECONDS = 30  # 200 characters of speech is about 12 s; anything far longer is a bad response


class GroqOrpheus:
    name: EngineName = "orpheus"

    def __init__(self, client: AsyncGroq, *, model: str) -> None:
        self._client = client
        self.model = model

    async def synthesize(self, text: str, voice: str) -> Speech:
        if len(text) > ORPHEUS_MAX_CHARS:
            raise TTSError(f"Orpheus takes at most {ORPHEUS_MAX_CHARS} characters, got {len(text)}.")
        try:
            response = await self._client.audio.speech.create(
                model=self.model, input=text, voice=voice, response_format="wav"
            )
            data = await response.read()
        except groq.RateLimitError as exc:
            raise TTSRateLimitedError(retry_after_seconds(exc.response)) from exc
        except groq.NotFoundError as exc:
            logger.error("Groq model %s not found (withdrawn or misnamed).", self.model)
            raise TTSUnavailableError(f"Groq model {self.model} not found.") from exc
        except groq.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TTSUnavailableError(f"Groq returned HTTP {exc.status_code}.") from exc
            # e.g. model_terms_required (terms not accepted) or an unknown voice.
            raise TTSError(
                f"Groq rejected the request (HTTP {exc.status_code}): {error_detail(exc)}"
            ) from exc
        except (groq.APIConnectionError, groq.APITimeoutError) as exc:
            raise TTSUnavailableError("Could not reach Groq.") from exc
        try:
            samples = decode_mono(data, rate=TTS_SAMPLE_RATE, max_seconds=MAX_CHUNK_SECONDS)
        except AudioError as exc:
            raise TTSUnavailableError(f"Groq returned unreadable audio: {exc}") from exc
        return Speech(samples=samples, sample_rate=TTS_SAMPLE_RATE)
