"""Helpers shared by the Groq adapters (LLM, Whisper): reading Retry-After and error bodies."""

from typing import Any

import groq


def retry_after_seconds(response: Any) -> float | None:
    value = getattr(response, "headers", {}).get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def error_body(exc: groq.APIStatusError) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    return error if isinstance(error, dict) else {}


def error_detail(exc: groq.APIStatusError) -> str:
    error = error_body(exc)
    code = error.get("code") or error.get("type") or "unknown"
    message = str(error.get("message") or exc.message)
    return f"{code}: {message[:300]}"
