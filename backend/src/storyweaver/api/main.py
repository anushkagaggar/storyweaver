import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import gradio as gr
from fastapi.middleware.cors import CORSMiddleware
from gradio.route_utils import CustomCORSMiddleware

from storyweaver import __version__
from storyweaver.api.errors import register_error_handlers
from storyweaver.api.gpu_endpoints import register_gpu_endpoints
from storyweaver.api.middleware import REQUEST_ID_HEADER, request_id_middleware
from storyweaver.api.routers import diagnostics, health, stories
from storyweaver.config import get_settings
from storyweaver.services import build_services


class StoryWeaverServer(gr.Server):
    """A gr.Server whose only CORS policy is ours.

    Gradio's own CORS middleware reflects any Origin when the host is not localhost (i.e. on the Space), and
    only defers to a *parent* app's CORSMiddleware, which Server mode never passes. Skipping it leaves the
    CORSMiddleware added in create_app() as the single policy (ADR-0001).
    """

    def add_middleware(self, middleware_class: Any, *args: Any, **kwargs: Any) -> None:
        if middleware_class is CustomCORSMiddleware:
            return
        super().add_middleware(middleware_class, *args, **kwargs)


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    """Build long-lived services (DB, Redis, Groq, job worker) and close them on shutdown.

    Under gr.Server.launch(), Gradio replaces the constructor's lifespan, so app.py must also pass this
    function via launch(app_kwargs={"lifespan": lifespan}); the constructor argument covers TestClient.
    """
    services = build_services(get_settings())
    app.state.services = services
    services.start_worker()
    try:
        yield
    finally:
        await services.close()


def create_app() -> gr.Server:
    """Build the app: a Gradio Server (a FastAPI subclass) carrying REST routes and Gradio GPU endpoints.

    Gradio Server is required by ZeroGPU (see ADR-0001); REST routes work on it exactly as on FastAPI.
    Gradio GPU endpoints are registered here but only become routable after server.launch().
    """
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    is_local = settings.app_env == "local"
    app = StoryWeaverServer(
        title="StoryWeaver API",
        version=__version__,
        # Schema and interactive docs only locally; the production API is called by the Vercel server only.
        openapi_url="/openapi.json" if is_local else None,
        docs_url="/docs" if is_local else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", REQUEST_ID_HEADER, "Idempotency-Key"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.middleware("http")(request_id_middleware)
    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(diagnostics.router)
    app.include_router(stories.router)
    register_gpu_endpoints(app)
    return app
