import httpx
import pytest
import respx
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def _completion() -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1760000000,
        "model": "openai/gpt-oss-20b",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


@respx.mock
def test_groq_check_success(client: TestClient, auth_headers: dict[str, str]) -> None:
    route = respx.post(GROQ_CHAT_URL).mock(return_value=httpx.Response(200, json=_completion()))

    response = client.get("/v1/groq-check", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["total_tokens"] == 12
    assert route.calls.last.request.headers["authorization"] == "Bearer gsk_test"


@respx.mock
def test_groq_rate_limit_maps_to_429_with_retry_after(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(GROQ_CHAT_URL).mock(
        return_value=httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow"}})
    )

    response = client.get("/v1/groq-check", headers=auth_headers)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"


@respx.mock
def test_groq_auth_failure_maps_to_502_without_leaking_details(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(GROQ_CHAT_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API Key gsk_test"}})
    )

    response = client.get("/v1/groq-check", headers=auth_headers)

    assert response.status_code == 502
    assert "gsk_test" not in response.text


def test_groq_check_returns_503_when_key_missing(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from storyweaver.config import get_settings

    monkeypatch.setenv("GROQ_API_KEY", "")
    get_settings.cache_clear()

    response = client.get("/v1/groq-check", headers=auth_headers)

    assert response.status_code == 503
