"""LLM call tracing (plan V1: "Langfuse tracing on every LLM call").

Off unless Langfuse keys are configured (then `adapters/langfuse_tracer.py` is used). Privacy by default: a
call carries prompts and outputs only when content capture is switched on; otherwise metadata only (model,
tokens, latency, outcome, fallback). Calls made inside `trace_scope(...)` group into one trace per scope and
one session per story.

A call is traced as start -> end (not recorded afterwards) so the backend measures its real duration.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, Protocol

from storyweaver.llm.client import ChatMessage

Outcome = Literal["ok", "rate_limited", "unavailable", "error"]


@dataclass(frozen=True)
class TraceScope:
    """A unit of work whose LLM calls belong together, e.g. checking an idea or planning one story."""

    name: str
    session_id: str | None = None  # the story id: Langfuse groups a story's traces by session
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 32 hex chars, as OpenTelemetry needs


@dataclass(frozen=True)
class LLMCallStart:
    name: str  # what the call was for, e.g. "story" (planner schema), "policy_verdict", "text"
    requested_model: str
    model: str
    scope: TraceScope | None
    messages: tuple[ChatMessage, ...] | None  # only when content capture is on

    @property
    def fallback(self) -> bool:
        return self.model != self.requested_model


@dataclass(frozen=True)
class LLMCallEnd:
    outcome: Outcome
    prompt_tokens: int = 0
    completion_tokens: int = 0
    output: str | None = None  # only when content capture is on
    error: str | None = None


class LLMCallSpan(Protocol):
    def end(self, result: LLMCallEnd) -> None: ...


class Tracer(Protocol):
    @property
    def capture_content(self) -> bool: ...

    def start_llm_call(self, start: LLMCallStart) -> LLMCallSpan:
        """Must not block. Raising is tolerated by callers but never stops the LLM call."""
        ...

    def shutdown(self) -> None:
        """Flush buffered traces; called once at app shutdown."""
        ...


class _NoopSpan:
    def end(self, result: LLMCallEnd) -> None:
        return None


class NoopTracer:
    capture_content = False

    def start_llm_call(self, start: LLMCallStart) -> LLMCallSpan:
        return _NoopSpan()

    def shutdown(self) -> None:
        return None


_current_scope: ContextVar[TraceScope | None] = ContextVar("storyweaver_trace_scope", default=None)


@contextmanager
def trace_scope(name: str, session_id: str | None = None) -> Iterator[TraceScope]:
    """Group the LLM calls made inside this block (async-safe: context variables follow the task)."""
    scope = TraceScope(name=name, session_id=session_id)
    token = _current_scope.set(scope)
    try:
        yield scope
    finally:
        _current_scope.reset(token)


def current_scope() -> TraceScope | None:
    return _current_scope.get()
