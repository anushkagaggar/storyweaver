# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

StoryWeaver is a voice-first, illustrated children's audiobook generator (text ↔ image ↔ speech). It is also a learning project covering an MCP server, data collection, LLM fine-tuning, RAG and system design.

`plan.md` is the source of truth. **V0 is done (14 Oct 2026):** frontend live on Vercel (`https://storyweaver-eight-woad.vercel.app`, root dir `frontend`), backend on the ZeroGPU Space `gaggaranushka/storyweaver` (`https://gaggaranushka-storyweaver.hf.space`). V1+ are still designs.

Deploys: pushing to `main` redeploys Vercel; pushing a `v*` tag runs CI and then uploads `backend/` to the Space. Vercel env: `STORYWEAVER_API_URL` (the `*.hf.space` URL, never `huggingface.co/spaces/...`), `STORYWEAVER_API_KEY`, and `HF_TOKEN` (read-only; GPU quota). The write HF token lives only in GitHub Actions secrets. Space Secrets: `STORYWEAVER_API_KEY`, `GROQ_API_KEY`; Variables: `APP_ENV=prod`, `ALLOWED_ORIGINS` (Vercel domain only, no trailing slash). Before you implement anything, read the matching version section of `plan.md` (§10). Each version has its own task list and exit criteria. <!-- pragma: allowlist secret (names only, no values) -->

Two deviations from the plan's layout:
- Backend tests live in `backend/tests/`, not the root `tests/`.
- `backend/requirements.txt` isn't committed. The deploy workflow generates it from `uv.lock`, and also writes the locked Gradio version into the `sdk_version` field of `backend/README.md`.

`backend/` sets `package = false` in uv. Imports resolve through `pythonpath`/`mypy_path` = `src` and through `sys.path` in `app.py`. `[tool.uv] exclude-newer` pins dependency resolution to releases at least two weeks old; move that date forward deliberately. ZeroGPU supports torch 2.8.0–2.13.0 and Python 3.12.12, so don't bump `torch` beyond that range.

## Stack (fixed by the plan)

- **Backend:** Python 3.12 (managed with `uv`). The app is a **`gr.Server`** (Gradio Server mode, a FastAPI subclass) started with `server.launch()`; see the ZeroGPU rule below. It deploys to a **Hugging Face ZeroGPU Gradio Space**, which is why there is no Docker Space and `backend/Dockerfile` is only a local mirror of the Space.
- **Frontend:** Next.js (App Router) and TypeScript, deployed to **Vercel Hobby**.
- **MCP server:** FastMCP in `mcp_server/`. It uses stdio until V9, then Streamable HTTP on the Space.
- **Data and infrastructure:** Neon Postgres with pgvector, Upstash Redis, Cloudflare R2 (SeaweedFS S3 locally, because MinIO images are no longer published; the planned `MinioBlobStore` becomes a generic S3 adapter serving both), Langfuse, Grafana Cloud, MLflow, DVC + DagsHub and Prefect OSS.
- **Windows host:** run everything in Linux containers. Celery isn't supported on Windows, so the plan uses an in-process async worker. Keep the repo, caches, datasets and the Docker disk on **D:**, because C: is nearly full.

## Commands

```powershell
# backend/ (uv; add --extra gpu for torch, which is CPU-only on Windows, so use Docker for the GPU)
uv sync
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
uv run pytest -m unit                                            # CI gate today
uv run pytest tests/unit/test_groq_check.py::test_groq_check_success   # single test
uv run python app.py                                             # :7860 (/docs locally; Gradio API under /gradio_api)

# frontend/
npm run dev ; npm run lint ; npm run typecheck ; npm test ; npm run build   # npm test = Vitest (tests/*.test.ts)

# mcp_server/ (own uv project; thin adapter over /v1, plan §6.7)
uv run pytest ; uv run mcp dev server.py   # tests (real MCP protocol in memory, API mocked); MCP Inspector

# repo root: full local stack (api with GPU, web, postgres+pgvector, redis, SeaweedFS S3 on :8333)
docker compose -f infra/docker-compose.yml --env-file .env up --build
bash scripts/local_smoke.sh .env                                 # 33 end-to-end checks (REST, Gradio GPU endpoint, frontend); exits non-zero on failure
```

The user's rule is to test locally (unit tests plus the smoke script against the Docker stack) before deploying anything.

Planned for later versions: the gate becomes `uv run pytest -m "unit or integration or contract"`, `npx playwright test` gets added, and the MCP Inspector runs via `mcp dev mcp_server/server.py`.

Pre-commit runs ruff, mypy (both as local hooks through `uv run`) and detect-secrets (needs `.secrets.baseline`). Backend `/v1/*` routes require `X-API-Key`, and errors are RFC 7807 `application/problem+json` (`api/errors.py`). The Next.js route handlers in `frontend/app/api/` are the only callers that hold the API key (`lib/backend.ts`). Integration tests mock Groq with `respx`; each model also gets one opt-in live test. From V8 onwards, the red-team suite (`tests/fixtures/redteam.jsonl`) is a CI gate and must pass at 100%.

