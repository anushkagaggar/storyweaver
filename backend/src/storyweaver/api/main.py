import logging
from typing import Any

import gradio as gr
from fastapi.middleware.cors import CORSMiddleware
from gradio.route_utils import CustomCORSMiddleware

from storyweaver import __version__
from storyweaver.api.errors import register_error_handlers
from storyweaver.api.gpu_endpoints import register_gpu_endpoints
from storyweaver.api.middleware import REQUEST_ID_HEADER, request_id_middleware
from storyweaver.api.routers import diagnostics, health
from storyweaver.config import get_settings


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
    register_gpu_endpoints(app)
    return app
