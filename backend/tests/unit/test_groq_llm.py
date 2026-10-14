import json

import httpx
import pytest
import respx
from groq import AsyncGroq

from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.llm.client import (
    ChatMessage,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMUnavailableError,
)

pytestmark = pytest.mark.unit

URL = "https://api.groq.com/openai/v1/chat/completions"
DUMMY_KEY = "gsk_test"  # pragma: allowlist secret
SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
    "additionalProperties": False,
}


def _completion(content: str = '{"a": "b"}', finish_reason: str = "stop") -> dict[str, object]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-oss-20b",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


async def _call(llm: GroqLLM) -> object:
    return await llm.complete_json(
        model="openai/gpt-oss-20b",
        messages=[ChatMessage("system", "s"), ChatMessage("user", "u")],
        schema_name="thing",
        schema=SCHEMA,
        max_tokens=100,
        temperature=0.5,
    )


def _llm() -> GroqLLM:
    return GroqLLM(AsyncGroq(api_key=DUMMY_KEY, max_retries=0), reasoning_effort="low")


@respx.mock
async def test_sends_strict_json_schema_and_returns_usage() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_completion()))

    result = await _call(_llm())

    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "thing", "schema": SCHEMA, "strict": True},
    }
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == 100
    assert body["messages"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert result.content == '{"a": "b"}'  # type: ignore[attr-defined]
    assert (result.prompt_tokens, result.completion_tokens) == (11, 22)  # type: ignore[attr-defined]


@respx.mock
async def test_rate_limit_carries_retry_after() -> None:
    respx.post(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "12"}, json={"error": {}}))

    with pytest.raises(LLMRateLimitedError) as info:
        await _call(_llm())

    assert info.value.retry_after_seconds == 12.0


@respx.mock
@pytest.mark.parametrize("status", [404, 500, 503])
async def test_withdrawn_model_and_server_errors_are_unavailable(status: int) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status, json={"error": {"message": "x"}}))

    with pytest.raises(LLMUnavailableError):
        await _call(_llm())


@respx.mock
async def test_bad_request_and_truncation_are_llm_errors() -> None:
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(LLMError):
        await _call(_llm())

    respx.post(URL).mock(return_value=httpx.Response(200, json=_completion(finish_reason="length")))
    with pytest.raises(LLMError, match="truncated"):
        await _call(_llm())


@respx.mock
async def test_json_validate_failed_becomes_invalid_output_with_partial_generation() -> None:
    """Live 15 Oct 2026: strict mode still returned 400 json_validate_failed for some prompts."""
    respx.post(URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "json_validate_failed",
                    "message": "Failed to generate JSON. Please adjust your prompt.",
                    "failed_generation": '{"title": "Half a story',
                }
            },
        )
    )

    with pytest.raises(LLMInvalidOutputError) as info:
        await _call(_llm())

    assert info.value.partial_output == '{"title": "Half a story'
    assert "json_validate_failed" in str(info.value)


@respx.mock
async def test_other_bad_requests_include_groqs_reason() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "context_length_exceeded", "message": "too long"}}
        )
    )

    with pytest.raises(LLMError, match="context_length_exceeded: too long"):
        await _call(_llm())
