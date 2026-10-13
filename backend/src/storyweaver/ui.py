"""Minimal Gradio UI. Also a fallback path for the ADR-0001 spike: Gradio-native events are ZeroGPU's
documented route, so if the FastAPI route fails on the Space and this button works, the problem is the
FastAPI + @spaces.GPU combination."""

from typing import Any

import gradio as gr


def _gpu_check() -> dict[str, Any]:
    from storyweaver.diagnostics import run_gpu_matmul

    result: dict[str, Any] = run_gpu_matmul()
    return result


def build_demo() -> gr.Blocks:
    demo = gr.Blocks(title="StoryWeaver diagnostics")
    with demo:
        gr.Markdown("# StoryWeaver diagnostics")
        button = gr.Button("Run GPU check")
        output = gr.JSON(label="Result")
        button.click(_gpu_check, outputs=output, api_name="gpu_check")
    return demo
