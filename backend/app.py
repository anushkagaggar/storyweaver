"""Hugging Face Space entrypoint: a Gradio Server carrying the REST API and the GPU endpoints (ADR-0001)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Privacy: stop Gradio/HF Hub from sending usage analytics. Must be set before gradio is imported.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from storyweaver.api.main import create_app

app = create_app()

if __name__ == "__main__":
    # launch() (not uvicorn.run) is required: ZeroGPU's startup hook runs inside it.
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("PORT", "7860")),
        show_error=False,
        ssr_mode=False,
        mcp_server=False,
    )
