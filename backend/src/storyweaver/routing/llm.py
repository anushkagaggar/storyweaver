"""LLMClient wrapper that routes every call through the quota router, with model fallbacks."""

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from storyweaver.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRateLimitedError,
    LLMResult,
    LLMUnavailableError,
)
from storyweaver.routing.router import ModelRouter
from storyweaver.routing.store import Usage

logger = logging.getLogger(__name__)

# Both models are free tier with separate quotas. The fine-tuned local model joins these chains in V6.
DEFAULT_FALLBACKS: dict[str, list[str]] = {
    "openai/gpt-oss-20b": ["openai/gpt-oss-120b"],
    "openai/gpt-oss-120b": ["openai/gpt-oss-20b"],
}

DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
UNAVAILABLE_COOLDOWN = 30.0


def estimate_prompt_tokens(messages: Sequence[ChatMessage], schema: Mapping[str, Any]) -> int:
    """Deliberately high estimate (about 3 characters per token), corrected by `settle` after the call."""
    characters = sum(len(m.content) for m in messages) + len(json.dumps(schema))
    return characters // 3 + 20 * len(messages)


class RoutedLLM:
    def __init__(
        self,
        inner: LLMClient,
        router: ModelRouter,
        fallbacks: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._inner = inner
        self._router = router
        self._fallbacks = DEFAULT_FALLBACKS if fallbacks is None else fallbacks

    async def complete_json(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        candidates = [model, *self._fallbacks.get(model, [])]
        usage = Usage(requests=1, tokens=estimate_prompt_tokens(messages, schema) + max_tokens)
        last_error: LLMError | None = None

        while candidates:
            reservation = await self._router.acquire(candidates, usage)  # may raise QuotaExhaustedError
            chosen = reservation.model
            try:
                result = await self._inner.complete_json(
                    model=chosen,
                    messages=messages,
                    schema_name=schema_name,
                    schema=schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except LLMRateLimitedError as exc:
                await self._router.release_tokens(reservation)
                await self._router.cooldown(chosen, exc.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN)
                logger.warning("Groq 429 on %s; cooling down and trying the next model.", chosen)
                last_error = exc
            except LLMUnavailableError as exc:
                await self._router.release_tokens(reservation)
                await self._router.cooldown(chosen, UNAVAILABLE_COOLDOWN)
                logger.warning("%s unavailable (%s); trying the next model.", chosen, exc)
                last_error = exc
            else:
                await self._router.settle(reservation, result.prompt_tokens + result.completion_tokens)
                logger.info(
                    "LLM call routed: requested=%s used=%s fallback=%s tokens=%d latency_ms=%.0f",
                    model,
                    chosen,
                    chosen != model,
                    result.prompt_tokens + result.completion_tokens,
                    result.latency_ms,
                )
                return result
            candidates = [c for c in candidates if c != chosen]

        if last_error is None:  # unreachable: the loop only ends after a failed call
            raise LLMError("No model was tried.")
        raise last_error
