---
title: StoryWeaver
emoji: 📖
colorFrom: purple
colorTo: yellow
sdk: gradio
sdk_version: 0.0.0  # replaced at deploy time with the gradio version from uv.lock
python_version: "3.12.12"
app_file: app.py
pinned: false
short_description: Voice-first illustrated children's audiobook generator
---

# StoryWeaver backend

FastAPI API with Gradio mounted at `/ui`, running on a ZeroGPU Space. This folder is pushed to the Space by
`.github/workflows/deploy-backend.yml`. Hardware (ZeroGPU) is selected in the Space settings, not here.
