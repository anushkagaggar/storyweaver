"""Every MCP tool, the resource and the prompt, through the real MCP protocol (in-memory client, plan §12).

The REST API is mocked with respx: success, bad input and API errors per tool.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent, TextResourceContents

from client import ApiConfig, StoryWeaverClient
from server import make_server

API = "http://api.test"
STORY_ID = "7fd3029c-610e-47c2-adfc-db3f55e3ce75"
JOB_ID = "7f7a597f-11f9-40c8-92b9-f7fa96527fec"
STORY = {
    "id": STORY_ID,
    "status": "ready",
    "title": "Tilly the Tiny Turtle",
    "moral": "Dreams take patience.",
    "scenes": [
        {"idx": 1, "text": "Tilly looked at the sea.", "lines": [], "image_url": None, "audio_url": None}
    ],
}


@asynccontextmanager
async def connect() -> AsyncIterator[ClientSession]:
    """Open the session in the test's own task: anyio task groups must exit where they began."""
    server = make_server(lambda: StoryWeaverClient(ApiConfig(API, "test-key")))
    async with create_connected_server_and_client_session(server) as client_session:
        yield client_session


def _text(result: Any) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_lists_v1_tools_resource_and_prompt() -> None:
    async with connect() as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        templates = [t.uriTemplate for t in (await session.list_resource_templates()).resourceTemplates]
        prompts = [p.name for p in (await session.list_prompts()).prompts]

        assert tools == {"create_story", "get_story", "get_job_status"}
        assert templates == ["story://{story_id}"]
        assert prompts == ["bedtime_story"]


@respx.mock
async def test_create_story_sends_api_key_and_idempotency_key() -> None:
    async with connect() as session:
        route = respx.post(f"{API}/v1/stories").mock(
            return_value=httpx.Response(
                202, json={"story_id": STORY_ID, "job_id": JOB_ID, "status": "queued"}
            )
        )

        result = await session.call_tool("create_story", {"prompt": "a tiny turtle who wants to see the sea"})

        assert not result.isError
        assert result.structuredContent == {"story_id": STORY_ID, "job_id": JOB_ID, "status": "queued"}
        request = route.calls.last.request
        assert request.headers["X-API-Key"] == "test-key"  # pragma: allowlist secret
        assert request.headers["Idempotency-Key"].startswith("mcp-")
        assert json.loads(request.content) == {
            "prompt": "a tiny turtle who wants to see the sea",
            "age_band": "4-6",
            "num_scenes": 5,
        }


@respx.mock
async def test_unsafe_idea_returns_the_api_message_as_a_tool_error() -> None:
    async with connect() as session:
        respx.post(f"{API}/v1/stories").mock(
            return_value=httpx.Response(
                422,
                json={
                    "code": "unsafe_input",
                    "detail": "This story idea can't be used. Try a gentle, kind idea.",
                },
            )
        )

        result = await session.call_tool("create_story", {"prompt": "visit www.example.com"})

        assert result.isError
        assert "can't be used" in _text(result)


@respx.mock
async def test_busy_api_tells_the_assistant_when_to_retry() -> None:
    async with connect() as session:
        respx.post(f"{API}/v1/stories").mock(
            return_value=httpx.Response(429, headers={"retry-after": "42"}, json={})
        )

        result = await session.call_tool("create_story", {"prompt": "a hedgehog learns to swim"})

        assert result.isError
        assert "busy" in _text(result) and "42 seconds" in _text(result)


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [({"prompt": "hi"}, "prompt"), ({"prompt": "a dog", "num_scenes": 9}, "num_scenes")],
)
async def test_create_story_validates_arguments_before_calling_the_api(
    arguments: dict[str, Any], fragment: str
) -> None:
    async with connect() as session:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/v1/stories")
            result = await session.call_tool("create_story", arguments)

        assert result.isError and fragment in _text(result)
        assert not route.called


