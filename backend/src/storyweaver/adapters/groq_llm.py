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
        payload = cast(
            list[ChatCompletionMessageParam],
            [{"role": message.role, "content": message.content} for message in messages],
        )
        response_format: ResponseFormatResponseFormatJsonSchema = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }

        start = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=payload,
                response_format=response_format,
                max_completion_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=self._reasoning_effort if self._reasoning_effort else omit,
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
            raise LLMError(f"Groq rejected the request (HTTP {exc.status_code}).") from exc
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
