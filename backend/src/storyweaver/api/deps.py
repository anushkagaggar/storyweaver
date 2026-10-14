import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from groq import AsyncGroq
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.config import Settings, get_settings
from storyweaver.orchestrator.story_service import StoryService
from storyweaver.services import Services

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


def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    return services if services is not None else Services()


ServicesDep = Annotated[Services, Depends(get_services)]


def get_story_service(services: ServicesDep) -> StoryService:
    if services.stories is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Story storage is not configured.")
    return services.stories


StoryServiceDep = Annotated[StoryService, Depends(get_story_service)]


def get_sessions(services: ServicesDep) -> async_sessionmaker[AsyncSession]:
    if services.sessions is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Story storage is not configured.")
    return services.sessions


SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]
