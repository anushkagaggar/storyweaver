import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from storyweaver import __version__
from storyweaver.api.errors import register_error_handlers
from storyweaver.api.middleware import REQUEST_ID_HEADER, request_id_middleware
from storyweaver.api.routers import diagnostics, health
from storyweaver.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    app = FastAPI(
        title="StoryWeaver API",
        version=__version__,
        # Interactive docs only locally; the production API is called by the Vercel server only.
        docs_url="/docs" if settings.app_env == "local" else None,
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
    return app