@respx.mock
async def test_get_story_and_job_status() -> None:
    async with connect() as session:
        respx.get(f"{API}/v1/stories/{STORY_ID}").mock(return_value=httpx.Response(200, json=STORY))
        respx.get(f"{API}/v1/jobs/{JOB_ID}").mock(
            return_value=httpx.Response(200, json={"id": JOB_ID, "status": "succeeded"})
        )

        story = await session.call_tool("get_story", {"story_id": STORY_ID})
        job = await session.call_tool("get_job_status", {"job_id": JOB_ID})

        assert (
            story.structuredContent is not None
            and story.structuredContent["title"] == "Tilly the Tiny Turtle"
        )
        assert job.structuredContent is not None
        assert job.structuredContent["status"] == "succeeded"
        assert "get_story" in job.structuredContent["progress"]
        assert "check_again_in_seconds" not in story.structuredContent  # ready = final


@respx.mock
async def test_bad_or_unknown_ids_are_errors() -> None:
    async with connect() as session:
        respx.get(f"{API}/v1/stories/{STORY_ID}").mock(
            return_value=httpx.Response(404, json={"detail": "Story not found."})
        )

        unknown = await session.call_tool("get_story", {"story_id": STORY_ID})
        malformed = await session.call_tool("get_story", {"story_id": "../../v1/jobs"})

        assert unknown.isError and "Not found" in _text(unknown)
        assert malformed.isError and "UUID" in _text(malformed)


async def test_unreachable_api_is_a_clear_error() -> None:
    async with connect() as session:
        with respx.mock as router:
            router.get(f"{API}/v1/jobs/{JOB_ID}").mock(side_effect=httpx.ConnectError("refused"))
            result = await session.call_tool("get_job_status", {"job_id": JOB_ID})

        assert result.isError and "unreachable" in _text(result)


@respx.mock
async def test_story_resource_returns_full_story_json() -> None:
    async with connect() as session:
        respx.get(f"{API}/v1/stories/{STORY_ID}").mock(return_value=httpx.Response(200, json=STORY))

        resource = await session.read_resource(f"story://{STORY_ID}")  # type: ignore[arg-type]

        content = resource.contents[0]
        assert isinstance(content, TextResourceContents)
        assert json.loads(content.text) == STORY


async def test_bedtime_story_prompt_guides_the_tool_flow() -> None:
    async with connect() as session:
        prompt = await session.get_prompt("bedtime_story", {"favourite_animal": "otter", "theme": "sharing"})

        text = prompt.messages[0].content
        assert isinstance(text, TextContent)
        assert "about an otter" in text.text and "sharing" in text.text and "create_story" in text.text


@respx.mock
async def test_in_progress_story_explains_itself_and_says_when_to_check() -> None:
    """Live 15 Oct 2026: Claude stopped polling a story in 'checking' because it looked stuck."""
    async with connect() as session:
        checking = {**STORY, "status": "checking", "title": None, "moral": None, "scenes": []}
        respx.get(f"{API}/v1/stories/{STORY_ID}").mock(return_value=httpx.Response(200, json=checking))

        result = await session.call_tool("get_story", {"story_id": STORY_ID})

        content = result.structuredContent
        assert content is not None and content["status"] == "checking"
        assert "safety check" in content["progress"] and "Keep checking" in content["progress"]
        assert content["check_again_in_seconds"] == 15


@respx.mock
async def test_quota_deferred_job_is_explained_as_normal() -> None:
    async with connect() as session:
        later = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        deferred = {"id": JOB_ID, "status": "queued", "attempts": 0, "run_after": later}
        respx.get(f"{API}/v1/jobs/{JOB_ID}").mock(return_value=httpx.Response(200, json=deferred))

        result = await session.call_tool("get_job_status", {"job_id": JOB_ID})

        content = result.structuredContent
        assert content is not None
        assert "quota" in content["progress"] and "normal" in content["progress"]
        assert 30 <= content["check_again_in_seconds"] <= 36
