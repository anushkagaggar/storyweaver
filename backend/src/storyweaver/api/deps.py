import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from groq import AsyncGroq

from storyweaver.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


INVALID_API_KEY_MESSAGE = "Missing or invalid API key."  # pragma: allowlist secret


def api_key_is_valid(provided: str | None, settings: Settings) -> bool:
    """Constant-time API key check shared by REST routes and Gradio GPU endpoints."""
    expected = settings.storyweaver_api_key.get_secret_value()
    return provided is not None and secrets.compare_digest(provided.encode(), expected.encode())


async def require_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if not api_key_is_valid(x_api_key, settings):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_API_KEY_MESSAGE)


async def get_groq_client(settings: SettingsDep) -> AsyncIterator[AsyncGroq]:
    if settings.groq_api_key is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GROQ_API_KEY is not configured.")
    # Retries are the router's job (V1); diagnostics should report the first failure as-is.
    client = AsyncGroq(api_key=settings.groq_api_key.get_secret_value(), max_retries=0, timeout=15.0)
    try:
        yield client
    finally:
        await client.close()


GroqClientDep = Annotated[AsyncGroq, Depends(get_groq_client)]
