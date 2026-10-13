import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from groq import AsyncGroq

from storyweaver.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def require_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.storyweaver_api_key.get_secret_value()
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid API key.")


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
