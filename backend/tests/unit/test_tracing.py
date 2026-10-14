import uuid
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from storyweaver.adapters.langfuse_tracer import LangfuseTracer
from storyweaver.llm.client import ChatMessage, LLMError, LLMRateLimitedError, LLMResult
from storyweaver.observability.tracing import (
    LLMCallEnd,
    LLMCallSpan,
    LLMCallStart,
    TraceScope,
    current_scope,
    trace_scope,
)
from storyweaver.routing.llm import RoutedLLM
from storyweaver.routing.router import ModelRouter
from storyweaver.routing.store import InMemoryQuotaStore

pytestmark = pytest.mark.unit

GPT20 = "openai/gpt-oss-20b"
GPT120 = "openai/gpt-oss-120b"
MESSAGES = [ChatMessage("system", "policy"), ChatMessage("user", "a shy otter")]
FAKE_PUBLIC = "pk-lf-test"  # pragma: allowlist secret
FAKE_SECRET = "sk-lf-test"  # pragma: allowlist secret


class RecordingTracer:
    """Collects (start, end) pairs for assertions."""

    def __init__(self, capture_content: bool = False) -> None:
        self.capture_content = capture_content
        self.calls: list[tuple[LLMCallStart, LLMCallEnd | None]] = []

    def start_llm_call(self, start: LLMCallStart) -> LLMCallSpan:
        index = len(self.calls)
        self.calls.append((start, None))
        tracer = self

        class Span:
            def end(self, result: LLMCallEnd) -> None:
                tracer.calls[index] = (start, result)

        return Span()

    def shutdown(self) -> None:
        return None


class BrokenTracer(RecordingTracer):
    def start_llm_call(self, start: LLMCallStart) -> LLMCallSpan:
        raise RuntimeError("tracing backend down")


class ScriptedLLM:
    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script

    async def complete_json(self, *, model: str, **_: Any) -> LLMResult:
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResult('{"ok": true}', model, 100, 50, 12.0, "stop")

    async def complete_text(self, *, model: str, **_: Any) -> LLMResult:
        return await self.complete_json(model=model)


def _llm(script: dict[str, list[Any]], tracer: Any) -> RoutedLLM:
    router = ModelRouter(InMemoryQuotaStore(), clock=lambda: 1_000_000_040.0)
    return RoutedLLM(ScriptedLLM(script), router, tracer=tracer)


async def _call(llm: RoutedLLM) -> LLMResult:
    return await llm.complete_json(
        model=GPT20, messages=MESSAGES, schema_name="story", schema={}, max_tokens=500, temperature=0.7
    )


async def test_successful_call_is_traced_with_metadata_only_by_default() -> None:
    tracer = RecordingTracer()

    await _call(_llm({GPT20: ["ok"]}, tracer))

    ((start, end),) = tracer.calls
    assert (start.name, start.model, start.fallback, start.messages) == ("story", GPT20, False, None)
    assert end is not None
    assert (end.outcome, end.prompt_tokens, end.completion_tokens, end.output) == ("ok", 100, 50, None)


async def test_content_is_traced_only_when_capture_is_on() -> None:
    tracer = RecordingTracer(capture_content=True)

    await _call(_llm({GPT20: ["ok"]}, tracer))

    start, end = tracer.calls[0]
    assert start.messages == tuple(MESSAGES)
    assert end is not None and end.output == '{"ok": true}'


async def test_each_attempt_is_traced_and_fallback_is_flagged() -> None:
    tracer = RecordingTracer()

    await _call(_llm({GPT20: [LLMRateLimitedError(5)], GPT120: ["ok"]}, tracer))

    summary = [(s.model, s.fallback, e.outcome if e else None) for s, e in tracer.calls]
    assert summary == [(GPT20, False, "rate_limited"), (GPT120, True, "ok")]


async def test_other_errors_are_traced_then_raised() -> None:
    tracer = RecordingTracer()

    with pytest.raises(LLMError):
        await _call(_llm({GPT20: [LLMError("truncated")]}, tracer))

    end = tracer.calls[0][1]
    assert end is not None and (end.outcome, end.error) == ("error", "truncated")


async def test_calls_inside_a_scope_share_its_trace() -> None:
    tracer = RecordingTracer()
    llm = _llm({GPT20: ["ok", "ok"]}, tracer)

    with trace_scope("story.plan", session_id="story-1") as scope:
        await _call(llm)
        await llm.complete_text(model=GPT20, messages=MESSAGES, max_tokens=10)

    assert [s.scope for s, _ in tracer.calls] == [scope, scope]
    assert [s.name for s, _ in tracer.calls] == ["story", "text"]
    assert scope.session_id == "story-1" and len(scope.trace_id) == 32
    assert current_scope() is None


async def test_a_failing_tracer_never_breaks_the_call() -> None:
    result = await _call(_llm({GPT20: ["ok"]}, BrokenTracer()))

    assert result.model == GPT20


# --- Langfuse adapter, exported to an in-memory OpenTelemetry exporter (no network, no account) ----------


def _langfuse(capture: bool) -> tuple[LangfuseTracer, InMemorySpanExporter]:
    # Langfuse caches one client per public key, so each test uses its own key and exporter.
    exporter = InMemorySpanExporter()
    tracer = LangfuseTracer(
        public_key=f"{FAKE_PUBLIC}-{uuid.uuid4().hex[:8]}",
        secret_key=FAKE_SECRET,
        host="http://localhost:9",
        capture_content=capture,
        environment="local",
        span_exporter=exporter,
    )
    return tracer, exporter


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_langfuse_generation_carries_trace_session_model_usage_and_no_content() -> None:
    tracer, exporter = _langfuse(capture=False)
    scope = TraceScope(name="story.plan", session_id="story-123")

    span = tracer.start_llm_call(LLMCallStart("story", GPT20, GPT120, scope, None))
    span.end(LLMCallEnd("ok", prompt_tokens=700, completion_tokens=1300))
    tracer.shutdown()

    (exported,) = exporter.get_finished_spans()
    attrs = _attrs(exported)
    assert format(exported.context.trace_id, "032x") == scope.trace_id
    assert exported.name == "llm.story"
    assert attrs.get("session.id") == "story-123"
    assert attrs.get("langfuse.trace.name") == "story.plan"
    assert attrs.get("langfuse.observation.type") == "generation"
    assert attrs.get("langfuse.observation.model.name") == GPT120
    assert '"input": 700' in str(attrs.get("langfuse.observation.usage_details"))
    assert "langfuse.observation.input" not in attrs and "langfuse.observation.output" not in attrs
    assert exported.end_time is not None and exported.end_time >= exported.start_time


def test_langfuse_captures_content_and_flags_failures_when_asked() -> None:
    tracer, exporter = _langfuse(capture=True)

    span = tracer.start_llm_call(LLMCallStart("story", GPT20, GPT20, None, tuple(MESSAGES)))
    span.end(LLMCallEnd("rate_limited", error="429 from Groq"))
    tracer.shutdown()

    attrs = _attrs(exporter.get_finished_spans()[0])
    assert "a shy otter" in str(attrs.get("langfuse.observation.input"))
    assert attrs.get("langfuse.observation.level") == "WARNING"
    assert attrs.get("langfuse.observation.status_message") == "429 from Groq"
