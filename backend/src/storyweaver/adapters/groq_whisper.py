"""Groq Whisper implementation of STTEngine (`whisper-large-v3-turbo`, plan §2.1).

Audio is sent as an in-memory 16 kHz mono WAV and never stored or logged (plan §6.6; Groq Zero Data
Retention is on for the account).
"""

import logging
import time

import groq
from groq import AsyncGroq, omit

from storyweaver.adapters.groq_common import error_detail, retry_after_seconds
from storyweaver.speech.audio_utils import NormalizedAudio, to_wav_bytes
from storyweaver.speech.stt import STTError, STTRateLimitedError, STTUnavailableError, Transcript

logger = logging.getLogger(__name__)


class GroqWhisper:
    engine = "groq"

    def __init__(self, client: AsyncGroq, *, model: str, language: str | None = "en") -> None:
        self._client = client
        self.model = model
        self._language = language

    async def transcribe(self, audio: NormalizedAudio) -> Transcript:
        start = time.perf_counter()
        try:
            result = await self._client.audio.transcriptions.create(
                file=("speech.wav", to_wav_bytes(audio), "audio/wav"),
                model=self.model,
                language=self._language if self._language else omit,
                response_format="json",
                temperature=0.0,
            )
        except groq.RateLimitError as exc:
            raise STTRateLimitedError(retry_after_seconds(exc.response)) from exc
        except groq.NotFoundError as exc:
            # A withdrawn model must fail loudly (plan risk 1); the router then falls back.
            logger.error("Groq model %s not found (withdrawn or misnamed).", self.model)
            raise STTUnavailableError(f"Groq model {self.model} not found.") from exc
        except groq.APIStatusError as exc:
            if exc.status_code >= 500:
                raise STTUnavailableError(f"Groq returned HTTP {exc.status_code}.") from exc
            raise STTError(f"Groq rejected the audio (HTTP {exc.status_code}): {error_detail(exc)}") from exc
        except (groq.APIConnectionError, groq.APITimeoutError) as exc:
            raise STTUnavailableError("Could not reach Groq.") from exc
        return Transcript(
            text=result.text.strip(),
            engine=self.engine,
            model=self.model,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
