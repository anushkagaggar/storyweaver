"""LLMClient wrapper that routes every call through the quota router, with model fallbacks."""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from storyweaver.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRateLimitedError,
    LLMResult,
    LLMUnavailableError,
)
from storyweaver.observability.tracing import (
    LLMCallEnd,
    LLMCallSpan,
    LLMCallStart,
    NoopTracer,
    Outcome,
    Tracer,
    current_scope,
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


CHARS_PER_TOKEN = 4  # typical for English; `settle` replaces the estimate with Groq's real count


def estimate_prompt_tokens(messages: Sequence[ChatMessage], schema: Mapping[str, Any] | None = None) -> int:
    """Estimate input tokens before the call. Too high blocks small budgets (safeguard: 1,800 tokens/min);
    too low only risks a Groq 429, which the router handles."""
    characters = sum(len(m.content) for m in messages) + (len(json.dumps(schema)) if schema else 0)
    return characters // CHARS_PER_TOKEN + 10 * len(messages)


class RoutedLLM:
    def __init__(
        self,
        inner: LLMClient,
        router: ModelRouter,
        fallbacks: Mapping[str, Sequence[str]] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._inner = inner
        self._router = router
        self._fallbacks = DEFAULT_FALLBACKS if fallbacks is None else fallbacks
        self._tracer: Tracer = tracer or NoopTracer()

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
        async def call(chosen: str) -> LLMResult:
            return await self._inner.complete_json(
                model=chosen,
                messages=messages,
                schema_name=schema_name,
                schema=schema,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        estimate = estimate_prompt_tokens(messages, schema) + max_tokens
        return await self._route(model, estimate, call, name=schema_name, messages=messages)

    async def complete_text(self, *, model: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult:
        async def call(chosen: str) -> LLMResult:
            return await self._inner.complete_text(model=chosen, messages=messages, max_tokens=max_tokens)

        estimate = estimate_prompt_tokens(messages) + max_tokens
        return await self._route(model, estimate, call, name="text", messages=messages)

    async def _route(
        self,
        model: str,
        estimated_tokens: int,
        call: Callable[[str], Awaitable[LLMResult]],
        *,
        name: str,
        messages: list[ChatMessage],
    ) -> LLMResult:
        candidates = [model, *self._fallbacks.get(model, [])]
        usage = Usage(requests=1, tokens=estimated_tokens)
        last_error: LLMError | None = None

        while candidates:
            reservation = await self._router.acquire(candidates, usage)  # may raise QuotaExhaustedError
            chosen = reservation.model
            span = self._start_span(name, model, chosen, messages)

            try:
                result = await call(chosen)
            except LLMRateLimitedError as exc:
                self._end_span(span, "rate_limited", error=str(exc))
                await self._router.release_tokens(reservation)
                await self._router.cooldown(chosen, exc.retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN)
                logger.warning("Groq 429 on %s; cooling down and trying the next model.", chosen)
                last_error = exc
            except LLMUnavailableError as exc:
                self._end_span(span, "unavailable", error=str(exc))
                await self._router.release_tokens(reservation)
                await self._router.cooldown(chosen, UNAVAILABLE_COOLDOWN)
                logger.warning("%s unavailable (%s); trying the next model.", chosen, exc)
                last_error = exc
            except LLMError as exc:
                self._end_span(span, "error", error=str(exc))
                raise
            else:
                self._end_span(span, "ok", result)
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

    def _start_span(
        self, name: str, requested: str, chosen: str, messages: list[ChatMessage]
    ) -> LLMCallSpan | None:
        try:
            return self._tracer.start_llm_call(
                LLMCallStart(
                    name=name,
                    requested_model=requested,
                    model=chosen,
                    scope=current_scope(),
                    messages=tuple(messages) if self._tracer.capture_content else None,
                )
            )
        except Exception:  # tracing must never break a story
            logger.warning("Tracing failed to start; continuing without it.", exc_info=True)
            return None

    def _end_span(
        self,
        span: LLMCallSpan | None,
        outcome: Outcome,
        result: LLMResult | None = None,
        error: str | None = None,
    ) -> None:
        if span is None:
            return
        try:
            span.end(
                LLMCallEnd(
                    outcome=outcome,
                    prompt_tokens=result.prompt_tokens if result else 0,
                    completion_tokens=result.completion_tokens if result else 0,
                    output=result.content if result and self._tracer.capture_content else None,
                    error=error,
                )
            )
        except Exception:  # tracing must never break a story
            logger.warning("Tracing failed to end; continuing without it.", exc_info=True)
