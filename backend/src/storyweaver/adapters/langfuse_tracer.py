"""Langfuse implementation of Tracer (SDK v4, OpenTelemetry-based).

Each TraceScope becomes one trace (its trace_id), named after the scope and tagged with the story id as the
Langfuse session; each LLM call becomes a `generation` observation inside it. The SDK batches and exports in
the background, so starting and ending observations never waits on the network.
"""

import logging
from typing import Any, Literal

from langfuse import Langfuse, propagate_attributes
from langfuse.types import TraceContext

from storyweaver.observability.tracing import LLMCallEnd, LLMCallSpan, LLMCallStart

logger = logging.getLogger(__name__)

Level = Literal["DEFAULT", "WARNING", "ERROR"]
_LEVELS: dict[str, Level] = {
    "ok": "DEFAULT",
    "rate_limited": "WARNING",
    "unavailable": "WARNING",
    "error": "ERROR",
}


class _GenerationSpan:
    def __init__(self, observation: Any) -> None:
        self._observation = observation

    def end(self, result: LLMCallEnd) -> None:
        self._observation.update(
            output=result.output,
            usage_details={"input": result.prompt_tokens, "output": result.completion_tokens}
            if result.outcome == "ok"
            else None,
            level=_LEVELS[result.outcome],
            status_message=result.error,
            metadata={"outcome": result.outcome},
        )
        self._observation.end()


class LangfuseTracer:
    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str,
        capture_content: bool,
        environment: str,
        span_exporter: Any = None,  # tests pass an in-memory exporter
    ) -> None:
        self._capture = capture_content
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            environment=environment,
            span_exporter=span_exporter,
        )

    @property
    def capture_content(self) -> bool:
        return self._capture

    def start_llm_call(self, start: LLMCallStart) -> LLMCallSpan:
        scope = start.scope
        trace_context: TraceContext | None = {"trace_id": scope.trace_id} if scope else None
        messages = (
            [{"role": m.role, "content": m.content} for m in start.messages] if start.messages else None
        )
        # Trace name and session (story id) must be set as the observation starts; Langfuse's span processor
        # copies attributes propagated in the current context onto new spans.
        with propagate_attributes(
            session_id=scope.session_id if scope else None,
            trace_name=scope.name if scope else f"llm.{start.name}",
        ):
            observation = self._client.start_observation(
                trace_context=trace_context,
                name=f"llm.{start.name}",
                as_type="generation",
                model=start.model,
                input=messages,
                metadata={"requested_model": start.requested_model, "fallback": start.fallback},
            )
        return _GenerationSpan(observation)

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception:
            logger.warning("Langfuse shutdown failed; some traces may be lost.", exc_info=True)
