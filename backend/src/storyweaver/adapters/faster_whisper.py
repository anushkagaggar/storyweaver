"""faster-whisper implementation of STTEngine: the local fallback, and the whole engine when
STT_ENGINE=local keeps voice on the Space (plan §6.6).

Model: `small` (Systran/faster-whisper-small, MIT), int8 on CPU. It is downloaded and loaded on first use,
not at startup, so it doesn't slow cold starts. Calls run one at a time in a thread so the event loop and
the Space's CPU aren't swamped.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from storyweaver.speech.audio_utils import NormalizedAudio
from storyweaver.speech.stt import STTUnavailableError, Transcript

logger = logging.getLogger(__name__)

ModelFactory = Callable[[], Any]


class FasterWhisper:
    engine = "local"

    def __init__(
        self,
        *,
        model_size: str = "small",
        compute_type: str = "int8",
        language: str | None = "en",
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.model = f"faster-whisper-{model_size}"
        self._model_size = model_size
        self._compute_type = compute_type
        self._language = language
        self._factory = model_factory or self._load_whisper_model
        self._loaded: Any | None = None
        self._lock = asyncio.Lock()

    def _load_whisper_model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTUnavailableError("faster-whisper is not installed.") from exc
        return WhisperModel(self._model_size, device="cpu", compute_type=self._compute_type)

    async def _model(self) -> Any:
        if self._loaded is None:
            async with self._lock:
                if self._loaded is None:
                    started = time.perf_counter()
                    try:
                        self._loaded = await asyncio.to_thread(self._factory)
                    except STTUnavailableError:
                        raise
                    except Exception as exc:  # download or load failure
                        raise STTUnavailableError(
                            f"Could not load {self.model}: {type(exc).__name__}"
                        ) from exc
                    logger.info("Loaded %s in %.1f s.", self.model, time.perf_counter() - started)
        return self._loaded

    async def transcribe(self, audio: NormalizedAudio) -> Transcript:
        model = await self._model()
        start = time.perf_counter()
        async with self._lock:  # one transcription at a time on the shared CPU
            try:
                text = await asyncio.to_thread(self._run, model, audio)
            except Exception as exc:
                raise STTUnavailableError(f"{self.model} failed: {type(exc).__name__}") from exc
        return Transcript(
            text=text, engine=self.engine, model=self.model, latency_ms=(time.perf_counter() - start) * 1000
        )

    def _run(self, model: Any, audio: NormalizedAudio) -> str:
        # `segments` is a lazy generator: it must be consumed here, inside the worker thread.
        segments, _info = model.transcribe(
            audio.samples,
            language=self._language,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
