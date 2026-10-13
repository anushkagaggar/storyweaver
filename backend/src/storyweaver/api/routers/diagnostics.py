import logging
import time
from typing import Any

import groq
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from storyweaver.api.deps import GroqClientDep, SettingsDep, require_api_key
from storyweaver.api.middleware import get_request_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["diagnostics"], dependencies=[Depends(require_api_key)])


class EchoRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class EchoResponse(BaseModel):
    message: str
    request_id: str


class GroqCheckResponse(BaseModel):
    ok: bool
    model: str
    finish_reason: str | None
    latency_ms: float
    total_tokens: int | None


@router.post("/echo")
async def echo(body: EchoRequest, request: Request) -> EchoResponse:
    return EchoResponse(message=body.message, request_id=get_request_id(request))


@router.get("/gpu-check")
async def gpu_check() -> dict[str, Any]:
    try:
        from storyweaver.diagnostics import run_gpu_matmul
    except ImportError as exc:
        logger.warning("GPU diagnostics unavailable: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GPU support is not installed.") from exc

    result: dict[str, Any] = await run_in_threadpool(run_gpu_matmul)
    if not result.get("ok"):
        reason = str(result.get("reason", "GPU unavailable."))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, reason)
    return result


@router.get("/groq-check")
async def groq_check(settings: SettingsDep, client: GroqClientDep) -> GroqCheckResponse:
    model = settings.planner_model_id
    start = time.perf_counter()
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with OK."}],
            max_completion_tokens=8,
        )
    except groq.RateLimitError as exc:
        retry_after = exc.response.headers.get("retry-after")
        headers = {"Retry-After": retry_after} if retry_after else None
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Groq rate limit reached.", headers) from exc
    except groq.AuthenticationError as exc:
        logger.error("Groq rejected the API key.")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Groq rejected the configured API key.") from exc
    except groq.APIStatusError as exc:
        logger.error("Groq returned HTTP %s for model %s.", exc.status_code, model)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Groq returned HTTP {exc.status_code}.") from exc
    except groq.APIConnectionError as exc:
        logger.error("Could not reach Groq: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not reach Groq.") from exc

    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    choice = completion.choices[0] if completion.choices else None
    return GroqCheckResponse(
        ok=True,
        model=completion.model,
        finish_reason=choice.finish_reason if choice else None,
        latency_ms=latency_ms,
        total_tokens=completion.usage.total_tokens if completion.usage else None,
    )
