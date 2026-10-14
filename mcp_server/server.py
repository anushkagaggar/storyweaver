"""StoryWeaver MCP server (plan §6.7, V1): tools, a resource and a prompt over the REST API.

Transport: stdio for Claude Desktop and the MCP Inspector (V1-V8); Streamable HTTP on the Space comes in V9.
Run:  uv run server.py      Inspect:  uv run mcp dev server.py
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from client import ApiConfig, ApiError, StoryWeaverClient


@dataclass
class AppContext:
    api: StoryWeaverClient


def make_server(client_factory: Any = None) -> FastMCP:
    """Build the server. `client_factory` (tests) returns a StoryWeaverClient; default reads env vars."""

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
        api = client_factory() if client_factory else StoryWeaverClient(ApiConfig.from_env())
        try:
            yield AppContext(api=api)
        finally:
            await api.aclose()

    mcp = FastMCP(
        "StoryWeaver",
        instructions=(
            "StoryWeaver makes gentle illustrated bedtime stories for children aged 4-6. "
            "Call create_story with the parent's idea, then poll get_job_status (or get_story) every few "
            "seconds until the story status is 'ready' (usually under two minutes), then read it page "
            "by page."
        ),
        lifespan=lifespan,
    )

    def api_of(ctx: Context[Any, AppContext, Any]) -> StoryWeaverClient:
        return ctx.request_context.lifespan_context.api

    async def call(coro: Any) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await coro
        except ApiError as exc:
            wait = f" Try again in {exc.retry_after_seconds} seconds." if exc.retry_after_seconds else ""
            raise ToolError(f"{exc.message}{wait}") from exc
        return result

    @mcp.tool()
    async def create_story(
        ctx: Context[Any, AppContext, Any],
        prompt: Annotated[str, Field(min_length=3, max_length=500, description="The story idea.")],
        age_band: Literal["4-6"] = "4-6",
        num_scenes: Annotated[int, Field(ge=5, le=6, description="Number of pages: 5 or 6.")] = 5,
    ) -> dict[str, Any]:
        """Start a new bedtime story. Returns story_id and job_id; the story takes up to a few minutes."""
        created = await call(api_of(ctx).create_story(prompt, age_band, num_scenes))
        return {"story_id": created["story_id"], "job_id": created["job_id"], "status": created["status"]}

    @mcp.tool()
    async def get_story(ctx: Context[Any, AppContext, Any], story_id: str) -> dict[str, Any]:
        """Get a story: its status, and when ready its title, moral, characters and pages."""
        return await call(api_of(ctx).get_story(story_id))

    @mcp.tool()
    async def get_job_status(ctx: Context[Any, AppContext, Any], job_id: str) -> dict[str, Any]:
        """Get the status of a story's background job (queued, running, succeeded, failed)."""
        return await call(api_of(ctx).get_job(job_id))

    @mcp.resource("story://{story_id}", mime_type="application/json")
    async def story_resource(story_id: str) -> str:
        """The full Story JSON for a story id."""
        ctx = mcp.get_context()
        try:
            story = await api_of(ctx).get_story(story_id)
        except ApiError as exc:
            raise ValueError(exc.message) from exc
        return json.dumps(story, ensure_ascii=False)

    @mcp.prompt()
    def bedtime_story(favourite_animal: str, theme: str) -> str:
        """A ready-made request for a bedtime story about the child's favourite animal."""
        animal = favourite_animal.strip()
        article = "an" if animal[:1].lower() in "aeiou" else "a"
        return (
            f"Please make a gentle bedtime story about {article} {animal} that is about {theme}. "
            "Use the create_story tool with a short, kind story idea. Then check get_job_status every few "
            "seconds until it has succeeded, fetch the story with get_story, and read it to me one page at a "
            "time, ending with its moral."
        )

    return mcp


# Module-level server: `mcp dev server.py` (MCP Inspector) and `mcp run` look for an object named `server`.
server = make_server()


def main() -> None:
    server.run()  # stdio


if __name__ == "__main__":
    main()
