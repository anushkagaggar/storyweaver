"""Provider-neutral LLM interface (plan §4.1). Adapters (Groq now, local models later) implement it."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class LLMResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    finish_reason: str | None


class LLMError(Exception):
    """The LLM call failed in a way the caller cannot fix by changing the request."""


class LLMRateLimitedError(LLMError):
    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("LLM provider rate limit reached.")
        self.retry_after_seconds = retry_after_seconds


class LLMUnavailableError(LLMError):
    """Provider unreachable, 5xx, or the model was withdrawn (404)."""


class LLMClient(Protocol):
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
        """Return a completion whose content is JSON conforming to `schema` (strict mode where supported)."""
        ...

    async def complete_text(self, *, model: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult:
        """Return a plain-text completion (e.g. a classifier score)."""
        ...
