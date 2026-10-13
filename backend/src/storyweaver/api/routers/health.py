from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    # V0 has no backing services yet. From V1, check Postgres and Redis here and return 503 if either is down.
    return {"status": "ready"}
