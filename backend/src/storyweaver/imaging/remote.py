"""Pictures drawn through the Space's `illustrate` Gradio endpoint (plan V3, ZeroGPU from the worker).

On ZeroGPU a GPU exists only inside a `@spaces.GPU` call made from a Gradio request, and its time is charged
to whoever the request comes from. The job worker has no visitor's request, so it calls the Space's own
public URL with an HF token (`HF_TOKEN`), and ZeroGPU charges that account. The endpoint stores the picture
before it answers, so a picture the GPU finished is never lost to a worker crash.

This module is both sides' wire format and the worker's client (`GradioIllustrator`). It never imports
`spaces` or torch; the drawing side is imaging/gpu_drawing.py.

Gradio's HTTP API is two steps: POST /gradio_api/call/<name> returns an event id, then GET
/gradio_api/call/<name>/<event id> streams server-sent events ending in `complete` or `error`.
"""

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from storyweaver.imaging.generator import (
    ImageQuotaExhaustedError,
    ImageRejectedError,
    ImageUnavailableError,
)
from storyweaver.imaging.params import GenerationParams, cache_key
from storyweaver.storage.blob import image_key

logger = logging.getLogger(__name__)

ENDPOINT = "illustrate"
# How long to stop drawing when ZeroGPU says the account's quota is used up (it resets daily).
QUOTA_RETRY_SECONDS = 3600.0

# The endpoint's answers. "rejected": the safety checker flagged it; "quota": ZeroGPU refused to lend a GPU
# (or our own budget); "unavailable": anything worth retrying; "invalid": a bad request (a bug, not retried
# sooner but treated like "unavailable" so the page still ends with the placeholder).
Outcome = str


@dataclass(frozen=True)
class IllustrateRequest:
    story_id: uuid.UUID
    params: GenerationParams
    reference: bytes | None = None  # page 1's PNG, whose sha256 is params.reference_sha256

    @property
    def key(self) -> str:
        """Where the picture is stored: decided by the endpoint from the params, never by the caller."""
        return image_key(self.story_id, cache_key(self.params))

    def to_json(self) -> str:
        return json.dumps(
            {
                "story_id": str(self.story_id),
                "params": json.loads(self.params.to_json()),
                "reference": base64.b64encode(self.reference).decode() if self.reference else None,
            }
        )

    @classmethod
    def from_json(cls, data: str) -> "IllustrateRequest":
        """Raises ValueError (or TypeError/KeyError) for anything malformed."""
        raw = json.loads(data)
        reference = base64.b64decode(raw["reference"], validate=True) if raw.get("reference") else None
        request = cls(uuid.UUID(raw["story_id"]), GenerationParams(**raw["params"]), reference)
        sha = hashlib.sha256(reference).hexdigest() if reference is not None else None
        if sha != request.params.reference_sha256:
            raise ValueError("The reference picture doesn't match its sha256.")
        return request


def answer(outcome: Outcome, **fields: Any) -> dict[str, Any]:
    return {"ok": outcome == "ok", "outcome": outcome, **fields}


def raise_for_answer(value: object, expected_key: str) -> None:
    """Turn the endpoint's answer into success or the ImageGenerator errors the job handles."""
    if not isinstance(value, dict):
        raise ImageUnavailableError(f"unexpected answer from the illustrate endpoint: {value!r:.200}")
    outcome, detail = value.get("outcome"), str(value.get("detail") or "")
    if outcome == "ok":
        if value.get("key") != expected_key:
            raise ImageUnavailableError("the illustrate endpoint stored the picture under another key")
        return
    if outcome == "rejected":
        raise ImageRejectedError(detail or "flagged by the safety checker")
    if outcome == "quota":
        raise ImageQuotaExhaustedError(QUOTA_RETRY_SECONDS, detail or "ZeroGPU quota used up")
    raise ImageUnavailableError(f"illustrate endpoint: {outcome}: {detail}")


def last_event(stream: str) -> tuple[str, str] | None:
    """The last (event, data) pair of a server-sent event stream."""
    event: str | None = None
    last: tuple[str, str] | None = None
    for line in stream.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event is not None:
            last = (event, line[5:].strip())
    return last


class GradioIllustrator:
    """The worker's Illustrator for IMAGE_GRADIO_URL: one Gradio call per picture."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        engine: str,
        hf_token: str | None = None,
        timeout_seconds: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        if hf_token:
            self._headers["Authorization"] = f"Bearer {hf_token}"
        self._engine = engine
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def name(self) -> str:
        return f"gradio:{self._engine}"

    async def illustrate(
        self, story_id: uuid.UUID, params: GenerationParams, key: str, reference: bytes | None = None
    ) -> None:
        request = IllustrateRequest(story_id, params, reference)
        if request.key != key:
            raise ValueError("The image key doesn't match the generation settings.")
        raise_for_answer(await self.call(ENDPOINT, request.to_json()), key)

    async def call(self, name: str, *data: object) -> object:
        """One Gradio API call: its first output, or ImageUnavailableError for any transport failure."""
        url = f"{self._base_url}/gradio_api/call/{name}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers, transport=self._transport
            ) as client:
                started = await client.post(url, json={"data": list(data)})
                started.raise_for_status()
                event_id = started.json().get("event_id")
                if not event_id:
                    raise ImageUnavailableError("Gradio returned no event id")
                result = await client.get(f"{url}/{event_id}")
                result.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImageUnavailableError(
                f"illustrate endpoint unreachable: {type(exc).__name__}: {exc}"
            ) from exc
        event = last_event(result.text)
        if event is None or event[0] != "complete":
            # Gradio sends `error` with the message of a gr.Error, or null when it hides the details.
            raise ImageUnavailableError(f"illustrate endpoint failed: {event[1] if event else 'no result'}")
        try:
            output = json.loads(event[1])
        except json.JSONDecodeError as exc:
            raise ImageUnavailableError("illustrate endpoint sent malformed JSON") from exc
        return output[0] if isinstance(output, list) and output else output
