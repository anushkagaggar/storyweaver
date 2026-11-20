"""HTTP client for the StoryWeaver REST API (/v1). The MCP server holds no business logic: it calls these."""

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:7860"
STORY_ID_CHARS = set("0123456789abcdefABCDEF-")

# Pre-flight limits so a huge or non-audio file is never read or sent. The API enforces the real ones (10 MB,
# 60 s, 600 characters); these only stop a wrong path early. `transcribe` is the one tool that reads a file
# off this machine, so the extension allowlist is also what stops a prompt from sending an arbitrary file.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
AUDIO_CONTENT_TYPES = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}
# Problem codes whose `detail` is a parent-facing sentence written for this purpose (safe to show as is).
FRIENDLY_CODES = {
    "unsafe_input",
    "unsupported_media_type",
    "audio_too_large",
    "audio_empty",
    "audio_unreadable",
    "audio_too_long",
    "audio_silent",
    "no_speech",
    "stt_unavailable",
    "stt_busy",
    "unknown_voice",
    "tts_busy",
    "tts_unavailable",
    "visitor_limit",
    "nothing_to_draw",
    "pictures_unavailable",
    "character_not_found",
}
MAX_PICTURE_BYTES = 15 * 1024 * 1024  # an SDXL PNG is ~2 MB; anything far larger isn't ours


@dataclass
class ApiError(Exception):
    """A non-2xx answer from the API, with a message safe to show to the user."""

    status: int
    message: str
    retry_after_seconds: int | None = None

    def __str__(self) -> str:
        return self.message


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines from a .env file; `#` comments, blanks, `export ` and quotes are handled.

    Deliberately tiny and dependency-free: only the two STORYWEAVER_* settings below are ever read from it.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def env_file_candidates() -> list[Path]:
    """Where to look for a .env: STORYWEAVER_ENV_FILE, then beside this file, then the repo root."""
    configured = os.environ.get("STORYWEAVER_ENV_FILE", "").strip()
    if configured:
        return [Path(configured).expanduser()]
    here = Path(__file__).resolve().parent
    return [here / ".env", here.parent / ".env"]


def setting(name: str, from_file: dict[str, str], default: str = "") -> str:
    """An environment variable if set, else the .env file, else `default`."""
    value = os.environ.get(name, "").strip()
    return value or from_file.get(name, "").strip() or default


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    api_key: str

    @classmethod
    def from_env(cls) -> "ApiConfig":
        """Settings from the environment, falling back to a .env file.

        The .env fallback is what makes `mcp dev` work: the MCP SDK spawns a stdio server with only a small
        allowlist of environment variables (PATH, TEMP, ...), so anything exported in the shell is dropped
        before the server starts. Claude Desktop's `env` block does reach us, and takes precedence.
        """
        from_file: dict[str, str] = {}
        for candidate in env_file_candidates():
            from_file = read_env_file(candidate)
            if from_file:
                break
        api_key = setting("STORYWEAVER_API_KEY", from_file)
        if not api_key:
            looked_in = ", ".join(str(p) for p in env_file_candidates())
            raise RuntimeError(
                "STORYWEAVER_API_KEY is not set for the MCP server. Put it in Claude Desktop's `env` "
                f"block, or in a .env file (looked in: {looked_in}). Note that exporting it in your shell "
                "does not work under `mcp dev`: the MCP SDK does not pass it to the server process."
            )
        return cls(
            base_url=setting("STORYWEAVER_API_URL", from_file, DEFAULT_API_URL).rstrip("/"), api_key=api_key
        )


def _problem_message(response: httpx.Response) -> str:
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail")
    if body.get("code") in FRIENDLY_CODES and isinstance(detail, str):
        return detail
    if response.status_code == 429:
        return "The storyteller is busy right now."
    if response.status_code == 503:
        return "Story making is paused for a moment."
    if response.status_code == 404:
        return "Not found."
    if response.status_code == 422:
        return f"Invalid request: {detail}" if isinstance(detail, str) else "Invalid request."
    if response.status_code == 401:
        return "The MCP server's StoryWeaver API key was rejected."
    return f"The StoryWeaver API returned HTTP {response.status_code}."


