import numpy as np
import pytest

from storyweaver.routing.router import ModelRouter, QuotaExhaustedError
from storyweaver.routing.store import InMemoryQuotaStore
from storyweaver.routing.stt import RoutedSTT, billed_audio_seconds
from storyweaver.speech.audio_utils import SAMPLE_RATE, NormalizedAudio
from storyweaver.speech.stt import STTError, STTRateLimitedError, STTUnavailableError, Transcript

pytestmark = pytest.mark.unit

WHISPER = "whisper-large-v3-turbo"


def _audio(seconds: float) -> NormalizedAudio:
    return NormalizedAudio(samples=np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32))


class FakeEngine:
    def __init__(self, engine: str, *, error: Exception | None = None) -> None:
        self.engine = engine
        self.error = error
        self.calls = 0

    async def transcribe(self, audio: NormalizedAudio) -> Transcript:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Transcript(text=f"from {self.engine}", engine=self.engine, model=self.engine, latency_ms=1.0)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _routed(primary: FakeEngine | None, fallback: FakeEngine | None, clock: Clock | None = None) -> RoutedSTT:
    router = ModelRouter(InMemoryQuotaStore(), margin=1.0, clock=clock or Clock())
    return RoutedSTT(router=router, primary=primary, primary_model=WHISPER, fallback=fallback)


def test_groq_bills_at_least_ten_seconds() -> None:
    assert billed_audio_seconds(_audio(2.0)) == 10
    assert billed_audio_seconds(_audio(37.2)) == 38


async def test_uses_groq_while_quota_lasts() -> None:
    groq, local = FakeEngine("groq"), FakeEngine("local")
    result = await _routed(groq, local).transcribe(_audio(5))
    assert result.engine == "groq"
    assert (groq.calls, local.calls) == (1, 0)


async def test_audio_seconds_per_hour_run_out_then_falls_back() -> None:
    # 7,200 audio-seconds an hour at 60 s per call is 120 calls. One call every 4 s stays under 20 per minute.
    groq, local, clock = FakeEngine("groq"), FakeEngine("local"), Clock()
    stt = _routed(groq, local, clock)
    for _ in range(120):
        assert (await stt.transcribe(_audio(60))).engine == "groq"
        clock.now += 4
    assert (await stt.transcribe(_audio(60))).engine == "local"
    assert groq.calls == 120


async def test_requests_per_minute_run_out_then_falls_back() -> None:
    groq, local = FakeEngine("groq"), FakeEngine("local")
    stt = _routed(groq, local)
    engines = [(await stt.transcribe(_audio(2))).engine for _ in range(21)]
    assert engines == ["groq"] * 20 + ["local"]


async def test_groq_429_cools_down_and_falls_back() -> None:
    groq, local = FakeEngine("groq", error=STTRateLimitedError(12)), FakeEngine("local")
    stt = _routed(groq, local)
    assert (await stt.transcribe(_audio(3))).engine == "local"
    # While cooling down, Groq isn't tried again.
    assert (await stt.transcribe(_audio(3))).engine == "local"
    assert groq.calls == 1


async def test_groq_unavailable_falls_back() -> None:
    groq, local = FakeEngine("groq", error=STTUnavailableError("model withdrawn")), FakeEngine("local")
    assert (await _routed(groq, local).transcribe(_audio(3))).engine == "local"


async def test_a_rejected_request_fails_loudly_instead_of_falling_back() -> None:
    groq, local = FakeEngine("groq", error=STTError("bad request")), FakeEngine("local")
    with pytest.raises(STTError):
        await _routed(groq, local).transcribe(_audio(3))
    assert local.calls == 0


async def test_local_engine_only_never_calls_groq() -> None:
    local = FakeEngine("local")
    assert (await _routed(None, local).transcribe(_audio(3))).engine == "local"


async def test_no_fallback_and_no_quota_is_busy_with_retry_after() -> None:
    groq = FakeEngine("groq", error=STTRateLimitedError(25))
    with pytest.raises(QuotaExhaustedError) as caught:
        await _routed(groq, None).transcribe(_audio(3))
    assert caught.value.retry_after_seconds == 25


async def test_no_fallback_and_groq_down_is_unavailable() -> None:
    groq = FakeEngine("groq", error=STTUnavailableError("down"))
    with pytest.raises(STTUnavailableError):
        await _routed(groq, None).transcribe(_audio(3))


async def test_fallback_failure_surfaces() -> None:
    local = FakeEngine("local", error=STTUnavailableError("model download failed"))
    with pytest.raises(STTUnavailableError):
        await _routed(None, local).transcribe(_audio(3))


def test_needs_at_least_one_engine() -> None:
    with pytest.raises(ValueError, match="primary or a fallback"):
        _routed(None, None)
