"""StoryWeaver MCP server (plan §6.7, V1-V3): tools, resources and a prompt over the REST API.

Transport: stdio for Claude Desktop and the MCP Inspector (V1-V8); Streamable HTTP on the Space comes in V9.
Run:  uv run server.py      Inspect:  uv run mcp dev server.py
"""

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from client import ApiConfig, ApiError, StoryWeaverClient, read_audio_file


@dataclass
class AppContext:
    api: StoryWeaverClient


MAX_NARRATE_CHARS = 600  # the API's limit (enforced there too); here it gives a clear error before the call


def default_audio_dir() -> Path:
    """Where `narrate` saves MP3s: STORYWEAVER_AUDIO_DIR, else a folder under the system temp directory."""
    configured = os.environ.get("STORYWEAVER_AUDIO_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "storyweaver-audio"


def wait_phrase(seconds: int) -> str:
    """A Retry-After as words: a daily quota's ~14 h reads "about 14 hours", not "50400 seconds"."""
    if seconds < 120:
        return f"{seconds} seconds"
    if seconds < 3600:
        return f"about {round(seconds / 60)} minutes"
    hours = round(seconds / 3600)
    return "about 1 hour" if hours == 1 else f"about {hours} hours"


def default_picture_dir() -> Path:
    """Where `illustrate` saves pictures: STORYWEAVER_PICTURE_DIR, else a folder under the system temp dir."""
    configured = os.environ.get("STORYWEAVER_PICTURE_DIR", "").strip()
    return (
        Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "storyweaver-pictures"
    )


def save_png(directory: Path, png: bytes, story_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"picture-{story_id[:8]}.png"
    path.write_bytes(png)
    return path


# How long `illustrate` waits for its picture before handing back the story id to check later. A picture
# takes ~10 s on ZeroGPU (up to ~30 s with redraws), plus queueing behind a story's pages.
PICTURE_WAIT_SECONDS = 120.0
PICTURE_POLL_SECONDS = 2.0
PICTURE_NOTES = {
    "ready": "Here is the picture.",
    "placeholder": (
        "Today's drawing time has run out, so a soft placeholder stands in. Try again tomorrow for a real "
        "picture."
    ),
    "failed": "The picture couldn't be drawn. Try a different idea.",
}


def save_mp3(directory: Path, mp3: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"narration-{uuid.uuid4().hex[:12]}.mp3"
    path.write_bytes(mp3)
    return path


POLL_SECONDS = 15
FINAL_STORY = {"ready", "failed"}
FINAL_JOB = {"succeeded", "failed"}

# What each story status means, for the assistant reading the tool result (the API returns bare statuses).
STORY_PROGRESS = {
    "queued": "Waiting for the storyteller. Keep checking.",
    "planning": "The story is being written. Keep checking.",
    "checking": (
        "The story is written and is waiting for its safety check. On the free tier this can take a minute "
        "or two when busy. Keep checking; the title and pages appear once it is ready."
    ),
    "ready": (
        "The story is ready. Share its notice first, if any. Read it page by page, then share its moral."
    ),
    "failed": "The story could not be made. Share the error detail and suggest a different, gentle idea.",
}


def seconds_until(iso_time: str | None) -> int | None:
    if not iso_time:
        return None
    try:
        when = datetime.fromisoformat(iso_time)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, round((when - datetime.now(UTC)).total_seconds()))


def _picture_status(story: dict[str, Any]) -> str:
    """The picture page's image status: pending, ready, placeholder or failed."""
    if story.get("status") == "failed":
        return "failed"
    scenes = story.get("scenes") or []
    return str(scenes[0].get("image_status", "pending")) if scenes else "pending"


def with_story_progress(story: dict[str, Any]) -> dict[str, Any]:
    status = str(story.get("status", ""))
    extra: dict[str, Any] = {"progress": STORY_PROGRESS.get(status, "Keep checking.")}
    if status not in FINAL_STORY:
        extra["check_again_in_seconds"] = POLL_SECONDS
    return {**story, **extra}


def with_job_progress(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status", ""))
    if status in FINAL_JOB:
        return {**job, "progress": "Finished. Call get_story to read the result."}
    wait = seconds_until(job.get("run_after"))
    note = "The job is running. Keep checking."
    if status == "queued" and wait:
        note = (
            "Queued: waiting for free-tier model quota, which is normal and does not use up a retry. "
            "Keep checking; the story usually finishes within 1-3 minutes."
        )
    return {**job, "progress": note, "check_again_in_seconds": max(5, min(60, (wait or 0) + 5))}


def make_server(
    client_factory: Any = None,
    audio_dir: Path | None = None,
    picture_dir: Path | None = None,
    picture_wait_seconds: float = PICTURE_WAIT_SECONDS,
    picture_poll_seconds: float = PICTURE_POLL_SECONDS,
) -> FastMCP:
    """Build the server. The keyword arguments (tests) override the env-var defaults and timings."""

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
            "Call create_story with the parent's idea, then call get_story every check_again_in_seconds "
            "until its status is 'ready' or 'failed'. Stories usually take 1-3 minutes on the free tier: "
            "queued, planning and checking are normal progress, so keep checking and don't give up early. "
            "Then read the story page by page and end with its moral. "
            "To turn a recording of the parent's voice into a story idea, call transcribe with the audio "
            "file's path, then pass the text to create_story. To hear any text read aloud, call narrate "
            "(the voices://list resource lists the voices). "
            'To draw one picture ("draw a purple dragon"), call illustrate; to draw a character from one '
            "of the parent's stories, pass that story's story_id (and the character's id from get_story)."
        ),
        lifespan=lifespan,
    )

    def api_of(ctx: Context[Any, AppContext, Any]) -> StoryWeaverClient:
        return ctx.request_context.lifespan_context.api

    async def call[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except ApiError as exc:
            wait = f" Try again in {wait_phrase(exc.retry_after_seconds)}." if exc.retry_after_seconds else ""
            raise ToolError(f"{exc.message}{wait}") from exc

    @mcp.tool()
    async def create_story(
        ctx: Context[Any, AppContext, Any],
        prompt: Annotated[str, Field(min_length=3, max_length=500, description="The story idea.")],
        age_band: Literal["4-6"] = "4-6",
        num_scenes: Annotated[int, Field(ge=5, le=6, description="Number of pages: 5 or 6.")] = 5,
    ) -> dict[str, Any]:
        """Start a new bedtime story. Returns story_id and job_id. The story usually takes 1-3 minutes:
        poll get_story until its status is 'ready' or 'failed'."""
        created = await call(api_of(ctx).create_story(prompt, age_band, num_scenes))
        return {"story_id": created["story_id"], "job_id": created["job_id"], "status": created["status"]}

    @mcp.tool()
    async def get_story(ctx: Context[Any, AppContext, Any], story_id: str) -> dict[str, Any]:
        """Get a story: its status, a plain-English progress note, and when ready its title, moral,
        characters and pages. While not final, call again after check_again_in_seconds."""
        return with_story_progress(await call(api_of(ctx).get_story(story_id)))

    @mcp.tool()
    async def get_job_status(ctx: Context[Any, AppContext, Any], job_id: str) -> dict[str, Any]:
        """Get a story job's status (queued, running, succeeded, failed) with a progress note. A queued job
        with a later run_after is waiting for free-tier quota, which is normal."""
        return with_job_progress(await call(api_of(ctx).get_job(job_id)))

    @mcp.tool()
    async def transcribe(
        ctx: Context[Any, AppContext, Any],
        audio_path: Annotated[
            str,
            Field(
                description="Path to a voice recording on this computer (webm, ogg, mp4/m4a, mp3, wav, flac)."
            ),
        ],
    ) -> dict[str, Any]:
        """Turn a short voice recording (up to 60 seconds and 10 MB) into text, for example a spoken story
        idea. Returns {text, engine, model, duration_seconds}; pass the text to create_story. The recording
        is sent for transcription only; it is not kept."""
        audio, content_type = await call(read_audio_file(audio_path))
        return await call(api_of(ctx).transcribe(audio, content_type))

    @mcp.tool()
    async def narrate(
        ctx: Context[Any, AppContext, Any],
        text: Annotated[
            str, Field(min_length=1, max_length=MAX_NARRATE_CHARS, description="What to read aloud.")
        ],
        voice: Annotated[
            str | None, Field(description="A voice id from voices://list; omit for the default narrator.")
        ] = None,
    ) -> dict[str, Any]:
        """Read text aloud (up to 600 characters) as an MP3. The file is saved on this computer; open
        audio_path (or audio_url) to listen. Returns {audio_url, audio_path, engine, voice,
        duration_seconds}. For a whole story, use the audio on each page from get_story instead."""
        narrated = await call(api_of(ctx).narrate(text, voice))
        path = await asyncio.to_thread(save_mp3, audio_dir or default_audio_dir(), narrated.mp3)
        return {
            "audio_url": path.as_uri(),
            "audio_path": str(path),
            "engine": narrated.engine,
            "voice": narrated.voice,
            "duration_seconds": narrated.duration_seconds,
        }

    @mcp.tool(structured_output=False)
    async def illustrate(
        ctx: Context[Any, AppContext, Any],
        prompt: Annotated[
            str,
            Field(
                min_length=3,
                max_length=300,
                description='What to draw, as the child said it: "draw a purple dragon", "Pip at the beach".',
            ),
        ],
        story_id: Annotated[
            str | None,
            Field(description="character_ref: a story of the parent's whose character to draw (get_story)."),
        ] = None,
        character_id: Annotated[
            str | None,
            Field(max_length=32, description="That story's character id; omit for its first character."),
        ] = None,
    ) -> list[Any]:
        """Draw one gentle picture-book picture (text -> image) and show it. Takes 10-30 seconds; waits for
        it. With story_id (and optionally character_id) it draws that story's character, keeping its look.
        Returns the picture plus {story_id, title, image_status, picture_path, note}. If it isn't ready in
        time, returns the story_id: call get_story later and open the page's image_url."""
        api = api_of(ctx)
        created = await call(api.create_picture(prompt, story_id, character_id))
        picture_id = str(created["story_id"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + picture_wait_seconds
        story = await call(api.get_story(picture_id))
        while _picture_status(story) == "pending" and loop.time() < deadline:
            await ctx.report_progress(min(0.95, 1 - (deadline - loop.time()) / picture_wait_seconds), 1.0)
            await asyncio.sleep(picture_poll_seconds)
            story = await call(api.get_story(picture_id))
        status = _picture_status(story)
        summary: dict[str, Any] = {
            "story_id": picture_id,
            "title": story.get("title"),
            "image_status": status,
        }
        page = (story.get("scenes") or [{}])[0]
        if status == "pending" or not page.get("image_url"):
            summary["note"] = PICTURE_NOTES.get(status) or (
                f"Still drawing. Call get_story with story_id {picture_id} in a little while."
            )
            summary["check_again_in_seconds"] = POLL_SECONDS
            return [summary]
        png = await call(api.download_picture(str(page["image_url"])))
        path = await asyncio.to_thread(save_png, picture_dir or default_picture_dir(), png, picture_id)
        summary.update(picture_path=str(path), picture_url=path.as_uri(), note=PICTURE_NOTES.get(status, ""))
        return [summary, Image(data=png, format="png")]

    @mcp.resource("voices://list", mime_type="application/json")
    async def voices_resource() -> str:
        """The voices narrate accepts, for both engines (Orpheus and Kokoro), with each engine's narrator."""
        ctx = mcp.get_context()
        try:
            voices = await api_of(ctx).list_voices()
        except ApiError as exc:
            raise ValueError(exc.message) from exc
        return json.dumps(voices, ensure_ascii=False)

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
