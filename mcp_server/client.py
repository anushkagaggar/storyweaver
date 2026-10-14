"""HTTP client for the StoryWeaver REST API (/v1). The MCP server holds no business logic: it calls these."""

import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:7860"
STORY_ID_CHARS = set("0123456789abcdefABCDEF-")


@dataclass
class ApiError(Exception):
    """A non-2xx answer from the API, with a message safe to show to the user."""

    status: int
    message: str
    retry_after_seconds: int | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    api_key: str

    @classmethod
    def from_env(cls) -> "ApiConfig":
        api_key = os.environ.get("STORYWEAVER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("STORYWEAVER_API_KEY is not set for the MCP server.")
        base_url = os.environ.get("STORYWEAVER_API_URL", DEFAULT_API_URL).strip().rstrip("/")
        return cls(base_url=base_url, api_key=api_key)


def _problem_message(response: httpx.Response) -> str:
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail")
    if body.get("code") == "unsafe_input" and isinstance(detail, str):
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


class StoryWeaverClient:
    def __init__(self, config: ApiConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"X-API-Key": config.api_key},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ApiError(503, "The StoryWeaver API is unreachable.") from exc
        if response.is_success:
            result: dict[str, Any] = response.json()
            return result
        retry = response.headers.get("retry-after")
        raise ApiError(
            response.status_code,
            _problem_message(response),
            int(retry) if retry and retry.isdigit() else None,
        )

    async def create_story(self, prompt: str, age_band: str, num_scenes: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/stories",
            json={"prompt": prompt, "age_band": age_band, "num_scenes": num_scenes},
            # A fresh key per tool call: the API then de-duplicates transport-level retries of this call.
            headers={"Idempotency-Key": f"mcp-{uuid.uuid4()}"},
        )

    async def get_story(self, story_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/stories/{check_id(story_id, 'story_id')}")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/jobs/{check_id(job_id, 'job_id')}")
