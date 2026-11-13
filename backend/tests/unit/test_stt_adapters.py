import asyncio
import io
import threading
import wave
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
import respx
from groq import AsyncGroq

from storyweaver.adapters.faster_whisper import FasterWhisper
from storyweaver.adapters.groq_whisper import GroqWhisper
from storyweaver.speech.audio_utils import SAMPLE_RATE, NormalizedAudio
from storyweaver.speech.stt import STTError, STTRateLimitedError, STTUnavailableError

pytestmark = pytest.mark.unit

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DUMMY_KEY = "gsk_test"  # pragma: allowlist secret
MODEL = "whisper-large-v3-turbo"


def _audio(seconds: float = 1.5) -> NormalizedAudio:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return NormalizedAudio(samples=(0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))


def _whisper() -> GroqWhisper:
    return GroqWhisper(AsyncGroq(api_key=DUMMY_KEY, max_retries=0), model=MODEL, language="en")


# --- GroqWhisper -------------------------------------------------------------------------------------------


@respx.mock
async def test_groq_sends_16k_mono_wav_and_returns_stripped_text() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={"text": "  A dragon who loves rain. "})
    )
    result = await _whisper().transcribe(_audio(1.5))
    assert (result.text, result.engine, result.model) == ("A dragon who loves rain.", "groq", MODEL)

    request = route.calls.last.request
    body = request.content
    assert b'name="model"' in body and MODEL.encode() in body
    assert b'name="language"' in body and b"\r\n\r\nen\r\n" in body
    # The uploaded file is a valid 16 kHz mono 16-bit WAV of the right length.
    start = body.index(b"RIFF")
    end = body.index(b"\r\n--", start)
    with wave.open(io.BytesIO(body[start:end])) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16_000, 2)
        assert wav.getnframes() == int(1.5 * SAMPLE_RATE)


@respx.mock
async def test_groq_429_carries_retry_after() -> None:
    respx.post(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "17"}, json={"error": {}}))
    with pytest.raises(STTRateLimitedError) as caught:
        await _whisper().transcribe(_audio())
    assert caught.value.retry_after_seconds == 17


@pytest.mark.parametrize("status", [404, 500, 503])
@respx.mock
async def test_groq_withdrawn_or_down_is_unavailable(status: int) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(STTUnavailableError):
        await _whisper().transcribe(_audio())


@respx.mock
async def test_groq_bad_request_is_an_error_with_groq_detail() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"code": "invalid_file", "message": "bad file"}})
    )
    with pytest.raises(STTError, match="invalid_file") as caught:
        await _whisper().transcribe(_audio())
    assert not isinstance(caught.value, STTUnavailableError)


@respx.mock
async def test_groq_unreachable_is_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(STTUnavailableError):
        await _whisper().transcribe(_audio())


# --- FasterWhisper (with a fake model: the real one is a 460 MB download, see tests/live) ----------------


class FakeWhisperModel:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[dict[str, Any]] = []
        self.threads: list[str] = []

    def transcribe(self, samples: np.ndarray, **options: Any) -> tuple[Any, Any]:
        self.calls.append({"samples": samples, **options})

        def segments() -> Any:
            self.threads.append(threading.current_thread().name)  # recorded when consumed
            for text in self.texts:
                yield SimpleNamespace(text=text)

        return segments(), SimpleNamespace(language="en")


async def test_local_joins_segments_and_passes_16k_samples() -> None:
    model = FakeWhisperModel([" Once upon a time,", " a fox. "])
    stt = FasterWhisper(model_factory=lambda: model, language="en")
    result = await stt.transcribe(_audio(2.0))
    assert (result.text, result.engine, result.model) == (
        "Once upon a time, a fox.",
        "local",
        "faster-whisper-small",
    )
    call = model.calls[0]
    assert call["samples"].dtype == np.float32 and len(call["samples"]) == 2 * SAMPLE_RATE
    assert call["language"] == "en" and call["vad_filter"] is True
    # The lazy segment generator is consumed off the event loop thread.
    assert model.threads and model.threads[0] != threading.main_thread().name


async def test_local_model_loads_once_even_under_concurrency() -> None:
    loads = 0

    def factory() -> FakeWhisperModel:
        nonlocal loads
        loads += 1
        return FakeWhisperModel(["hi"])

    stt = FasterWhisper(model_factory=factory)
    results = await asyncio.gather(*(stt.transcribe(_audio()) for _ in range(4)))
    assert loads == 1 and all(r.text == "hi" for r in results)


async def test_local_load_failure_is_unavailable() -> None:
    def factory() -> Any:
        raise OSError("download failed")

    with pytest.raises(STTUnavailableError, match="OSError"):
        await FasterWhisper(model_factory=factory).transcribe(_audio())


async def test_local_transcribe_failure_is_unavailable() -> None:
    class Broken:
        def transcribe(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("ctranslate2 crashed")

    with pytest.raises(STTUnavailableError, match="RuntimeError"):
        await FasterWhisper(model_factory=Broken).transcribe(_audio())
