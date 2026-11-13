import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
import respx
from groq import AsyncGroq

from audio_samples import wav_bytes
from storyweaver.adapters.groq_orpheus import GroqOrpheus
from storyweaver.adapters.kokoro_tts import Kokoro, ModelFile, ensure_file
from storyweaver.speech.tts import TTS_SAMPLE_RATE, TTSError, TTSRateLimitedError, TTSUnavailableError

pytestmark = pytest.mark.unit

URL = "https://api.groq.com/openai/v1/audio/speech"
DUMMY_KEY = "gsk_test"  # pragma: allowlist secret
MODEL = "canopylabs/orpheus-v1-english"


def _orpheus() -> GroqOrpheus:
    return GroqOrpheus(AsyncGroq(api_key=DUMMY_KEY, max_retries=0), model=MODEL)


def _wav(seconds: float, rate: int = 24_000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return wav_bytes(0.3 * np.sin(2 * np.pi * 220 * t), rate=rate)


# --- GroqOrpheus --------------------------------------------------------------------------------------


@respx.mock
async def test_orpheus_sends_text_and_voice_and_returns_24k_samples() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, content=_wav(1.0), headers={"content-type": "audio/wav"})
    )
    speech = await _orpheus().synthesize("Goodnight, little fox.", "hannah")
    assert speech.sample_rate == TTS_SAMPLE_RATE
    assert speech.duration_seconds == pytest.approx(1.0, abs=0.02)
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "model": MODEL,
        "input": "Goodnight, little fox.",
        "voice": "hannah",
        "response_format": "wav",
    }


@respx.mock
async def test_orpheus_resamples_other_rates() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, content=_wav(0.5, rate=48_000)))
    speech = await _orpheus().synthesize("Hi.", "troy")
    assert len(speech.samples) == pytest.approx(0.5 * TTS_SAMPLE_RATE, abs=50)


async def test_orpheus_refuses_more_than_200_characters_without_calling_groq() -> None:
    with pytest.raises(TTSError, match="200"):
        await _orpheus().synthesize("x" * 201, "hannah")


@respx.mock
async def test_orpheus_429_carries_retry_after() -> None:
    respx.post(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "9"}, json={"error": {}}))
    with pytest.raises(TTSRateLimitedError) as caught:
        await _orpheus().synthesize("Hi.", "hannah")
    assert caught.value.retry_after_seconds == 9


@pytest.mark.parametrize("status", [404, 500, 503])
@respx.mock
async def test_orpheus_withdrawn_or_down_is_unavailable(status: int) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(TTSUnavailableError):
        await _orpheus().synthesize("Hi.", "hannah")


@respx.mock
async def test_orpheus_terms_not_accepted_is_a_loud_error() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "model_terms_required", "message": "accept terms"}}
        )
    )
    with pytest.raises(TTSError, match="model_terms_required") as caught:
        await _orpheus().synthesize("Hi.", "hannah")
    assert not isinstance(caught.value, TTSUnavailableError)


@respx.mock
async def test_orpheus_garbage_audio_is_unavailable() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, content=b"not a wav"))
    with pytest.raises(TTSUnavailableError, match="unreadable"):
        await _orpheus().synthesize("Hi.", "hannah")


# --- Kokoro (fake model; the real one is a ~350 MB download, see tests/live) ---------------------------


class FakeKokoroOnnx:
    def __init__(self, rate: int = TTS_SAMPLE_RATE) -> None:
        self.rate = rate
        self.calls: list[dict[str, Any]] = []

    def create(self, text: str, **options: Any) -> tuple[np.ndarray, int]:
        self.calls.append({"text": text, **options})
        return np.zeros(int(0.4 * self.rate), dtype=np.float32), self.rate


async def test_kokoro_passes_voice_and_accent() -> None:
    fake = FakeKokoroOnnx()
    kokoro = Kokoro(directory=Path("unused"), model_factory=lambda: fake)
    speech = await kokoro.synthesize("Cheerio!", "bm_george")
    assert speech.duration_seconds == pytest.approx(0.4)
    assert fake.calls == [{"text": "Cheerio!", "voice": "bm_george", "speed": 1.0, "lang": "en-gb"}]


async def test_kokoro_unknown_voice_is_a_key_error() -> None:
    kokoro = Kokoro(directory=Path("unused"), model_factory=FakeKokoroOnnx)
    with pytest.raises(KeyError):
        await kokoro.synthesize("Hi.", "hannah")


async def test_kokoro_wrong_sample_rate_is_unavailable() -> None:
    kokoro = Kokoro(directory=Path("unused"), model_factory=lambda: FakeKokoroOnnx(rate=22_050))
    with pytest.raises(TTSUnavailableError, match="22050"):
        await kokoro.synthesize("Hi.", "af_heart")


async def test_kokoro_load_failure_is_unavailable() -> None:
    def broken() -> Any:
        raise OSError("disk full")

    with pytest.raises(TTSUnavailableError, match="OSError"):
        await Kokoro(directory=Path("unused"), model_factory=broken).synthesize("Hi.", "af_heart")


async def test_kokoro_synthesis_failure_is_unavailable() -> None:
    crashing = SimpleNamespace(create=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("onnx")))
    with pytest.raises(TTSUnavailableError, match="RuntimeError"):
        await Kokoro(directory=Path("unused"), model_factory=lambda: crashing).synthesize("Hi.", "af_heart")


def test_kokoro_rejects_an_unknown_variant() -> None:
    with pytest.raises(ValueError, match="KOKORO_VARIANT"):
        Kokoro(directory=Path("unused"), variant="int4")


# --- model download -----------------------------------------------------------------------------------

BASE = "https://example.test/models"
PAYLOAD = b"model bytes" * 1000
SPEC = ModelFile("model.onnx", hashlib.sha256(PAYLOAD).hexdigest())


@respx.mock
def test_download_verifies_and_caches(tmp_path: Path) -> None:
    route = respx.get(f"{BASE}/model.onnx").mock(return_value=httpx.Response(200, content=PAYLOAD))
    path = ensure_file(tmp_path, SPEC, base_url=BASE)
    assert path.read_bytes() == PAYLOAD
    ensure_file(tmp_path, SPEC, base_url=BASE)  # cached: no second download
    assert route.call_count == 1


@respx.mock
def test_download_with_a_wrong_digest_is_rejected_and_removed(tmp_path: Path) -> None:
    respx.get(f"{BASE}/model.onnx").mock(return_value=httpx.Response(200, content=b"tampered"))
    with pytest.raises(TTSUnavailableError, match="SHA-256"):
        ensure_file(tmp_path, SPEC, base_url=BASE)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_a_corrupted_cached_file_is_downloaded_again(tmp_path: Path) -> None:
    (tmp_path / "model.onnx").write_bytes(b"truncated")
    route = respx.get(f"{BASE}/model.onnx").mock(return_value=httpx.Response(200, content=PAYLOAD))
    assert ensure_file(tmp_path, SPEC, base_url=BASE).read_bytes() == PAYLOAD
    assert route.call_count == 1
