import asyncio
import logging
from collections.abc import Awaitable

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.api.deps import ServicesDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

CHECK_TIMEOUT_SECONDS = 5.0


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(services: ServicesDep) -> dict[str, object]:
    """Ready when every configured dependency answers. Unconfigured ones are reported, not failed."""
    checks: dict[str, str] = {}

    if services.sessions is None:
        checks["database"] = "not_configured"
    else:
        checks["database"] = await _check("database", _ping_database(services.sessions))
    if services.redis is None:
        checks["redis"] = "not_configured"
    else:
        checks["redis"] = await _check("redis", services.redis.ping())
    checks["worker"] = "running" if services.worker is not None else "disabled"

    if "down" in checks.values():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Not ready: {checks}")
    return {"status": "ready", "checks": checks}


async def _ping_database(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(text("SELECT 1"))


async def _check(name: str, probe: Awaitable[object]) -> str:
    try:
        await asyncio.wait_for(probe, timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Readiness check failed for %s: %s", name, type(exc).__name__)
        return "down"
    return "ok"
