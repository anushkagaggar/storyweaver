import logging
import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audio_samples import hiss, silence, tone
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.routing.stt import RoutedSTT
from storyweaver.speech.audio_utils import MAX_AUDIO_BYTES, NormalizedAudio
from storyweaver.speech.stt import STTError, STTUnavailableError, Transcript

pytestmark = pytest.mark.unit

URL = "/v1/speech/transcribe"
SPOKEN = "a little owl who is afraid of the dark"


class FakeSTT:
    def __init__(self, text: str = SPOKEN, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.durations: list[float] = []

    async def transcribe(self, audio: NormalizedAudio) -> Transcript:
        self.durations.append(audio.duration_seconds)
        if self.error is not None:
            raise self.error
        return Transcript(text=self.text, engine="groq", model="whisper-large-v3-turbo", latency_ms=5.0)


@pytest.fixture
def stt(client: TestClient) -> FakeSTT:
    fake = FakeSTT()
    client.app.state.services.stt = fake  # type: ignore[attr-defined]
    return fake


def _post(client: TestClient, body: bytes, headers: dict[str, str], content_type: str = "audio/wav") -> Any:
    return client.post(URL, content=body, headers={**headers, "Content-Type": content_type})


def test_app_builds_groq_with_local_fallback(client: TestClient) -> None:
    services = client.app.state.services  # type: ignore[attr-defined]
    assert isinstance(services.stt, RoutedSTT)  # works without a database


def test_transcribes_a_recording(client: TestClient, auth_headers: dict[str, str], stt: FakeSTT) -> None:
    response = _post(client, tone(2.0), auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "text": SPOKEN,
        "engine": "groq",
        "model": "whisper-large-v3-turbo",
        "duration_seconds": pytest.approx(2.0, abs=0.05),
    }
    assert stt.durations == [pytest.approx(2.0, abs=0.05)]


def test_content_type_parameters_are_accepted(
    client: TestClient, auth_headers: dict[str, str], stt: FakeSTT
) -> None:
    assert _post(client, tone(1.0), auth_headers, "audio/wav; charset=binary").status_code == 200


def test_needs_the_api_key(client: TestClient, stt: FakeSTT) -> None:
    response = client.post(URL, content=tone(1.0), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 401
    assert stt.durations == []


@pytest.mark.parametrize(
    "content_type", ["application/json", "text/plain", "video/mp4", "multipart/form-data", ""]
)
def test_wrong_content_type_is_415(
    client: TestClient, auth_headers: dict[str, str], stt: FakeSTT, content_type: str
) -> None:
    response = _post(client, tone(1.0), auth_headers, content_type)
    assert response.status_code == 415
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "unsupported_media_type"


def test_over_10_mb_is_413(client: TestClient, auth_headers: dict[str, str], stt: FakeSTT) -> None:
    response = _post(client, b"\0" * (MAX_AUDIO_BYTES + 1), auth_headers)
    assert response.status_code == 413
    assert response.json()["code"] == "audio_too_large"
    assert stt.durations == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"", "audio_empty"),
        (b"this is not audio", "audio_unreadable"),
        (silence(3.0), "audio_silent"),
        (hiss(3.0), "audio_silent"),
        (tone(61.0, rate=8_000, channels=1), "audio_too_long"),
        (tone(120.0, rate=8_000, channels=1), "audio_too_long"),
    ],
    ids=["empty", "garbage", "silence", "hiss", "61s", "2min"],
)
def test_bad_recordings_are_422_and_never_reach_stt(
    client: TestClient, auth_headers: dict[str, str], stt: FakeSTT, body: bytes, code: str
) -> None:
    response = _post(client, body, auth_headers)
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert response.json()["detail"]  # a sentence the parent can act on
    assert stt.durations == []


def test_no_words_heard_is_422(client: TestClient, auth_headers: dict[str, str], stt: FakeSTT) -> None:
    stt.text = ""
    response = _post(client, tone(2.0), auth_headers)
    assert response.status_code == 422
    assert response.json()["code"] == "no_speech"


def test_quota_exhausted_is_429_with_retry_after(
    client: TestClient, auth_headers: dict[str, str], stt: FakeSTT
) -> None:
    stt.error = QuotaExhaustedError(retry_after_seconds=42)
    response = _post(client, tone(2.0), auth_headers)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    assert response.json()["code"] == "stt_busy"

    stt.error = QuotaExhaustedError(retry_after_seconds=30_000)  # Whisper's daily audio seconds
    assert _post(client, tone(2.0), auth_headers).json()["code"] == "stt_daily_limit"


@pytest.mark.parametrize("error", [STTUnavailableError("down"), STTError("rejected")])
def test_stt_failure_is_503(
    client: TestClient, auth_headers: dict[str, str], stt: FakeSTT, error: Exception
) -> None:
    stt.error = error
    response = _post(client, tone(2.0), auth_headers)
    assert response.status_code == 503
    assert response.json()["code"] == "stt_unavailable"


def test_stt_not_configured_is_503(client: TestClient, auth_headers: dict[str, str]) -> None:
    client.app.state.services.stt = None  # type: ignore[attr-defined]
    response = _post(client, tone(2.0), auth_headers)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


def test_audio_never_touches_disk_and_nothing_private_is_logged(
    client: TestClient,
    auth_headers: dict[str, str],
    stt: FakeSTT,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_disk(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("audio must not be written to a temporary file")

    for name in ("SpooledTemporaryFile", "TemporaryFile", "NamedTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, no_disk)
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", no_disk)
    big = tone(50.0, rate=48_000, channels=2)  # 9.6 MB: Starlette multipart would spool this to disk
    assert 1024 * 1024 < len(big) <= MAX_AUDIO_BYTES

    with caplog.at_level(logging.DEBUG):
        response = _post(client, big, auth_headers)
    assert response.status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SPOKEN not in logged
    assert "RIFF" not in logged
    assert "Transcribed" in logged  # metadata is still logged
