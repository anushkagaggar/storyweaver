"""Hugging Face Space entrypoint: FastAPI with the Gradio UI mounted at /ui."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Privacy: stop Gradio/HF Hub from sending usage analytics. Must be set before gradio is imported.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import gradio as gr
import uvicorn

# ZeroGPU refuses to start unless at least one @spaces.GPU function is imported at startup.
import storyweaver.diagnostics  # noqa: F401
from storyweaver.api.main import create_app
from storyweaver.ui import build_demo

app = gr.mount_gradio_app(create_app(), build_demo(), path="/ui")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
