# ADR-0001: Running GPU work on a ZeroGPU Space alongside a FastAPI REST API

- **Status:** Accepted (14 Oct 2026). Live verification on the Space is pending; see "Live results".
- **Deciders:** project owner

## Context

Free HF accounts can host ZeroGPU Spaces only with the **Gradio SDK** (no Docker or CPU Spaces). The plan's
REST design (§6.8) assumed custom FastAPI routes could call `@spaces.GPU` functions.

Known constraints (HF ZeroGPU docs, checked 13 Oct 2026): Gradio 4+, torch 2.8.0–2.13.0, Python 3.12.12,
60 s default GPU duration, free hosts need a verified email and an account older than 30 days (max 2 Spaces).
Daily GPU quota per caller: anonymous about 2 min, free account 5 min, PRO 40 min.

## Spike findings (V0, 13–14 Oct 2026)

1. **v0.0.1–v0.0.3** used `FastAPI` + `gr.mount_gradio_app` + `uvicorn.run`. After a dependency fix, the Space
   built, then stopped with `RUNTIME_ERROR: No @spaces.GPU function detected during startup`. Importing the
   decorated function at startup did not help.
2. **Root cause, from the `spaces` 0.51.3 source:** ZeroGPU's startup step (`spaces/zero/__init__.py`, `startup()`)
   is attached to `gr.Blocks.launch()` through `gradio.one_launch(startup)`. It never runs unless Gradio's
   `launch()` starts the server.
3. **Quota attribution** (`spaces/zero/client.py`): each GPU call is scheduled using headers taken from the
   Gradio request (an `x-ip-token`, or an HF token). A GPU call from a plain FastAPI route has no Gradio
   request, so it cannot be charged to a known caller.
4. **Gradio 6 "Server mode"** (`gr.Server`, public and documented) is a `FastAPI` subclass. Its `launch()`
   builds an internal `Blocks(mode="server")` and calls `blocks.launch(_app=self)`, so ZeroGPU's startup step
   runs. FastAPI routers, middleware and exception handlers work on it unchanged (verified locally).

## Options considered

| Option | Verdict |
|---|---|
| A. Keep `uvicorn.run`; call ZeroGPU's internal `startup()` ourselves | Rejected: relies on an undocumented internal API, and GPU calls from REST routes still have no caller for quota |
| B. Gradio-only: move the whole API to Gradio endpoints | Rejected: gives up REST conventions (status codes, RFC 7807, idempotency) for the large majority of routes that need no GPU |
| **C. Hybrid** | **Accepted** |

## Decision: hybrid

- The app is a **`gr.Server`** (`storyweaver/api/main.py`), started with **`server.launch()`** (`backend/app.py`).
  `uvicorn.run` is not used.
- **REST stays FastAPI**: `/health/*`, `/v1/*`, and later stories, jobs and speech. These use the same routers,
  `X-API-Key`, RFC 7807 errors, `X-Request-ID` and CORS.
- **GPU work is never a REST route.** Each `@spaces.GPU` function is exposed only as a **Gradio API endpoint**
  (`storyweaver/api/gpu_endpoints.py`, registered with `server.api(...)`, `concurrency_limit=1`). Each endpoint
  checks the same `X-API-Key` through `api_key_is_valid()`.
- **Callers** (the Next.js server, `frontend/lib/gradio.ts`) use Gradio's HTTP API:
  `POST /gradio_api/call/<name>` returns `{event_id}`; then `GET /gradio_api/call/<name>/<event_id>` streams
  events ending in `complete` or `error`. They send `X-API-Key`, plus `Authorization: Bearer <HF_TOKEN>` so
  ZeroGPU charges the token owner's quota. This needs no new npm dependency.
- The Gradio `/ui` page was removed; the Next.js app is the UI.

## Consequences

- **Positive:** uses Gradio's documented path for ZeroGPU; REST conventions are kept for most routes; local
  Docker, CI and the Space run the same code; GPU quota can be attributed to an account.
- **GPU budget:** Vercel must hold a read-only `HF_TOKEN`. With a free account that is 5 GPU-min/day for the
  whole app, which is enough for V0–V2 diagnostics. Image generation (V3) will need careful budgeting or PRO.
- **CORS:** Gradio adds its own CORS middleware, which reflects *any* Origin (with credentials) when the host
  is not localhost, and only defers to a parent app's `CORSMiddleware`, which Server mode never passes. Found
  live on v0.0.4. `StoryWeaverServer` (in `api/main.py`) skips that middleware, so our `CORSMiddleware` is the
  single policy. The smoke test simulates the Space host (`Host: *.hf.space`) to guard this.
- **Gradio dependencies:** HF installs `gradio[oauth,mcp]` on every Gradio Space; our lock declares the same
  extras, and CI resolves the Space's install set (this caught the pydantic conflict in v0.0.2).
- **Open question for V3:** image jobs are started by the in-process job worker, not by a caller, so there is
  no request whose quota ZeroGPU can charge. Options to evaluate in V3: the frontend triggers per-scene Gradio
  calls; or the worker calls its own Gradio endpoint over HTTP with the Space's HF token. Decide in ADR-000x.
- **Errors:** `gr.Error` messages (such as an invalid API key) are shown to the caller; Gradio logs the
  traceback server-side. `show_error=False` hides unexpected exception details.

## Live results (fill in after deploying)

| Check | Result |
|---|---|
| Space reaches **Running** (no startup error) | |
| `GET /health/live` | |
| `GET /v1/groq-check` with `X-API-Key` | |
| Gradio `gpu_check` with `X-API-Key` + HF token: completes, `ok: true`, device name | |
| Gradio `gpu_check` without key: `error` | |
| Quota charged to the HF token owner (HF settings, ZeroGPU usage) | |
| Cold start after sleep | |
