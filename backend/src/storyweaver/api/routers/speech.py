"""Speech endpoints (plan §6.8): POST /v1/speech/transcribe.

The request body is the raw recording (`Content-Type: audio/webm`, `audio/mp4`, `audio/wav`, ...), not
multipart: Starlette spools multipart uploads over 1 MB to a temporary file on disk, and audio must never
touch disk (plan §6.6). The body is read in memory with a 10 MB cap, transcribed, and dropped. Neither
audio nor transcripts are logged.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from storyweaver.api.deps import ServicesDep, require_api_key
from storyweaver.api.errors import problem_response
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.speech.audio_utils import (
    ALLOWED_AUDIO_TYPES,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    AudioDecodeError,
    AudioTooLongError,
    decode_to_mono_16k,
    is_silent,
    media_type,
)
from storyweaver.speech.stt import STTError, STTUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/speech", tags=["speech"], dependencies=[Depends(require_api_key)])

UNAVAILABLE_RETRY_SECONDS = 30

# Parent-facing messages (the frontend shows `detail`).
MESSAGES = {
    "unsupported_media_type": "Please send a voice recording (for example webm, mp4, ogg or wav).",
    "audio_too_large": "That recording is too big. Please keep it under 10 MB.",
    "audio_empty": "We didn't receive any recording. Please try again.",
    "audio_unreadable": "We couldn't play that recording. Please record it again.",
    "audio_too_long": f"Please keep your story idea under {MAX_AUDIO_SECONDS} seconds.",
    "audio_silent": "We couldn't hear anything. Please hold the microphone closer and speak up.",
    "no_speech": "We couldn't make out any words. Please try again, speaking clearly.",
    "stt_unavailable": "Voice input isn't working right now. Please type your idea instead.",
    "stt_busy": "Voice input is busy right now. Please try again shortly, or type your idea.",
}


class TranscriptOut(BaseModel):
    text: str
    engine: str
    model: str
    duration_seconds: float


def _problem(
    request: Request, status_code: int, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return problem_response(request, status_code, MESSAGES[code], headers=headers, extra={"code": code})


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The whole body, or None once it passes `limit` bytes (reading stops there)."""
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            return None
    return bytes(buffer)


@router.post(
    "/transcribe",
    response_model=TranscriptOut,
    responses={
        413: {"description": "Recording larger than 10 MB"},
        415: {"description": "Not an audio Content-Type"},
        422: {"description": "Empty, unreadable, silent, longer than 60 s, or no words heard"},
        429: {"description": "Speech-to-text quota reached and no fallback; see Retry-After"},
        503: {"description": "Speech-to-text unavailable"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                kind: {"schema": {"type": "string", "format": "binary"}} for kind in ALLOWED_AUDIO_TYPES
            },
        }
    },
)
async def transcribe(request: Request, services: ServicesDep) -> Any:
    if media_type(request.headers.get("content-type")) not in ALLOWED_AUDIO_TYPES:
        return _problem(request, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
        return _problem(request, status.HTTP_413_CONTENT_TOO_LARGE, "audio_too_large")
    body = await _read_capped(request, MAX_AUDIO_BYTES)
    if body is None:
        return _problem(request, status.HTTP_413_CONTENT_TOO_LARGE, "audio_too_large")
    if not body:
        return _problem(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "audio_empty")

    try:
        audio = await asyncio.to_thread(decode_to_mono_16k, body)
    except AudioTooLongError:
        return _problem(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "audio_too_long")
    except AudioDecodeError:
        return _problem(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "audio_unreadable")
    finally:
        del body
    if is_silent(audio):
        return _problem(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "audio_silent")

    if services.stt is None:
        return _problem(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "stt_unavailable",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_SECONDS)},
        )
    try:
        transcript = await services.stt.transcribe(audio)
    except QuotaExhaustedError as exc:
        return _problem(
            request,
            status.HTTP_429_TOO_MANY_REQUESTS,
            "stt_busy",
            headers={"Retry-After": str(max(1, round(exc.retry_after_seconds)))},
        )
    except STTUnavailableError as exc:
        logger.warning("Speech-to-text unavailable: %s", exc)
        return _problem(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "stt_unavailable",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_SECONDS)},
        )
    except STTError as exc:
        logger.error("Speech-to-text failed: %s", exc)
        return _problem(request, status.HTTP_503_SERVICE_UNAVAILABLE, "stt_unavailable")

    # Metadata only: never the audio or the words.
    logger.info(
        "Transcribed %.1f s of audio with %s in %.0f ms (%d characters).",
        audio.duration_seconds,
        transcript.model,
        transcript.latency_ms,
        len(transcript.text),
    )
    if not transcript.text:
        return _problem(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "no_speech")
    return TranscriptOut(
        text=transcript.text,
        engine=transcript.engine,
        model=transcript.model,
        duration_seconds=round(audio.duration_seconds, 2),
    )
