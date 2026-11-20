"""The drawing side of the `illustrate` Gradio endpoint (api/gpu_endpoints.py; caller: imaging/remote.py).

`draw_on_gpu` is the `@spaces.GPU` function: on ZeroGPU it runs with a GPU lent for that call, in a process
of its own, so it returns a plain dict rather than raising. Elsewhere `spaces.GPU` does nothing and it runs
on the local GPU (sd15 through the same route, for tests).

`Drawing.illustrate` checks the request, skips pictures already stored, draws, then stores the picture
before answering. The upload runs outside the GPU call, so it costs no GPU time.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import gradio as gr
import spaces

from storyweaver.imaging.generator import PNG, ImageRejectedError, ImageUnavailableError
from storyweaver.imaging.params import GenerationParams
from storyweaver.imaging.remote import IllustrateRequest, answer
from storyweaver.imaging.sd15 import SD15Local
from storyweaver.storage.blob import BlobError, BlobStore

logger = logging.getLogger(__name__)

# What each call asks ZeroGPU for. SDXL at 1152x896 and 25 steps took 7.9 s on the test Space (20 Nov 2026);
# with the picture check (well under 1 s on the GPU) and up to 2 redraws, a call can take ~26 s. ZeroGPU
# checks the requested time against the quota left, so asking for much more wastes headroom; the plan
# keeps every GPU call under 60 s.
GPU_DURATION_SECONDS = 45

# Set at start-up (api/main.py); a module global so the ZeroGPU process inherits the loaded pipeline.
_engine: SD15Local | None = None


def set_engine(engine: SD15Local | None) -> None:
    global _engine
    _engine = engine


@spaces.GPU(duration=GPU_DURATION_SECONDS)  # type: ignore[untyped-decorator]  # spaces ships no type hints
def draw_on_gpu(params: GenerationParams, reference: bytes | None) -> dict[str, Any]:
    if _engine is None:
        return answer("unavailable", detail="no picture engine on this server")
    try:
        return answer("ok", png=_engine.draw(params, reference))
    except ImageRejectedError as exc:
        return answer("rejected", detail=str(exc))
    except ImageUnavailableError as exc:
        return answer("unavailable", detail=str(exc))
    except Exception as exc:  # crossing a process boundary: report it, don't lose it
        logger.exception("Drawing failed.")
        return answer("unavailable", detail=f"{type(exc).__name__}: {exc}")


def zerogpu_outcome(exc: gr.Error) -> str:
    """ZeroGPU refuses a call with a gr.Error titled e.g. "ZeroGPU quota exceeded" (spaces/zero/client.py)."""
    title = str(getattr(exc, "title", "")).lower()
    return "quota" if "quota" in title or "credits" in title else "unavailable"


class Drawing:
    """`blobs` returns the app's store (built in the lifespan, after the endpoints are registered)."""

    def __init__(self, blobs: Callable[[], BlobStore | None]) -> None:
        self._get_blobs = blobs

    async def illustrate(self, job: str) -> dict[str, Any]:
        blobs = self._get_blobs()
        if blobs is None:
            return answer("unavailable", detail="object storage is not configured")
        try:
            request = IllustrateRequest.from_json(job)
        except (ValueError, TypeError, KeyError) as exc:
            return answer("invalid", detail=str(exc))
        key = request.key
        try:
            if await blobs.exists(key):
                return answer("ok", key=key, seconds=0.0)  # a retry after the picture was stored
            started = time.monotonic()
            try:
                # In a thread: the GPU call blocks. The Gradio request (ZeroGPU reads who to charge from
                # it) travels with the context into the thread.
                result = await asyncio.to_thread(draw_on_gpu, request.params, request.reference)
            except gr.Error as exc:
                logger.warning("ZeroGPU refused a picture: %s", exc.message)
                return answer(zerogpu_outcome(exc), detail=exc.message)
            seconds = time.monotonic() - started
            if not result.get("ok"):
                return {name: value for name, value in result.items() if name != "png"}
            await blobs.put(key, result["png"], PNG)
        except BlobError as exc:
            return answer("unavailable", detail=f"storing the picture failed: {exc}")
        logger.info("Illustrated %s in %.1f s.", key, seconds)
        return answer("ok", key=key, seconds=round(seconds, 2))
