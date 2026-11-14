"""Shared by the MCP test modules: an in-memory MCP session against the mocked REST API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from client import ApiConfig, StoryWeaverClient
from server import make_server

API = "http://api.test"


@asynccontextmanager
async def connect(audio_dir: Path | None = None) -> AsyncIterator[ClientSession]:
    """Open the session in the test's own task: anyio task groups must exit where they began."""
    server = make_server(lambda: StoryWeaverClient(ApiConfig(API, "test-key")), audio_dir=audio_dir)
    async with create_connected_server_and_client_session(server) as client_session:
        yield client_session


def text_of(result: Any) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text
