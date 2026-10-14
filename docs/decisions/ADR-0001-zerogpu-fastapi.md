# ADR-0001: Running GPU work on a ZeroGPU Space alongside a FastAPI REST API

- **Status:** Accepted and verified live (14 Oct 2026, v0.0.5).
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
- **HF's proxy overrides CORS anyway** (verified on v0.0.5, 14 Oct 2026): the `*.hf.space` edge answers
  preflights itself and adds `Access-Control-Allow-Origin: <any origin>` and `expose-headers: *` to every
  response, including paths that don't exist. It does not set `allow-credentials`. A Space cannot change this.
  **So on HF, CORS is not an access control: the `X-API-Key` is.** Rules that follow: the key never reaches
  a browser (only the Vercel server holds it), and the API never uses cookies or other ambient credentials.
  Our own CORS policy stays for local runs and any future non-HF hosting.
- **Gradio dependencies:** HF installs `gradio[oauth,mcp]` on every Gradio Space; our lock declares the same
  extras, and CI resolves the Space's install set (this caught the pydantic conflict in v0.0.2).
- **Open question for V3:** image jobs are started by the in-process job worker, not by a caller, so there is
  no request whose quota ZeroGPU can charge. Options to evaluate in V3: the frontend triggers per-scene Gradio
  calls; or the worker calls its own Gradio endpoint over HTTP with the Space's HF token. Decide in ADR-000x.
- **Errors:** `gr.Error` messages (such as an invalid API key) are shown to the caller; Gradio logs the
  traceback server-side. `show_error=False` hides unexpected exception details.

## Live results (v0.0.5, 14 Oct 2026)

| Check | Result |
|---|---|
| Space reaches **Running** on `zero-a10g` (no startup error) | ✅ Log: `POST device-api.zero/startup-report 200` |
| `GET /health/live`, `/health/ready` | ✅ 200 |
| `/v1/*` without key; `/docs`, `/openapi.json`; REST `/v1/gpu-check` | ✅ 401; 404; 404 |
| Gradio `gpu_check` without key | ✅ `event: error`, "Missing or invalid API key." |
| Gradio `gpu_check` with key + HF token | ✅ `ok: true`, NVIDIA RTX PRO 6000 Blackwell (MIG 2g.48gb), torch 2.13.0+cu130, matmul 87 ms, 2.6 s round trip |
| Gradio `gpu_check` with key, no HF token (anonymous quota) | ✅ `ok: true`, 1.9 s round trip |
| Quota attribution | ✅ With an HF token, the `schedule` call's token carries the HF user (container log) |
| CORS | App-level policy works; the HF proxy adds permissive CORS on top (see Consequences) |
| Anomaly | The first live call (with HF token) completed on the GPU (`release fail=false` 0.5 s after `schedule`), but its result never reached the client, which received only heartbeats. Not reproduced in two later calls. If it recurs, capture the container log; the frontend's 90 s timeout bounds the impact. |
| Cold start after sleep | Not yet measured |
