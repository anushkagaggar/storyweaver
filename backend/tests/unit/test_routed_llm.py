from typing import Any

import pytest

from storyweaver.llm.client import ChatMessage, LLMRateLimitedError, LLMResult, LLMUnavailableError
from storyweaver.routing.llm import RoutedLLM, estimate_prompt_tokens
from storyweaver.routing.router import ModelRouter, QuotaExhaustedError
from storyweaver.routing.store import InMemoryQuotaStore

pytestmark = pytest.mark.unit

GPT20 = "openai/gpt-oss-20b"
GPT120 = "openai/gpt-oss-120b"
MESSAGES = [ChatMessage("system", "s" * 300), ChatMessage("user", "u" * 300)]
SCHEMA: dict[str, Any] = {"type": "object"}


class ScriptedLLM:
    """Each model maps to a list of outcomes (an LLMResult or an exception), consumed in order."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[str] = []

    async def complete_json(self, *, model: str, **_: Any) -> LLMResult:
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, LLMResult)
        return outcome


def _result(model: str, tokens: int = 1_500) -> LLMResult:
    return LLMResult("{}", model, tokens // 3, tokens - tokens // 3, 10.0, "stop")


def _routed(inner: ScriptedLLM) -> tuple[RoutedLLM, InMemoryQuotaStore]:
    store = InMemoryQuotaStore()
    router = ModelRouter(store, margin=0.9, clock=lambda: 1_000_000_040.0)
    return RoutedLLM(inner, router), store


async def _call(llm: RoutedLLM, model: str = GPT20) -> LLMResult:
    return await llm.complete_json(
        model=model, messages=MESSAGES, schema_name="s", schema=SCHEMA, max_tokens=4_000, temperature=0.7
    )


def _minute_tokens(store: InMemoryQuotaStore, model: str) -> int:
    return sum(v for k, v in store.counters.items() if k.startswith(f"quota:{model}:tokens:60:"))


def test_estimate_is_conservative() -> None:
    assert estimate_prompt_tokens(MESSAGES, SCHEMA) >= 600 // 4


async def test_success_settles_actual_tokens() -> None:
    inner = ScriptedLLM({GPT20: [_result(GPT20, tokens=1_500)]})
    llm, store = _routed(inner)

    result = await _call(llm)

    assert result.model == GPT20
    assert _minute_tokens(store, GPT20) == 1_500


async def test_rate_limit_cools_down_and_falls_back() -> None:
    inner = ScriptedLLM({GPT20: [LLMRateLimitedError(retry_after_seconds=7)], GPT120: [_result(GPT120)]})
    llm, store = _routed(inner)

    result = await _call(llm)

    assert inner.calls == [GPT20, GPT120]
    assert result.model == GPT120
    assert store.cooldown_until[GPT20] == 1_000_000_040.0 + 7
    assert _minute_tokens(store, GPT20) == 0  # reserved tokens returned after the 429


async def test_unavailable_model_falls_back() -> None:
    inner = ScriptedLLM({GPT20: [LLMUnavailableError("404")], GPT120: [_result(GPT120)]})
    llm, _ = _routed(inner)

    assert (await _call(llm)).model == GPT120


async def test_all_models_failing_raises_last_error() -> None:
    inner = ScriptedLLM(
        {GPT20: [LLMUnavailableError("down")], GPT120: [LLMRateLimitedError(retry_after_seconds=None)]}
    )
    llm, store = _routed(inner)

    with pytest.raises(LLMRateLimitedError):
        await _call(llm)
    assert store.cooldown_until[GPT120] == 1_000_000_040.0 + 60  # default cooldown without retry-after


async def test_no_quota_anywhere_raises_quota_exhausted_without_calling_groq() -> None:
    inner = ScriptedLLM({})
    llm, store = _routed(inner)
    for model in (GPT20, GPT120):
        store.cooldown_until[model] = 1_000_000_040.0 + 30

    with pytest.raises(QuotaExhaustedError) as info:
        await _call(llm)

    assert inner.calls == []
    assert info.value.retry_after_seconds == 30
