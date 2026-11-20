"""MCP `illustrate` (V3, text -> image) over the real MCP protocol; the REST API and the bucket are mocked."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import respx
from helpers import API, connect
from mcp.types import ImageContent, TextContent

PICTURE_ID = "5c2e0c4e-8a51-4d0f-9d8e-2f3b1a0c9e77"
SOURCE_ID = "7fd3029c-610e-47c2-adfc-db3f55e3ce75"
BUCKET_URL = "https://bucket.test/stories/pic.png?X-Amz-Signature=abc"
PNG = b"\x89PNG\r\n\x1a\n" + b"a picture of a purple dragon"


def story(image_status: str, image_url: str | None = None, status: str = "ready") -> dict[str, Any]:
    return {
        "id": PICTURE_ID,
        "kind": "picture",
        "status": status,
        "title": "A purple dragon",
        "scenes": [
            {"idx": 1, "text": "A purple dragon.", "image_status": image_status, "image_url": image_url}
        ],
    }


def created() -> httpx.Response:
    return httpx.Response(202, json={"story_id": PICTURE_ID, "job_id": "j", "status": "ready"})


def parts(result: Any) -> tuple[dict[str, Any], list[ImageContent]]:
    texts = [block for block in result.content if isinstance(block, TextContent)]
    images = [block for block in result.content if isinstance(block, ImageContent)]
    return json.loads(texts[0].text), images


@respx.mock
async def test_draws_waits_and_shows_the_picture(tmp_path: Path) -> None:
    post = respx.post(f"{API}/v1/pictures").mock(return_value=created())
    respx.get(f"{API}/v1/stories/{PICTURE_ID}").mock(
        side_effect=[
            httpx.Response(200, json=story("pending")),
            httpx.Response(200, json=story("ready", BUCKET_URL)),
        ]
    )
    bucket = respx.get(BUCKET_URL).mock(return_value=httpx.Response(200, content=PNG))

    async with connect(picture_dir=tmp_path, picture_poll_seconds=0.01) as session:
        result = await session.call_tool("illustrate", {"prompt": "draw a purple dragon"})

    assert not result.isError
    summary, images = parts(result)
    assert summary["story_id"] == PICTURE_ID and summary["image_status"] == "ready"
    assert summary["title"] == "A purple dragon" and summary["note"] == "Here is the picture."
    assert await asyncio.to_thread(Path(summary["picture_path"]).read_bytes) == PNG
    assert [(image.mimeType, base64.b64decode(image.data)) for image in images] == [("image/png", PNG)]
    request = post.calls.last.request
    assert json.loads(request.content) == {"prompt": "draw a purple dragon"}
    assert request.headers["Idempotency-Key"].startswith("mcp-")
    # The presigned URL is object storage, not our API: the API key must never be sent there.
    assert "X-API-Key" not in bucket.calls.last.request.headers


@respx.mock
async def test_character_ref_names_a_story_and_character(tmp_path: Path) -> None:
    post = respx.post(f"{API}/v1/pictures").mock(return_value=created())
    respx.get(f"{API}/v1/stories/{PICTURE_ID}").mock(
        return_value=httpx.Response(200, json=story("ready", BUCKET_URL))
    )
    respx.get(BUCKET_URL).mock(return_value=httpx.Response(200, content=PNG))

    async with connect(picture_dir=tmp_path) as session:
        result = await session.call_tool(
            "illustrate", {"prompt": "Pip at the beach", "story_id": SOURCE_ID, "character_id": "pip"}
        )

    assert not result.isError
    assert json.loads(post.calls.last.request.content) == {
        "prompt": "Pip at the beach",
        "story_id": SOURCE_ID,
        "character_id": "pip",
    }


@respx.mock
async def test_a_bad_story_id_never_reaches_the_api() -> None:
    post = respx.post(f"{API}/v1/pictures")
    async with connect() as session:
        result = await session.call_tool("illustrate", {"prompt": "Pip", "story_id": "../admin"})
    assert result.isError and "story_id must be a UUID" in result.content[0].text  # type: ignore[union-attr]
    assert not post.called


@respx.mock
async def test_still_drawing_returns_the_story_id_to_check_later() -> None:
    respx.post(f"{API}/v1/pictures").mock(return_value=created())
    respx.get(f"{API}/v1/stories/{PICTURE_ID}").mock(return_value=httpx.Response(200, json=story("pending")))

    async with connect(picture_wait_seconds=0.05, picture_poll_seconds=0.01) as session:
        result = await session.call_tool("illustrate", {"prompt": "draw a purple dragon"})

    assert not result.isError
    summary, images = parts(result)
    assert images == [] and summary["image_status"] == "pending"
    assert PICTURE_ID in summary["note"] and summary["check_again_in_seconds"] > 0


@respx.mock
async def test_a_placeholder_is_shown_with_a_note(tmp_path: Path) -> None:
    respx.post(f"{API}/v1/pictures").mock(return_value=created())
    respx.get(f"{API}/v1/stories/{PICTURE_ID}").mock(
        return_value=httpx.Response(200, json=story("placeholder", BUCKET_URL))
    )
    respx.get(BUCKET_URL).mock(return_value=httpx.Response(200, content=PNG))

    async with connect(picture_dir=tmp_path) as session:
        result = await session.call_tool("illustrate", {"prompt": "draw a purple dragon"})

    summary, images = parts(result)
    assert summary["image_status"] == "placeholder" and "placeholder" in summary["note"] and len(images) == 1


@respx.mock
async def test_api_refusals_come_back_as_their_own_words() -> None:
    respx.post(f"{API}/v1/pictures").mock(
        side_effect=[
            httpx.Response(
                422,
                json={"code": "nothing_to_draw", "detail": 'Please say what to draw, for example "a cat".'},
            ),
            httpx.Response(
                404,
                json={
                    "code": "character_not_found",
                    "detail": "We couldn't find that character in your stories.",
                },
            ),
            httpx.Response(
                429,
                json={"code": "visitor_limit", "detail": "You've drawn lots of pictures this hour!"},
                headers={"Retry-After": "600"},
            ),
        ]
    )
    async with connect() as session:
        unclear = await session.call_tool("illustrate", {"prompt": "draw me please"})
        missing = await session.call_tool("illustrate", {"prompt": "Pip", "story_id": SOURCE_ID})
        limited = await session.call_tool("illustrate", {"prompt": "a cat"})

    assert unclear.isError and "what to draw" in unclear.content[0].text  # type: ignore[union-attr]
    assert missing.isError and "couldn't find that character" in missing.content[0].text  # type: ignore[union-attr]
    assert limited.isError and "Try again in 600 seconds" in limited.content[0].text  # type: ignore[union-attr]


@respx.mock
async def test_a_failed_download_is_a_tool_error(tmp_path: Path) -> None:
    respx.post(f"{API}/v1/pictures").mock(return_value=created())
    respx.get(f"{API}/v1/stories/{PICTURE_ID}").mock(
        return_value=httpx.Response(200, json=story("ready", BUCKET_URL))
    )
    respx.get(BUCKET_URL).mock(return_value=httpx.Response(403))

    async with connect(picture_dir=tmp_path) as session:
        result = await session.call_tool("illustrate", {"prompt": "draw a purple dragon"})

    assert result.isError and "couldn't be downloaded" in result.content[0].text  # type: ignore[union-attr]
