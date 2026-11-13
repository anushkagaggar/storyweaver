import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from storyweaver.orchestrator.narration import MAX_NARRATE_CHARS, Narrator
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.speech.tts import TTSError, TTSRateLimitedError, TTSUnavailableError
from tts_fakes import FakeTTS, make_router

pytestmark = pytest.mark.unit

URL = "/v1/speech/narrate"
PRIVATE_TEXT = "Mia whispered a secret to the moon"


class FailingNarrator:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def narrate_text(self, text: str, voice_id: str | None = None) -> Any:
        raise self.error


@pytest.fixture
def engines(client: TestClient) -> tuple[FakeTTS, FakeTTS]:
    orpheus, kokoro = FakeTTS("orpheus"), FakeTTS("kokoro")
    router, _ = make_router(orpheus, kokoro)
    client.app.state.services.narrator = Narrator(router)  # type: ignore[attr-defined]
    return orpheus, kokoro


def test_app_builds_a_narrator_without_a_database(client: TestClient) -> None:
    assert isinstance(client.app.state.services.narrator, Narrator)  # type: ignore[attr-defined]


def test_narrates_to_mp3(client: TestClient, auth_headers: dict[str, str], engines: Any) -> None:
    response = client.post(URL, json={"text": "Goodnight, little fox."}, headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-tts-engine"] == "orpheus"
    assert response.headers["x-tts-voice"] == "hannah"
    assert float(response.headers["x-audio-duration"]) > 0.5
    assert len(response.content) > 1000


def test_a_kokoro_voice_is_honoured(client: TestClient, auth_headers: dict[str, str], engines: Any) -> None:
    response = client.post(URL, json={"text": "Hello.", "voice": "bf_emma"}, headers=auth_headers)
    assert (response.headers["x-tts-engine"], response.headers["x-tts-voice"]) == ("kokoro", "bf_emma")


def test_needs_the_api_key(client: TestClient, engines: Any) -> None:
    assert client.post(URL, json={"text": "Hi."}).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * (MAX_NARRATE_CHARS + 1)},
        {},
        {"text": "Hi.", "voice": "nobody"},
    ],
    ids=["empty", "blank", "too-long", "missing", "unknown-voice"],
)
def test_bad_requests_are_422(
    client: TestClient, auth_headers: dict[str, str], engines: Any, body: dict[str, Any]
) -> None:
    response = client.post(URL, json=body, headers=auth_headers)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    ("error", "status", "retry_after"),
    [
        (QuotaExhaustedError(retry_after_seconds=33), 429, "33"),
        (TTSRateLimitedError(12), 429, "12"),
        (TTSUnavailableError("down"), 503, "30"),
    ],
)
def test_busy_or_down_engines(
    client: TestClient, auth_headers: dict[str, str], error: Exception, status: int, retry_after: str
) -> None:
    client.app.state.services.narrator = FailingNarrator(error)  # type: ignore[attr-defined]
    response = client.post(URL, json={"text": "Hi."}, headers=auth_headers)
    assert response.status_code == status
    assert response.headers["retry-after"] == retry_after


def test_a_rejected_request_is_503(client: TestClient, auth_headers: dict[str, str]) -> None:
    client.app.state.services.narrator = FailingNarrator(TTSError("model_terms_required"))  # type: ignore[attr-defined]
    assert client.post(URL, json={"text": "Hi."}, headers=auth_headers).status_code == 503


def test_text_is_never_logged(
    client: TestClient, auth_headers: dict[str, str], engines: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert client.post(URL, json={"text": PRIVATE_TEXT}, headers=auth_headers).status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Narrated" in logged and PRIVATE_TEXT not in logged
