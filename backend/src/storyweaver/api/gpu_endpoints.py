"""GPU work exposed as Gradio API endpoints (ADR-0001, hybrid design).

ZeroGPU only works through Gradio: its startup hook runs inside Blocks.launch(), and GPU quota is charged
per caller using headers from the Gradio request. REST routes therefore never call @spaces.GPU functions;
callers use the Gradio API (POST /gradio_api/call/<name>, then GET the event stream) with X-API-Key, plus an
HF token in Authorization so ZeroGPU charges the token owner's quota.
"""

from typing import Any

import gradio as gr

# Importing registers the @spaces.GPU functions; ZeroGPU requires them to exist at startup.
from storyweaver.api.deps import INVALID_API_KEY_MESSAGE, api_key_is_valid
from storyweaver.config import get_settings
from storyweaver.diagnostics import run_gpu_matmul


def _require_api_key(request: gr.Request) -> None:
    if not api_key_is_valid(request.headers.get("x-api-key"), get_settings()):
        raise gr.Error(INVALID_API_KEY_MESSAGE)


def gpu_check(request: gr.Request) -> dict[str, Any]:
    _require_api_key(request)
    result: dict[str, Any] = run_gpu_matmul()
    return result


def register_gpu_endpoints(server: gr.Server) -> None:
    # One GPU job at a time: ZeroGPU quota is scarce and the local 6 GB GPU fits one model.
    server.api(fn=gpu_check, name="gpu_check", concurrency_limit=1)
