# ADR-0001: FastAPI routes with `@spaces.GPU` on a ZeroGPU Space

- **Status:** Proposed (spike pending)
- **Date:** 13 Oct 2026

## Context

Free HF accounts can host ZeroGPU Spaces only with the **Gradio SDK** (no Docker/CPU Spaces). HF documents
`@spaces.GPU` for Gradio event handlers; it does not document calling it from custom FastAPI routes served by
`uvicorn` with Gradio mounted (`gr.mount_gradio_app`). StoryWeaver's REST design (§6.8) depends on that.

Known constraints (HF ZeroGPU docs, checked 13 Oct 2026): Gradio 4+, torch 2.8.0–2.13.0, Python 3.12.12,
default GPU duration 60 s, free hosts need a verified email and an account older than 30 days, max 2 Spaces.

## Spike

Deploy V0 and record results here:

| Check | Result |
|---|---|
| Space builds and `/health/live` returns 200 | |
| `GET /v1/gpu-check` (FastAPI route) returns `ok: true` with a CUDA device | |
| `/ui` "Run GPU check" button (Gradio event) returns `ok: true` | |
| Gradio API `gpu_check` via `@gradio/client` from a Next.js route handler | |
| Cold start time after sleep | |
| Quota charged to whom when called server-to-server (anonymous ≈ 2 min/day) | |

## Decision

_To be completed after the spike._

- If the FastAPI route works: keep REST as designed.
- If only the Gradio path works: expose GPU work via `gr.api` endpoints and call them from Next.js route
  handlers with `@gradio/client`; keep non-GPU REST routes in FastAPI.

## Consequences

_To be completed._
