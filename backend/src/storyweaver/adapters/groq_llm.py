"""Groq implementation of LLMClient using strict JSON-schema structured outputs."""

import logging
import time
from typing import Any, Literal, cast

import groq
from groq import AsyncGroq, omit
from groq.types.chat import ChatCompletionMessageParam
from groq.types.chat.completion_create_params import ResponseFormatResponseFormatJsonSchema

from storyweaver.llm.client import (
    ChatMessage,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMResult,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)

ReasoningEffort = Literal["low", "medium", "high"]


def _retry_after_seconds(response: Any) -> float | None:
    value = getattr(response, "headers", {}).get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _error_body(exc: groq.APIStatusError) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    return error if isinstance(error, dict) else {}


def _error_detail(exc: groq.APIStatusError) -> str:
    error = _error_body(exc)
    code = error.get("code") or error.get("type") or "unknown"
    message = str(error.get("message") or exc.message)
    return f"{code}: {message[:300]}"


class GroqLLM:
    def __init__(self, client: AsyncGroq, *, reasoning_effort: ReasoningEffort | None = "low") -> None:
        self._client = client
        self._reasoning_effort = reasoning_effort

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
        response_format: ResponseFormatResponseFormatJsonSchema = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
        return await self._create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
            temperature=temperature,
            reasoning_effort=self._reasoning_effort if self._reasoning_effort else omit,
        )

    async def complete_text(self, *, model: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult:
        return await self._create(model=model, messages=messages, max_tokens=max_tokens)

    async def _create(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int, **options: Any
    ) -> LLMResult:
        payload = cast(
            list[ChatCompletionMessageParam],
            [{"role": message.role, "content": message.content} for message in messages],
        )
        start = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(
                model=model, messages=payload, max_completion_tokens=max_tokens, **options
            )
        except groq.RateLimitError as exc:
            raise LLMRateLimitedError(_retry_after_seconds(exc.response)) from exc
        except groq.NotFoundError as exc:
            # A withdrawn model must fail loudly (plan risk 1); the router can then fall back.
            logger.error("Groq model %s not found (withdrawn or misnamed).", model)
            raise LLMUnavailableError(f"Groq model {model} not found.") from exc
        except groq.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(f"Groq returned HTTP {exc.status_code}.") from exc
            # Groq's code and message explain 4xx failures; this text goes to logs and job records only,
            # never to end users (the API returns its own generic messages).
            detail = _error_detail(exc)
            if _error_body(exc).get("code") == "json_validate_failed":
                # Seen live (15 Oct 2026) despite strict mode: hand the partial output back for repair.
                partial = str(_error_body(exc).get("failed_generation") or "")
                raise LLMInvalidOutputError(f"Groq could not produce valid JSON: {detail}", partial) from exc
            raise LLMError(f"Groq rejected the request (HTTP {exc.status_code}): {detail}") from exc
        except (groq.APIConnectionError, groq.APITimeoutError) as exc:
            raise LLMUnavailableError("Could not reach Groq.") from exc

        latency_ms = (time.perf_counter() - start) * 1000
        if not completion.choices:
            raise LLMError("Groq returned no choices.")
        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise LLMError(f"Groq output was truncated at {max_tokens} tokens.")
        usage = completion.usage
        return LLMResult(
            content=choice.message.content or "",
            model=completion.model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            finish_reason=choice.finish_reason,
        )
