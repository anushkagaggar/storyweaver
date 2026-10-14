"""Hugging Face Space entrypoint: a Gradio Server carrying the REST API and the GPU endpoints (ADR-0001)."""

import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR / "src"))

# Privacy: stop Gradio/HF Hub from sending usage analytics. Must be set before gradio is imported.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from storyweaver.api.main import create_app, lifespan
from storyweaver.config import get_settings

logger = logging.getLogger("storyweaver.startup")


def run_migrations() -> None:
    """`alembic upgrade head` on start-up (plan §6.2). Migrations are expand/contract, so this is safe while
    the previous release is still serving."""
    settings = get_settings()
    if settings.database_url is None:
        logger.warning("DATABASE_URL not set; skipping migrations.")
        return
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    # alembic.ini's logging config would disable the app's existing loggers; the app configures logging.
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


app = create_app()

if __name__ == "__main__":
    run_migrations()
    # launch() (not uvicorn.run) is required: ZeroGPU's startup hook runs inside it. Gradio replaces the
    # constructor's lifespan under launch(), so pass ours again here.
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("PORT", "7860")),
        show_error=False,
        ssr_mode=False,
        mcp_server=False,
        app_kwargs={"lifespan": lifespan},
    )