## Architecture rules

1. **Groq free tier first.** Use Groq for every AI task it offers on the free tier: text (`openai/gpt-oss-20b` planner, `gpt-oss-120b` teacher/judge/repair), vision, Whisper STT, Orpheus TTS, Prompt Guard and gpt-oss-safeguard. Use open-weight models only for image generation, embeddings, re-ranking, the self-trained Qwen3-4B, and fallbacks.
2. **Every external model sits behind an interface with a fallback adapter** (§4.1). Examples: `LLMClient`, `STTEngine`, `TTSEngine`, `SafetyClassifier`, `ImageGenerator`, `BlobStore`, `RateLimiter`. The same code runs locally and in production; only `.env` differs. Groq Preview models can disappear without notice, so a model returning 404 must fail loudly in contract tests.
3. **No gated, non-commercial or GPL models.** §2.2 lists the excluded models. Record licences in the model cards.
4. **Quota-aware routing** (`routing/`). `ModelRouter.reserve(model, est_tokens)` checks Redis token buckets that mirror Groq's per-organisation limits (`quotas.py`, which is config-driven). It returns a fallback when a limit is near. The TTS engine (Orpheus or Kokoro) is chosen **once per story**. Free-tier planner tokens per day are the bottleneck, at about 50–60 stories a day.
5. **Async job flow** (§6.4). `POST /v1/stories` with an `Idempotency-Key` returns `202 {story_id, job_id}`. The worker runs `plan`, which does RAG, then the planner, then the safeguard check, then enqueues per-scene `tts`/`image` jobs, scene 1 first. The UI polls. A failed job retries up to 3 times with backoff, then degrades to a placeholder image or Kokoro audio. Don't proxy long jobs through Vercel route handlers. Keep GPU calls under 60 s.
6. **Story JSON is the planner contract** (§6.1). The planner uses Groq strict JSON-schema mode. Pydantic validates it: 5–6 scenes, 30–120 words each, Flesch-Kincaid grade ≤ 4, and valid character IDs. A failed validation gets exactly one repair call to gpt-oss-120b, then a clean error.
7. **The MCP server is a thin adapter over the REST API.** It holds no business logic (§6.7).
8. **API conventions** (§6.8): `/v1` prefix, `X-API-Key` (sent only from Vercel server-side code), RFC 7807 errors, `X-Request-ID`, async handlers, and CORS limited to the Vercel domain and localhost.
9. **Migrations** use Alembic and must be expand/contract only (backward-compatible). The Space runs `alembic upgrade head` on start-up, and a rollback never needs a down-migration.
10. **ZeroGPU hybrid (ADR-0001).** ZeroGPU only works through Gradio: its startup hook runs inside `Blocks.launch()`, and GPU quota is charged per caller from Gradio request headers. So:
    - Start the app with `server.launch()`, never `uvicorn.run`.
    - **Never call a `@spaces.GPU` function from a REST route.** Expose GPU work only as Gradio API endpoints in `api/gpu_endpoints.py` (`server.api(...)`), each checking `X-API-Key` via `api_key_is_valid()`.
    - Callers use Gradio's two-step HTTP API (`frontend/lib/gradio.ts`) and send an HF token in `Authorization`, so ZeroGPU charges that account.
    - `@spaces.GPU` modules must be imported at startup (`gpu_endpoints.py` imports them).
    - HF installs `gradio[oauth,mcp]` on the Space; CI resolves that install set, so keep the lock compatible.
    - Gradio's own CORS middleware reflects any origin on non-localhost hosts; `StoryWeaverServer` skips it so our `CORSMiddleware` is the only policy. Keep that override.
    - On HF the `*.hf.space` proxy adds permissive CORS to every response anyway, so **CORS is not an access control there; `X-API-Key` is**. Never expose the key to browsers or add cookie/ambient auth.
    - Open for V3: GPU work started by the job worker has no caller to charge quota to.
11. **Child safety and privacy** (§6.6):
    - Input checks run in this order: rules, then Prompt Guard, then gpt-oss-safeguard against `safety/policy.md`. The full output story is checked as well.
    - Audio is **never stored or logged**.
    - `STT_ENGINE=local` keeps voice processing on the Space.
    - Secrets go only in HF Space **Secrets** (not Variables, which are public) and Vercel env.

## Data and training

- Get Gutenberg text only through the catalogue CSV, `/robot/harvest` or a mirror. Never scrape the site.
- Scrapers must honour robots.txt and keep only PD/CC0 images.
- Synthetic SFT generation (`data_pipeline/flows/synth_generate.py`) must be resumable and rate-limited with the same token-bucket code as the backend.
- The local RTX 4050 has 6 GB of VRAM, so load one model at a time. SDXL LoRA and gpt-oss-20b training run on Colab or Kaggle, not locally.
- Log every training run to MLflow with the DVC data hash, and checkpoint to the HF Hub every 100–200 steps.