def check_id(value: str, what: str) -> str:
    """Ids are UUIDs; reject anything else before it reaches a URL path."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ApiError(422, f"{what} must be a UUID like 7fd3029c-610e-47c2-adfc-db3f55e3ce75.") from None


@dataclass(frozen=True)
class NarratedAudio:
    mp3: bytes
    engine: str
    voice: str
    duration_seconds: float


def _read_recording(audio_path: str) -> tuple[bytes, str]:
    path = Path(audio_path).expanduser()
    content_type = AUDIO_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        kinds = ", ".join(sorted(AUDIO_CONTENT_TYPES))
        raise ApiError(
            422, f"audio_path must be a recording ({kinds}), not '{path.suffix or 'no extension'}'."
        )
    try:
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise ApiError(413, "That recording is too big. Please keep it under 10 MB.")
        return path.read_bytes(), content_type
    except FileNotFoundError:
        raise ApiError(404, f"No recording found at {path}.") from None
    except OSError:  # a directory, no permission, ...
        raise ApiError(422, f"The recording at {path} can't be read.") from None


async def read_audio_file(audio_path: str) -> tuple[bytes, str]:
    """The recording's bytes and Content-Type to send, or an ApiError saying what is wrong with the path."""
    return await asyncio.to_thread(_read_recording, audio_path)


class StoryWeaverClient:
    def __init__(self, config: ApiConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._download_transport = transport
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"X-API-Key": config.api_key},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ApiError(503, "The StoryWeaver API is unreachable.") from exc
        if response.is_success:
            return response
        retry = response.headers.get("retry-after")
        raise ApiError(
            response.status_code,
            _problem_message(response),
            int(retry) if retry and retry.isdigit() else None,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        result: dict[str, Any] = (await self._send(method, path, **kwargs)).json()
        return result

    async def create_story(self, prompt: str, age_band: str, num_scenes: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/stories",
            json={"prompt": prompt, "age_band": age_band, "num_scenes": num_scenes},
            # A fresh key per tool call: the API then de-duplicates transport-level retries of this call.
            headers={"Idempotency-Key": f"mcp-{uuid.uuid4()}"},
        )

    async def create_picture(
        self, prompt: str, story_id: str | None = None, character_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": prompt}
        if story_id:
            body["story_id"] = check_id(story_id, "story_id")
        if character_id:
            body["character_id"] = character_id
        return await self._request(
            "POST", "/v1/pictures", json=body, headers={"Idempotency-Key": f"mcp-{uuid.uuid4()}"}
        )

    async def download_picture(self, url: str) -> bytes:
        """A page's picture from its presigned URL (object storage, not the API): a separate client, so the
        API key never leaves for another host."""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0), transport=self._download_transport
            ) as http:
                response = await http.get(url)
        except httpx.TransportError as exc:
            raise ApiError(503, "The picture couldn't be downloaded.") from exc
        if not response.is_success or len(response.content) > MAX_PICTURE_BYTES:
            raise ApiError(response.status_code, "The picture couldn't be downloaded.")
        return response.content

    async def get_story(self, story_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/stories/{check_id(story_id, 'story_id')}")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/jobs/{check_id(job_id, 'job_id')}")

    async def transcribe(self, audio: bytes, content_type: str) -> dict[str, Any]:
        """The recording goes as the raw request body (never multipart), and is not kept or logged here."""
        return await self._request(
            "POST", "/v1/speech/transcribe", content=audio, headers={"Content-Type": content_type}
        )

    async def narrate(self, text: str, voice: str | None) -> NarratedAudio:
        body: dict[str, Any] = {"text": text}
        if voice:
            body["voice"] = voice
        response = await self._send("POST", "/v1/speech/narrate", json=body)
        return NarratedAudio(
            mp3=response.content,
            engine=response.headers.get("x-tts-engine", "unknown"),
            voice=response.headers.get("x-tts-voice", voice or "unknown"),
            duration_seconds=float(response.headers.get("x-audio-duration", "0") or 0),
        )

    async def list_voices(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/speech/voices")
