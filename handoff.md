# StoryWeaver handoff

This is the single document to read before continuing StoryWeaver, whether you're a person or an AI assistant.

- **Written:** 20 Oct 2026, at the end of V1.1.
- **Sources:** `plan.md` (the source of truth for scope), the git history, the code and the tests.
- **Where to look for more detail:** `plan.md` holds the original plan, research and sources. `CLAUDE.md` holds the condensed rules for AI assistants. `docs/decisions/` holds the ADRs.

---

## Contents

1. [Where things stand](#1-where-things-stand)
2. [Live services, accounts and secrets](#2-live-services-accounts-and-secrets)
3. [Working rules](#3-working-rules)
4. [Tech stack](#4-tech-stack)
5. [Architecture and repository map](#5-architecture-and-repository-map)
6. [How to run, test, release and roll back](#6-how-to-run-test-release-and-roll-back)
7. [V0: foundations and deployment spike (done)](#7-v0-foundations-and-deployment-spike-done)
8. [V1: text storyteller and MCP v1 (done)](#8-v1-text-storyteller-and-mcp-v1-done)
9. [V1.1: friendlier guardrails (done)](#9-v11-friendlier-guardrails-done)
10. [Decision log](#10-decision-log)
11. [Test inventory](#11-test-inventory)
12. [Known issues and open follow-ups](#12-known-issues-and-open-follow-ups)
13. [Future phases V2–V10, slice by slice](#13-future-phases-v2v10-slice-by-slice)
14. [Free-tier budgets and limits](#14-free-tier-budgets-and-limits)

---

## 1. Where things stand

| Version | Status | Tag | Date |
|---|---|---|---|
| V0 Foundations and deployment spike | ✅ done, live | `v0.0.1`–`v0.0.5` | 13–14 Oct 2026 |
| V1 Text storyteller + MCP v1 | ✅ done, live (all four exit criteria met) | `v1.0.0` | 15 Oct 2026 |
| V1.1 Friendlier guardrails | ✅ done, live | `v1.0.1` | 20 Oct 2026 |
| V2 Talking storybook (speech ↔ text) | ⏭ next; not started | — | — |
| V3–V10 | planned (`plan.md` §10, and section 13 below) | — | — |

**Git state:** `main`, `dev-v1` and tag `v1.0.1` all point at `f6ce5f7`, and everything is pushed.

**Next step:**
1. Do the live browser check of V1.1 (section 9.4), if you haven't already.
2. Optionally ship the "busy" auto-wait fix as `v1.0.2` (section 12).
3. Create `dev-v2` from `main` and start V2 slice 1 (section 13.V2).

**Live URLs**
- Frontend (Vercel, production): https://storyweaver-eight-woad.vercel.app
- Backend (HF ZeroGPU Space): https://gaggaranushka-storyweaver.hf.space (the Space page is `huggingface.co/spaces/gaggaranushka/storyweaver`)
- GitHub: https://github.com/anushkagaggar/storyweaver
- V1 report (Claude Doc): https://claude.ai/code/artifact/4098b8e7-97a8-4c26-9db9-76c80d056ad9

---

## 2. Live services, accounts and secrets

Every service is on a free tier, with no card on file. Values are never written down anywhere; this table lists **names only**.

| Service | Used for | Where its settings live |
|---|---|---|
| GitHub (public repo) | Code, CI (GitHub Actions) | Actions secret `HF_TOKEN` (**write** token; used only by the deploy workflow) |
| Hugging Face ZeroGPU Space `gaggaranushka/storyweaver` | Backend (`gr.Server`) | **Secrets:** `STORYWEAVER_API_KEY`, `GROQ_API_KEY`, `DATABASE_URL`, `REDIS_URL`. **Variables (public):** `APP_ENV=prod`, `ALLOWED_ORIGINS=https://storyweaver-eight-woad.vercel.app` (no trailing slash, no localhost) |
| Vercel Hobby (root directory `frontend`) | Frontend | Env: `STORYWEAVER_API_URL` (the `*.hf.space` URL, never `huggingface.co/spaces/...`), `STORYWEAVER_API_KEY`, `HF_TOKEN` (**read** token, so GPU quota is charged to the account) |
| Groq (free tier) | All LLM and safety models | Console: Orpheus terms accepted; **Global ZDR on** (Zero Data Retention), with Batch and Fine-tuning off |
| Neon Postgres (US East) | Production database | Direct (unpooled) connection string in the Space secret `DATABASE_URL`, pasted exactly as Neon shows it; the backend converts it for asyncpg |
| Upstash Redis (us-east-1, eviction off) | Quota counters | `rediss://` URL in the Space secret `REDIS_URL` |
| Langfuse | LLM tracing | Built but **off**; there are no keys yet |
| Cloudflare R2 | Planned blob storage (V2) | **Not set up.** It may ask for a card; check before V2 slice 3 |
| Claude Desktop (Microsoft Store install) | MCP client | `C:\Users\Anu\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` → `mcpServers.storyweaver`. It currently points at the local API; there's a backup `.bak-before-storyweaver` |

**Secret rules**
- Never print keys. Scripts read keys from `.env` without echoing them.
- Space **Variables** are public, so secrets only ever go in Space **Secrets**.
- The HF write token exists only as the GitHub Actions secret.
- The browser never sees `STORYWEAVER_API_KEY`. Only Next.js route handlers hold it (`frontend/lib/backend.ts`).

---

## 3. Working rules

These are the owner's standing rules; follow them in every session.

1. **The owner runs installs.** Never run `uv sync`, `npm install` or `docker compose up --build` on the owner's behalf.
   - You may edit `pyproject.toml` or `package.json` and run `uv lock`.
   - Run tools with `uv run --frozen --no-sync`.
2. **Test locally before deploying.** Unit + integration tests, the Docker stack, `scripts/local_smoke.sh` (33 checks) and a manual browser check all come before any tag or deploy.
3. **Ask before pushing.** End every reply that touches the repo by asking whether to push. Never push unasked.
4. **Free only.** Never choose anything that costs money or needs a card.
5. **Branches.** Each version is developed on `dev-vN`. To release, fast-forward `main` to `dev-vN` **without deleting `dev-vN`**.
6. **Order of release:** the backend tag (`vX.Y.Z`) first, then `main` (the frontend), so the live site never calls an API that lacks new fields.
7. **Guardrail changes are discussed before they're built.** For example, the owner asked to discuss the Tom and Jerry refusal before any change was made.
8. **Plain, short explanations** with steps; UK English.
9. **Commits** end with a `Co-Authored-By` line when an assistant writes them. CI also runs on `dev-*` branches.

---

## 4. Tech stack

### 4.1 Backend (`backend/`)

| Item | Choice / version | Notes |
|---|---|---|
| Language | Python 3.12 (exactly 3.12; ZeroGPU supports 3.12.12) | Managed with **uv**. `package = false`, and imports resolve through `pythonpath` / `mypy_path = src` and `sys.path` in `app.py` |
| App server | **`gr.Server`** (Gradio Server mode, a FastAPI subclass), started with `launch()` | Never use `uvicorn.run` (ADR-0001). `StoryWeaverServer` skips Gradio's own CORS middleware |
| Web framework | FastAPI ≥ 0.115 | REST `/v1`, RFC 7807 errors (`api/errors.py`), `X-Request-ID` middleware |
| Gradio | `gradio[oauth,mcp]` ≥ 5 (the Space installs exactly this set) | The deploy writes the locked Gradio version into `backend/README.md` `sdk_version` |
| ZeroGPU | `spaces` ≥ 0.51 | `@spaces.GPU` functions are exposed only as Gradio API endpoints (`api/gpu_endpoints.py`) |
| Settings | pydantic-settings ≥ 2.15 | `config.py`; secrets are `SecretStr` |
| LLM SDK | `groq` ≥ 1.7 | `adapters/groq_llm.py` |
| Database | SQLAlchemy 2 async + asyncpg; Alembic ≥ 1.16 | `storage/`; migrations in `backend/migrations/versions/` (`0001`, `0002`) |
| Redis | redis-py ≥ 5.2 | Lua scripts for atomic quota windows |
| Tracing | langfuse ≥ 4.15, < 5 | Off without keys |
| torch (optional extra `gpu`) | `torch==2.13.0` | ZeroGPU supports 2.8.0–2.13.0, so don't go higher. On Windows, PyPI torch is CPU-only; use Docker for the GPU |
| Dev tools | pytest ≥ 9.1, pytest-asyncio, respx, httpx, ruff, mypy | Pre-commit runs ruff, mypy (local hooks) and detect-secrets (`.secrets.baseline`) |
| Dependency freshness | `[tool.uv] exclude-newer = "2026-09-29T00:00:00Z"` | Releases must be at least two weeks old. Move the date forward on purpose, never by accident |
| `requirements.txt` | Not committed | The deploy workflow generates it from `uv.lock` |

### 4.2 Frontend (`frontend/`)

| Item | Version | Notes |
|---|---|---|
| Next.js (App Router) | ^16 | Pages: `/`, `/story/[id]`, `/status`. Route handlers: `/api/stories`, `/api/stories/[id]`, `/api/status` |
| React | ^19 | Components: `StoryForm`, `StoryViewer`, `StoryBook` |
| TypeScript | ^5.9 | `lib/story-types.ts` mirrors the backend's API shapes |
| Tests | Vitest ^5 (`vitest.config.mts`) | `tests/story-logic.test.ts` |
| Lint | ESLint 9 + eslint-config-next | |
| Node | 24.x | `package-lock.json` must be generated by **Linux** npm, because Vercel builds on Linux |

### 4.3 MCP server (`mcp_server/`)

- A separate uv project: `mcp` ≥ 1.30, < 2 (code verified against the 1.x API) plus `httpx`.
- Transport is **stdio** until V9, then Streamable HTTP on the Space.
- It is a thin adapter over `/v1` (plan §6.7) with no business logic.

### 4.4 AI models

**In use (all on the Groq free tier):**
- `openai/gpt-oss-20b`: the planner.
- `openai/gpt-oss-120b`: repair, teacher and judge.
- `meta-llama/llama-prompt-guard-2-22m`: prompt-injection check.
- `openai/gpt-oss-safeguard-20b`: policy check against our own policy.

**Planned:**
- Groq: `whisper-large-v3-turbo` (V2), `canopylabs/orpheus-v1-english` (V2), `qwen/qwen3.8-27b` vision (V5).
- Open-weight: Kokoro-82M, faster-whisper small (int8), SDXL base 1.0 + IP-Adapter, SD 1.5, Florence-2-base, bge-small-en-v1.5, bge-reranker-base, SigLIP base, and Qwen3-4B-Instruct-2507 (fine-tuned by us).

**Excluded:** gated, non-commercial and GPL models (plan §2.2).

### 4.5 Infrastructure

**Production:**
- HF ZeroGPU Space (hardware `zero-a10g`; sleeps after 48 h idle).
- Neon (scales to zero after 5 minutes idle).
- Upstash.
- Vercel.
- GitHub Actions.

**Local:** Docker Desktop (WSL2, GPU) running `infra/docker-compose.yml`:
- `api`: built from `backend/Dockerfile` (a local mirror of the Space), with the GPU.
- `web`: `node:24-alpine`, which runs `npm ci && npm run dev`, with `frontend/` mounted.
- `postgres`: `pgvector/pgvector:pg16`.
- `redis`: `redis:7.4-alpine`.
- `s3`: `chrislusf/seaweedfs:4.48`, on port 8333. It replaced MinIO, whose images are no longer published.

**Machine:** RTX 4050 (6 GB VRAM), 16 GB RAM. **Keep everything on D:**, because C: is nearly full (`HF_HOME=D:\hf`).

---

## 5. Architecture and repository map

```
Browser ──► Vercel (Next.js) route handlers [hold STORYWEAVER_API_KEY]
              │ HTTPS + X-API-Key
              ▼
        HF ZeroGPU Space: gr.Server (FastAPI)
          REST /v1  ─► orchestrator ─► services ─► adapters ─► Groq
          Gradio API (GPU endpoints only, ADR-0001)
          in-process async job worker (jobs table)
          ModelRouter (Redis counters mirroring Groq limits)
              │                │               │
           Neon Postgres    Upstash Redis   (R2 from V2)
```

**One story, end to end (V1):**
1. The browser posts to `/api/stories`, which forwards to `POST /v1/stories` with an `Idempotency-Key`.
2. **Input safety runs synchronously:** rules, then Prompt Guard, then safeguard.
   - An unsafe idea gets 422 and is **never stored**.
   - A brand idea is stored with `adaptation="brands"`.
3. The API inserts the story and a `plan` job, and returns **202** `{story_id, job_id}`.
4. The worker claims the job (`FOR UPDATE SKIP LOCKED`), and the router reserves quota.
5. The planner calls gpt-oss-20b in strict JSON mode, then validates the result. If validation fails, it makes **one** repair call to gpt-oss-120b.
6. The story is saved with status `checking` (content still hidden), and the **output** safety check runs on the full story.
7. The status becomes `ready`. The browser polls `GET /v1/stories/{id}` (every 1.5 s, backing off to 8 s) and shows the pages.

**Story statuses:** `queued → planning → checking → ready | failed`.
**Job statuses:** `queued | running | succeeded | failed`. Jobs also have `attempts`, `run_after` and `locked_at`.

**Key backend files**

| Path | Purpose |
|---|---|
| `backend/app.py` | Space entrypoint: runs migrations (`configure_logger=False`), then `launch(app_kwargs={"lifespan": …})` |
| `src/storyweaver/api/` | `main.py`, `routers/{stories,health,…}.py`, `errors.py`, `deps.py`, `middleware.py`, `gpu_endpoints.py` |
| `domain/story.py`, `domain/readability.py` | The Story contract, `story_problems()`, our own Flesch-Kincaid, seeds, `idea_name_words()` |
| `llm/planner.py`, `llm/schemas.py`, `llm/prompts/*.md`, `llm/client.py` | Planner, strict schema, prompts, the `LLMClient` interface and errors |
| `adapters/groq_llm.py`, `adapters/langfuse_tracer.py` | Groq and Langfuse implementations |
| `routing/quotas.py`, `store.py`, `router.py`, `llm.py` | Free-tier limits, Redis/in-memory stores, `ModelRouter`, `RoutedLLM` |
| `jobs/queue.py`, `jobs/worker.py` | Postgres job queue and worker |
| `orchestrator/story_service.py` | `StoryService.create`, `PlanStoryJob`, the failure hook |
| `safety/policy.md`, `rules.py`, `guards.py` | Policy, rules, `InputGuard`, `OutputGuard`, per-category messages |
| `storage/db.py`, `models.py`, `repositories.py` | Engine (`normalize_database_url`), ORM rows, repositories |
| `observability/tracing.py` | `Tracer`, `NoopTracer`, `trace_scope` |
| `services.py` | Wires everything from settings (fails closed when there's no Groq key) |
| `evaluation/json_validity.py` (repo root) | V1 eval (resumable); results in `evaluation/results/` |

---

## 6. How to run, test, release and roll back

### 6.1 Commands

```powershell
# backend/
uv run --frozen --no-sync ruff check . ; uv run --frozen --no-sync ruff format --check . ; uv run --frozen --no-sync mypy
uv run --frozen --no-sync pytest -m unit
# integration tests need the local Postgres + Redis from Docker (the test DB name must end in _test; it is dropped and recreated):
#   bash: PW=$(grep -E '^POSTGRES_PASSWORD=' ../.env | cut -d= -f2- | tr -d '\r')
#   TEST_DATABASE_URL="postgresql+asyncpg://storyweaver:${PW}@localhost:5432/storyweaver_test" REDIS_URL=redis://localhost:6379/15 \
#     uv run --frozen --no-sync pytest -m "unit or integration"
# live (opt-in, uses Groq quota): tests/live/

# frontend/
npm run lint ; npm run typecheck ; npm test ; npm run build

# mcp_server/
uv run --frozen --no-sync pytest ; uv run mcp dev server.py   # MCP Inspector

# repo root: full local stack, then the 33-check smoke test
docker compose -f infra/docker-compose.yml --env-file .env up -d --build
docker compose -f infra/docker-compose.yml --env-file .env logs api --tail 30
bash scripts/local_smoke.sh .env      # PowerShell: & "C:\Program Files\Git\bin\bash.exe" scripts/local_smoke.sh .env
```

### 6.2 Release (every version; plan §11)

1. **Local gate:**
   - backend unit + integration tests;
   - frontend lint, typecheck, test and build;
   - MCP tests;
   - Docker stack + smoke test (33/33);
   - the version's exit criteria checked by hand in the browser.
2. **Push `dev-vN`** and wait for CI to pass. (Without `gh`, CI can be polled through `api.github.com/repos/anushkagaggar/storyweaver/actions/runs?head_sha=<sha>`.)
3. **Tag `vX.Y.Z`** on that commit and push the tag. `deploy-backend.yml` then:
   - re-runs CI;
   - exports `requirements.txt` from `uv.lock`;
   - pushes `backend/` to the Space with the GitHub `HF_TOKEN`.
4. **Check the Space:**
   - `https://huggingface.co/api/spaces/gaggaranushka/storyweaver/runtime` shows `RUNNING` and a new `sha`;
   - `/health/ready` shows `{"database":"ok","redis":"ok","worker":"running"}`;
   - unauthenticated `POST /v1/stories` returns 401, and `/docs` returns 404.
5. **Fast-forward `main`:** `git push origin origin/dev-vN:refs/heads/main` (keep `dev-vN`). Vercel deploys in about 20 s.
6. **Live browser check** of the exit criteria.

### 6.3 Rollback

- **Frontend:** Vercel → Deployments → **Instant Rollback**.
- **Backend:** re-run the deploy workflow on the previous tag (e.g. `v1.0.0`), or push that tag's `backend/`.
- **Database:** never runs a down-migration. All migrations are expand-only, so older code keeps working on the newer schema.

---

## 7. V0: foundations and deployment spike (done)

**Goal:** prove the hosting design end to end. **Exit criterion:** the live Vercel page shows GPU OK and Groq OK from the live Space. ✅ Met on 14 Oct 2026, with `v0.0.5`.

### 7.1 What was built (commit by commit)

| Commit / tag | What |
|---|---|
| `764841d` / `v0.0.1` | Monorepo skeleton. Pre-commit (ruff, mypy, detect-secrets), `.env.example`. Backend: `/health/live`, `/health/ready`, `/v1/echo`, `/v1/groq-check` (a 1-token gpt-oss-20b call), and an `@spaces.GPU` torch-matmul check. Next.js status page. Docker compose. CI and the deploy workflow |
| `799b581` | OpenAPI/`/docs` hidden in production; `scripts/local_smoke.sh` |
| `97a98c8` | The deploy script reports each misconfiguration explicitly |
| `b28e920` / `v0.0.2` | **Space build fix:** the Space installs `gradio[oauth,mcp]`, whose mcp extra caps pydantic. The lock now declares those extras, so it matches what the Space resolves. CI checks the Space can install our requirements |
| `fecaf4a` / `v0.0.3` | `@spaces.GPU` functions are imported at startup. **This was not enough;** see the spike |
| `dc75b64` / `v0.0.4` | **ADR-0001, the hybrid design** (`gr.Server` + `launch()`, GPU work only through Gradio API endpoints) |
| `3196426` / `v0.0.5` | A single CORS policy: Gradio's `CustomCORSMiddleware` is skipped |
| `4dba089`, `2c94914`, `73aa0ec` | Docs: the HF proxy adds permissive CORS anyway, so the API key is the access control. Live verification. V0 complete |

### 7.2 Decisions in V0

- **ADR-0001: ZeroGPU hybrid.**
  - **The problem:** FastAPI + `gr.mount_gradio_app` + `uvicorn.run` failed with `No @spaces.GPU function detected during startup`. ZeroGPU's startup hook runs only inside `Blocks.launch()`, and GPU quota is charged per caller from Gradio request headers.
  - **The decision:**
    - The app is a `gr.Server`, started with `launch()`.
    - REST stays FastAPI.
    - GPU work is exposed **only** as Gradio API endpoints, each checking `X-API-Key`.
    - Callers use Gradio's two-step HTTP API (`frontend/lib/gradio.ts`) and send an HF token, so ZeroGPU charges that account.
  - **Still open for V3:** GPU work started by the background worker has no caller to charge quota to.
- **CORS is not access control on HF.** The `*.hf.space` proxy reflects any Origin. So the `X-API-Key`, held only by the Vercel server, is the access control. There are no cookies or ambient credentials.
- **SeaweedFS replaces MinIO** for local S3; MinIO's Docker images were withdrawn.
- **In-process async worker** instead of Celery, because Celery doesn't run on Windows.
- **OpenAPI hidden in production** (`/docs` and `/openapi.json` return 404).

### 7.3 Tests and edge cases in V0

- **Unit tests:**
  - `test_health_and_echo.py` (7): health needs no key; `/v1` rejects a missing or wrong key; request IDs are propagated, and malformed ones replaced; validation errors are problem+json; docs are hidden in production.
  - `test_gpu_endpoints.py` (5): key checks; the GPU check runs with a valid key; Gradio's CORS middleware is skipped; there is no REST GPU route.
  - `test_groq_check.py` (4): success; 429 with `Retry-After`; an auth failure becomes 502 without leaking details; 503 without a key.
- **Smoke script** (33 checks today): health; echo with no key / wrong key / valid; the 401 body is problem+json; echo validation (empty message, 1,001 characters, malformed JSON); `X-Request-ID`; CORS (localhost allowed; another origin, the Gradio route and the Space host give no ACAO header); unknown route → 404; groq-check; `/docs` available locally; the REST gpu-check route is removed; `gpu_check` works with the key, runs on CUDA, and fails without the key or with a wrong one; the frontend pages; the V1 story routes; status checks; the **API key never appears in browser HTML/JS**.

---

## 8. V1: text storyteller and MCP v1 (done)

**Exit criteria (plan §10), all met:**
1. **JSON valid for ≥ 95% of 50 prompts:** 50/50 (45 on the first try, 5 after repair; 144,310 tokens). Results in `evaluation/results/json_validity_v1.md`.
2. **Unsafe prompts refused:** verified live, e.g. a URL idea → 422 `unsafe_input`.
3. **MCP tools work in Claude Desktop:** "Penny the Penguin's New Friend" was created and read back.
4. **Deployed live:** `v1.0.0`, with `main` fast-forwarded on 16 Oct 2026.

### Slice 1: Story contract and Groq planner (`1edc23a`)

**Built:**
- **`domain/story.py`:** Pydantic models (`extra="forbid"`, frozen).
  - `Character`: `id` (lower snake case, ≤ 32 characters), `name`/`species` (≤ 40), `visual` (10–300), `seed`, `voice` (filled in V2).
  - `Line`: `speaker`, `text`.
  - `Scene`: `idx`, `text`, `image_prompt`, `characters`, `lines` (≥ 1).
  - `Story`: `title` (≤ 80), `age_band`, `moral` (≤ 200), 1–6 `characters`, `setting` (≤ 300), `scenes`.
- **`story_problems()`:** the business rules, returned as a readable list that feeds the repair call.
  - exact scene count;
  - `idx` values are 1..n;
  - character IDs are unique, and `narrator` is reserved;
  - words per scene (now 30–120);
  - known character IDs and speakers;
  - Flesch-Kincaid ≤ 4 on the **whole** story.
- **`domain/readability.py`:** our own Flesch-Kincaid with a syllable heuristic; no extra dependency, and deterministic.
- **Seeds:** `character_seed()` = SHA-256 of `story_key:character_id`, 31 bits. They're assigned **server-side**, never by the LLM, for V3 image consistency.
- **`llm/schemas.py`:** `strict_json_schema()` makes every field required with `additionalProperties: false` and no unsupported keywords. `PlannedStory` is the LLM-facing shape, without seeds.
- **`llm/planner.py`:** a `StoryPlanner` call to gpt-oss-20b, then **exactly one** repair call to gpt-oss-120b carrying the problems list, then `PlanningError`.
- **`adapters/groq_llm.py`:**
  - 429 → `LLMRateLimitedError(retry_after)`.
  - 404 → `LLMUnavailableError`, logged loudly (a withdrawn model).
  - 5xx or connection failure → unavailable.
  - `finish_reason == "length"` → error.

**Decisions:**
- Only the 4–6 age band.
- 5 or 6 scenes (default 5).
- Reading level is checked on the whole story, because single short scenes give noisy scores.
- Seeds are assigned server-side.

**Tests:**
- `test_story_rules.py`: valid story; deterministic seeds; scene count; indices; word limits plus repair advice; unknown characters and speakers; duplicate and reserved IDs; reading level; field limits; syllables; FK ordering.
- `test_strict_schema.py`: every object is strict; no server-owned fields.
- `test_planner.py`: no repair when valid; one repair with the teacher model; failure after repair; unparseable JSON is repaired; 6 scenes.
- `test_groq_llm.py`: strict schema and usage; rate limit; withdrawn model; bad request and truncation.

### Slice 2: Postgres and migration 0001 (`42440c6`)

**Built:**
- **Migration 0001** creates four tables:
  - `stories`: prompt, `age_band`, `num_scenes`, `status`, `title`, `moral`, `setting`, `tts_engine`, `error_code`/`error_detail`, `idempotency_key` (unique), `request_hash`; indexes on `status` and `created_at`.
  - `characters`: `char_key`, `position`, `name`, `species`, `visual_desc`, `voice_id`, `seed`, `ref_image_key`; unique on `(story_id, char_key)`.
  - `scenes`: `idx`, `text`, `image_prompt`, `character_keys` (array), `lines` (JSONB), `image_key`/`audio_key`, `status`; unique on `(story_id, idx)`.
  - `jobs`: `kind`, `story_id`, `status`, `attempts`, `last_error`, `payload` (JSONB), `payload_hash` (unique), `run_after`, `locked_at`; indexes on `(status, run_after)` and `story_id`.
- **`storage/repositories.py`:** async repositories. A plan is saved in one transaction, and `save_plan` replaces any earlier plan, so retries are safe.
- **CI** gained a Postgres service.

**Decisions:**
- Expand-only migrations.
- `position` keeps the planner's character order.
- Integration tests run on **real Postgres**, never SQLite.

**Tests (`test_storage.py`):** models match the migration; expected indexes exist; create and find by idempotency key; the key is unique; a plan round-trips; a second plan replaces the first; `mark_failed` and cascade delete; downgrade then upgrade.

### Slice 3: Quota-aware model router (`b4b3ac9`, `ea3df22`, `f31a719`)

**Built:**
- **`routing/quotas.py`:** `GROQ_FREE_TIER` limits per model (section 14). The margin is 0.9, but never below 1. An unknown model raises an error instead of running unlimited.
- **`routing/store.py`:** a Redis Lua script checks and reserves every window **atomically**. `InMemoryQuotaStore` covers tests and runs without Redis.
- **`routing/router.py`:** `ModelRouter.reserve` reserves quota, the reservation is corrected to actual usage afterwards, cooldowns apply, and `QuotaExhaustedError(retry_after)` is raised when nothing is available.
- **`routing/llm.py`:** `RoutedLLM` wraps any `LLMClient`, with 20b ↔ 120b fallback.
- **CI:** also runs on `dev-*` branches.

**Decisions:**
- Limits are per **organisation** (shared by every API key).
- The router is invisible to the planner, so the same code runs everywhere.

**Tests:**
- `test_router.py`: limits table; margin; requests per minute; burst at a boundary; returning unused tokens; fallback; cooldown; daily limit; shortest wait reported.
- `test_routed_llm.py`: conservative estimate; settle actual tokens; fallback on rate limit or unavailability; all failing; no quota anywhere means no Groq call.
- `test_quota_redis.py`: atomic reserve; window expiry; corrections affect only token counters; cooldown; rolling window.

### Slice 4: Job queue, worker and REST API (`53b5f4a`)

**Built:**
- **`jobs/queue.py`:**
  - claims jobs with `SELECT … FOR UPDATE SKIP LOCKED`;
  - `MAX_ATTEMPTS = 3`; backoff of 5 s, then 20 s (5 × 4ⁿ);
  - `defer` reschedules without using an attempt (for quota waits);
  - stale `running` jobs are requeued after 600 s at worker start.
- **`jobs/worker.py`:**
  - an in-process async loop that polls every 1 s;
  - failures are either retryable or `PermanentJobError`;
  - users only ever see the generic message "The story could not be generated. Please try again."
- **`orchestrator/story_service.py`** and **`services.py`** (dependency wiring). The lifespan is passed through `launch(app_kwargs=…)`.
- **Endpoints:**
  - `POST /v1/stories` → **202**, with `Location`.
  - The `Idempotency-Key` (8–128 characters) replays the same story; the same key with a different body returns **422**.
  - `GET /v1/stories/{id}` (content shown only when `ready`) and `GET /v1/jobs/{id}`.
  - `/health/ready` checks the database, Redis and worker.
- **`app.py`** runs `alembic upgrade head` at boot.

**Decisions:**
- No database transaction is held while an LLM runs.
- A deferral doesn't use an attempt.
- A retry after a deferred output check re-checks the saved plan instead of planning again.

**Tests (`test_stories_flow.py`, part):** 202 and queued; idempotency replay and conflict; invalid requests → 422; unknown IDs → 404 and bad IDs → 422; worker end to end; a planning failure fails the story without a retry; quota exhaustion defers without using an attempt; upstream errors retry, then fail; concurrent claims never take the same job; enqueue is idempotent and stale jobs are requeued.

### Slice 5: Child safety (`df68084`)

**Built:**
- **Input checks**, cheapest first:
  1. `safety/rules.py`: regexes for emails, URLs, phone numbers or long numbers, and a narrow list of blocked words. Word boundaries keep "killer whale", "heroine" and "shooting star" allowed.
  2. Prompt Guard 2 (score ≥ 0.5 blocks).
  3. gpt-oss-safeguard against `safety/policy.md`.
- **Output check:** `OutputGuard` sends the whole finished story (reader-visible text only; image prompts are left to V3's image safety check).
- **`checking` status:** content stays hidden until the output check passes.
- **Fail closed:**
  - unsafe → 422 `unsafe_input`, never stored;
  - quota → 429 with `Retry-After`;
  - guard unavailable → 503.
- **Sliding-window quota estimate,** needed for safeguard's tiny limits.

**Decisions:**
- Policy categories: violence, death, romance, sexual, self_harm_or_drugs, hate, real_people, brands, urls, personal_data, scary, unhappy_ending. The owner reviewed them.
- **Option A:** two safeguard checks per story (input + output), giving about one story every 1–2 minutes.
- The policy is kept at 400–650 tokens (Groq's recommendation; there's a unit test for it).

**Tests:**
- `test_safety.py`: rules allow ordinary ideas and block clear violations; all three stages; rules run before any model call; injection stops before the policy check; category reported; unusable guard output fails closed; the output guard reviews only reader-visible text; policy size.
- `test_stories_flow.py`: unsafe ideas rejected and never stored (no rows); guards unavailable → 429/503 with `Retry-After`; an idempotent replay skips the guard; a rejected output fails the story and hides its content; a deferred output check resumes without re-planning.
- Live: `tests/live/test_safety_live.py`.

### Slice 6: Frontend storybook (`0f7fbce`)

**Built:**
- **Pages:** `/` (`StoryForm`), `/story/[id]` (`StoryViewer`, which polls; then `StoryBook`, with page navigation and arrow keys), and `/status`.
- **Route handlers:** `/api/stories` and `/api/stories/[id]` forward only the allowed fields. They validate the idempotency key and story-ID format.
- **`lib/story-logic.ts`:** pure helpers (polling delay, status messages, prompt validation, problem messages, `Retry-After` parsing, UUID check).
- **One Idempotency-Key per distinct request,** so a double-click never creates two stories.

**Decisions:**
- The key stays server-side.
- `package-lock.json` must come from Linux npm.

**Tests:** Vitest `story-logic.test.ts` (7 now); smoke checks for the pages and story routes.

### Slice 7: MCP server v1 (`f3d2503`, `e0ae426`)

**Built:**
- **Tools:** `create_story`, `get_story`, `get_job_status`.
- **Resource:** `story://{story_id}`.
- **Prompt:** `bedtime_story`.
- Results include a plain-English `progress` note and `check_again_in_seconds`, so the assistant polls sensibly.
- A CI `mcp` job.

**Decision:** a thin adapter only; every rule lives in the API.

**Tests (`mcp_server/tests/test_server.py`, 12):** lists everything; sends the API key and idempotency key; an unsafe idea becomes a tool error with the API's message; busy → tells the assistant when to retry; arguments validated before calling the API; get story and job; bad IDs; API unreachable; the resource returns full JSON; the prompt guides the flow; in-progress explanations; a quota-deferred job is explained as normal.

### Slice 8: Langfuse tracing switch (`aff6943`)

**Built:**
- **`observability/tracing.py`:** a `Tracer` interface; `NoopTracer` by default; `trace_scope` groups one story's calls under one trace/session.
- **`adapters/langfuse_tracer.py`** (Langfuse 4.15.6).
- **Settings:** `LANGFUSE_PUBLIC_KEY` / `SECRET_KEY` / `HOST` / `CAPTURE_CONTENT`.

**Decisions:**
- Off unless keys are set.
- **Content capture is off by default** (privacy).
- A failing tracer never breaks a call.

**Tests (`test_tracing.py`, 8):** metadata only by default; content only when enabled; each attempt traced, with fallback flagged; errors traced; scope shared; a failing tracer is harmless; Langfuse generation fields.

### Slice 9: Production readiness and the eval (`2408f82`, `36ea2d7`)

**Built:**
- **`normalize_database_url`:** accepts Neon's URL exactly as pasted (`sslmode`, `channel_binding`) and converts it for asyncpg. Migrations use it too.
- **Live bug fixed:** Groq strict mode can still return 400 `json_validate_failed`. This now raises `LLMInvalidOutputError` carrying `failed_generation`, which goes through the normal repair call.
- **`evaluation/json_validity.py`:** resumable and quota-paced.
- **`docs/release-v1.md`.**
- detect-secrets excludes `tests/unit/test_database_url.py`.

**Decisions:**
- Use Neon's **direct** (unpooled) connection: PgBouncer transaction mode breaks asyncpg prepared statements and Alembic.
- Pool size 3 + 2.

**Tests:** `test_database_url.py`; `test_groq_llm.py` (json_validate_failed → invalid output with the partial generation; other 400s include Groq's reason); `test_planner.py` (a provider JSON failure goes through the repair call).

### V1 release facts
- **Cold-start baseline** (from Space logs, 15 Oct):
  - container start → app ready: **about 18 s**;
  - a story takes **about 7–11 s of work**, plus up to **about 35 s waiting** for the safety model's per-minute limit.
- **Groq ZDR** is on; every model still works with it.

---

## 9. V1.1: friendlier guardrails (done)

Shipped as `v1.0.1` (`5122472` + `f6ce5f7`) on 20 Oct 2026.

### 9.1 Why
- "Tom and Jerry" was refused with *"Try a gentle, kind idea"*, the same message a violent idea got. The backend knew the category (`brands`) but didn't pass it on.
- Live, 1 story in 9 failed: gpt-oss-20b writes 40–50 words per page, and the minimum was 40.

### 9.2 What changed (owner's decisions: option B + A, fixes 1 + 2, minimum 30 words)

1. **Brands redirect instead of refusing (ADR-0002).**
   - `policy.md`: a brand in a story **idea** is no longer a violation. Safeguard returns an extra field, `brand_mentioned` (0/1).
   - `InputGuard` returns `allowed` with `adaptation="brands"`. If the model still labels a brand as a violation in an idea, it's adapted anyway.
   - Any **other** violation in the same idea still refuses it ("Tom and Jerry hit each other with pans" → violence).
   - **Migration 0002** adds the nullable column `stories.adaptation` (expand only).
   - **Planner:** gets an explicit instruction to invent original characters. `story_problems(banned_name_words=…)` rejects a character name that reuses a word from the idea (`idea_name_words()` skips common words), which goes through the normal repair call.
   - **Output check unchanged:** a brand in a **finished** story is still a violation, so this fails closed.
   - **API:** `StoryOut.notice` = *"Your idea named characters we can't use, so we made up our own characters inspired by it."* (only when `ready`). The UI shows it above the book; MCP tells the assistant to share it first.
2. **Per-category refusal messages** (`INPUT_CATEGORY_MESSAGES` in `guards.py`, through `SafetyVerdict.message`). For example, real people: *"We can't make stories about real people. Try a made-up character instead, like a bunny who dreams of being a singer."* Rule matches show their own reason; unknown categories fall back to the generic message.
3. **Scene length:**
   - The prompt asks for **7–10 short sentences** (about 60–80 words), because models count sentences better than words.
   - Repair problems now say e.g. *"Add 4 more short sentences to it (aim for about 65 words)."* (`_length_fix`).
   - **The minimum is now 30 words** (plan §6.1 updated, with the reason).
4. **Safety-wait message:** while a story is `checking`, the page says *"Every story gets a safety check before you see it… this can take up to a minute."* (`statusHint`).

### 9.3 Verification
- **Live input guard: 7/7 correct.**
  - Adapted: Tom and Jerry, Peppa Pig, Elsa.
  - Refused: frying pans (violence), Taylor Swift (real people), "scary ghost" (scary).
  - Allowed: the dragon idea.
- **Live planner, 30-word minimum, 9 stories: 0 failed, 6 repaired.** With 40, one of nine failed. The Tom and Jerry story became "Milo and Pip's Moonlit Adventure", and the output check passed it.
- **Local:** 153 backend tests, 7 Vitest tests, 13 MCP tests, smoke 33/33, and a manual browser check of the four ideas all passed.
- **Production:** deploy succeeded; health all ok; 401 and 404 lockdown intact; the live API returns the new real-people message; Vercel served the V1.1 frontend.

### 9.4 Live browser check (owner)
On the live site, about a minute apart:
- `a story about Tom and Jerry` → new names and a notice.
- `Taylor Swift goes to the moon` → real-people message.
- `Tom and Jerry hit each other with frying pans` → violence message.
- `a little dragon who is scared of the dark` → a normal story, with the safety-wait message while it's checked.

### 9.5 New tests
- **Unit:**
  - brand idea adapted; brand labelled as a violation still adapted; brand + violence refused; brand in a finished story rejected; no adaptation for a safe idea;
  - each category explains itself;
  - adapted ideas get the instruction (and ordinary ideas don't); a borrowed name is sent to repair;
  - `idea_name_words`; banned words check only names;
  - repair advice (add or remove sentences).
- **Integration:** a brand idea is adapted and the reader is told; an ordinary story has no notice; the 422 detail is specific.
- **Vitest:** `statusHint`.

---

## 10. Decision log

| # | Date | Decision | Why | Where |
|---|---|---|---|---|
| D1 | 12 Oct | Groq free tier for every AI task it offers; open weights only where Groq has nothing | Free, fast; owner rule | plan §0 |
| D2 | 12 Oct | No gated, non-commercial or GPL models | Licence safety | plan §2.2 |
| D3 | 13 Oct | SeaweedFS replaces MinIO (local S3) | MinIO images withdrawn | plan §3 |
| D4 | 13 Oct | Lock declares `gradio[oauth,mcp]` | The Space installs that set; it caps pydantic | `b28e920` |
| D5 | 14 Oct | **ADR-0001:** `gr.Server` + `launch()`; GPU only through Gradio API endpoints | The ZeroGPU startup hook and per-caller quota | ADR-0001 |
| D6 | 14 Oct | The API key is the access control; Gradio CORS middleware skipped | The HF proxy reflects any Origin | `3196426`, `4dba089` |
| D7 | 14 Oct | Age band 4–6 only; 5 or 6 scenes (default 5) | Scope for V1 | Slice 1 |
| D8 | 14 Oct | FK ≤ 4 measured on the whole story; own FK implementation | Stable scores; no dependency | Slice 1 |
| D9 | 14 Oct | Seeds assigned server-side | Deterministic images in V3 | Slice 1 |
| D10 | 14 Oct | Exactly one repair call, to gpt-oss-120b | Plan §6.1; quota | Slice 1 |
| D11 | 14 Oct | Expand-only migrations; no down-migrations in production | Safe rollbacks | Slice 2 |
| D12 | 14 Oct | Quota margin 0.9 (never below 1); limits are per organisation | Headroom; Groq's rules | Slice 3 |
| D13 | 15 Oct | Postgres job queue with SKIP LOCKED; in-process worker; 3 attempts with 5 s/20 s backoff; deferral doesn't use an attempt | No Celery on Windows; simple | Slice 4 |
| D14 | 15 Oct | Idempotency-Key required for safe retries; same key with a different body → 422 | Correct retry semantics | Slice 4 |
| D15 | 15 Oct | Safety order: rules → Prompt Guard → safeguard; output check on the whole story; `checking` hides content; fail closed | Child safety | Slice 5 |
| D16 | 15 Oct | **Option A:** two safeguard checks per story (about one story every 1–2 minutes) | Safety over throughput | Slice 5 |
| D17 | 15 Oct | Rejected ideas are never stored | Privacy | Slice 5 |
| D18 | 15 Oct | `package-lock.json` from Linux npm | Vercel builds on Linux | Slice 6 |
| D19 | 15 Oct | MCP is a thin adapter; `check_again_in_seconds` | Plan §6.7 | Slice 7 |
| D20 | 15 Oct | Langfuse off by default; content capture off | Privacy; free tier | Slice 8 |
| D21 | 15 Oct | Neon direct connection, URL pasted as-is | PgBouncer breaks asyncpg and Alembic | Slice 9 |
| D22 | 15 Oct | `json_validate_failed` → repair path | Seen live despite strict mode | Slice 9 |
| D23 | 15 Oct | Groq Global ZDR on; Batch and Fine-tuning off | Privacy; neither used | — |
| D24 | 16 Oct | Kokoro is the everyday voice, Orpheus the extra when quota allows | Orpheus allows 3,600 tokens/day; portfolio project | V2 |
| D25 | 16 Oct | **ADR-0002:** brands adapted, not refused; per-category messages; real people stay refused | Friendlier; nothing trademarked published | ADR-0002 |
| D26 | 16 Oct | Scenes asked for in sentences; repair says how many to add | Models count sentences better | V1.1 |
| D27 | 20 Oct | Scene minimum lowered from 40 to 30 words | The 20b model writes 40–50 words; 1 in 9 failed | plan §6.1 |
| D28 | 20 Oct | Ship V1.1 without the "busy" auto-wait fix | Owner's choice; follow-up | section 12 |

---

## 11. Test inventory

| Suite | Count | How to run |
|---|---|---|
| Backend unit (`backend/tests/unit/`) | 121 | `uv run --frozen --no-sync pytest -m unit` |
| Backend integration (`backend/tests/integration/`) | 31 (needs Docker Postgres + Redis) | See section 6.1 |
| Backend live (`backend/tests/live/`) | 2 opt-in, use Groq quota | Skipped unless enabled |
| MCP (`mcp_server/tests/`) | 13 | `uv run --frozen --no-sync pytest` |
| Frontend (`frontend/tests/`) | 7 | `npm test` |
| Smoke (`scripts/local_smoke.sh`) | 33 checks | Against the Docker stack |
| Eval (`evaluation/json_validity.py`) | 50 prompts | Resumable, quota-paced |

Per-file test names appear in sections 7.3 and 8 (slices 1–9) and 9.5. The plan's general edge-case list (plan §12) is:
- silent, empty or 2-minute audio; wrong MIME type;
- non-English prompts; unsafe or injected prompts;
- a Groq 429 or 5xx; a Preview model returning 404;
- ZeroGPU quota exhausted; a Neon cold start;
- a duplicate POST; `continue_story` while still generating (409);
- an R2 upload failure.

---

## 12. Known issues and open follow-ups

| Item | Detail | Suggested fix | Priority |
|---|---|---|---|
| **"The storyteller is busy, try again in N s"** | One story uses the safety model's whole per-minute allowance (2 requests / 1,800 tokens after the margin), so a new idea within about 60 s gets 429 | **Option A:** the form counts down and resubmits automatically with the same Idempotency-Key (frontend only, about 30 lines plus tests). Optionally **option C:** margin 1.0 for safeguard (3 requests a minute). Rejected: queuing ideas before checking them (breaks "never stored") | High (UX); a good `v1.0.2` |
| **Repairs from undeclared side characters** | An owl, fish or teacher speaks but isn't in `characters`; the repair fixes it but costs about 5 s | Prompt tweak: background speakers must be declared, or their words go to the narrator | Medium |
| **Local and production share one Groq org** | If `.env` uses the same Groq account, local tests use the live site's real quota; Redis counters are separate (local vs Upstash) | Use a separate Groq org for local work, or accept it | Low |
| **Startup traceback noise** | `ValueError: Invalid file descriptor: -1` ("Exception ignored", from an event loop cleaned up twice, probably the migration loop) | Close the migration loop explicitly | Low |
| **Name check is a word match** | "Mia meets Peppa Pig" also renames Mia | Revisit if it shows up in practice | Low |
| **R2 may need a card** | Conflicts with the free/no-card rule | Check before V2 slice 3; otherwise choose another free S3-compatible store (the `S3BlobStore` design allows swapping) | Before V2 |
| Owner housekeeping | Optional: Langfuse keys; point Claude Desktop at the Space URL; re-run `cold_start_check.ps1` for a clean restart time; `docs/groq-limits.md` | — | Low |
| Postponed | Playwright tests (more useful once the mic and audio exist in V2) | Add in V2 | Medium |

---

## 13. Future phases V2–V10, slice by slice

The **tasks and exit criteria** come from `plan.md` §10. The **slice breakdown** is a proposal in the same style as V1 (one commit per slice, CI green, tests with each slice). Adjust as needed, and record decisions as ADRs.

### General rules for every phase
- Create a branch `dev-vN` from `main`.
- Read the plan's version section before coding.
- Every external model sits behind an interface with a fallback (plan §4.1).
- **Migrations:** the plan numbers them per version, but `0002` is already used. Renumber from **0003** (e.g. V4's tables become `0003`).
- Every phase ends with the release process in section 6.2 and an updated `handoff.md`.

### V2: Talking storybook, speech ↔ text

**Exit criteria:** a spoken prompt becomes a narrated story, **live**; the Orpheus → Kokoro fallback is shown by forcing the quota to 0.

| Slice | What to build | Key tests and edge cases |
|---|---|---|
| 1. STT | `STTEngine` interface; `GroqWhisper` (`whisper-large-v3-turbo`); `FasterWhisper` (small, int8, CPU) fallback; ffmpeg normalisation to 16 kHz mono; reject > 60 s, > 10 MB or silent audio; `POST /v1/speech/transcribe` (413, 415, 422) | Silent, empty and 2-minute audio; wrong MIME type; Groq 429 → fallback; audio-seconds quota in the router (`audio_seconds` limits already exist in `quotas.py`); **audio never written to disk or logs** |
| 2. TTS | `TTSEngine`; `GroqOrpheus` (sentences ≤ 200 characters, joined with `pydub`); `Kokoro` (Apache-2.0, CPU); voice map in `speech/voices.py`; **engine chosen once per story** (`stories.tts_engine` already exists); per-scene `tts` jobs, scene 1 first | Router picks Kokoro when Orpheus's daily tokens can't cover the whole story; forced quota 0 → Kokoro; long sentences are split; voices stay consistent within a story |
| 3. Blob storage | `BlobStore` → one `S3BlobStore` (SeaweedFS locally; R2 **or** another free S3 store in production); presigned GET URLs; `scenes.audio_key` | Upload failure → retry, then degrade; presigned URLs expire; content type set |
| 4. API + frontend | `POST /v1/stories` accepts multipart `audio`; `SceneOut.audio_url`; `MicRecorder` (MediaRecorder API); an audio player per page | Mic permission denied; unsupported browser; recording too long; partial pages (text ready, audio pending) |
| 5. Privacy | Audio held in memory only; `STT_ENGINE=local` option; `docs/privacy.md` (Groq processes data in the US; ZDR on) and `docs/dpia.md` (UK Children's Code; COPPA voiceprints) | A test that no audio bytes reach disk or logs |
| 6. MCP | `transcribe`, `narrate`, `voices://list` | Success, bad input, API error for each |
| 7. Playwright | E2E tests for mic-denied and polling (postponed from V1) | Run against localhost in the local gate |

**Decisions to make:**
- the production blob store (R2 versus a no-card alternative);
- where Kokoro runs (CPU on the Space) and how it affects cold start;
- whether to add the "busy" auto-wait first.

**Learn:** speech pipelines, audio formats, object storage, privacy by design.

### V3: Illustrated storybook, text/speech → image

**Exit criteria:** first page in ≤ 15 s locally; the main character is recognisable in ≥ 4 of 5 scenes; the placeholder fallback works.

| Slice | What to build | Key tests and edge cases |
|---|---|---|
| 1. GPU trigger design (**ADR-0003**) | Decide how GPU work runs when the **job worker**, not a browser, starts it (ZeroGPU charges per caller; the worker has none). Options: the frontend calls the Gradio endpoint per scene with the HF token, or the worker calls its own Gradio API with an HF token | Quota attribution verified live |
| 2. Image generation | `ImageGenerator`: `SDXLZeroGPU` (`@spaces.GPU(duration=…)`, < 60 s), `SD15Local` (6 GB: fp16, `enable_model_cpu_offload`, VAE tiling), `PlaceholderImage` | VRAM: one model at a time; GPU quota exhausted → placeholder plus a UI message |
| 3. Character consistency | `visual` string + server seed (already stored) + IP-Adapter reference from scene 1 (`characters.ref_image_key`); one main character per image | The same character across scenes |
| 4. Image self-check | Florence-2 caption matched against the description (keywords + SigLIP); up to 2 regenerations | Retry cap; never loops |
| 5. Image safety | Negative prompts, the diffusers safety checker, a caption check | An unsafe image never reaches the reader |
| 6. Pipeline + cache | Scene 1 TTS + image first, then the rest; content-addressed cache `sha256(prompt|seed|model|lora)` → blob key | A cache hit skips the GPU; ordering |
| 7. Speech → image + MCP | "Draw a purple dragon" path; MCP `illustrate` | — |
| 8. Synthetic data generator | Start `data_pipeline/flows/synth_generate.py` (resumable, uses the same token-bucket code, runs daily) so V6 has data | Resume after interruption; respects quota |

**Learn:** diffusion, VRAM management, async orchestration, caching, GPU as a scarce resource.

### V4: Story memory, the RAG pipeline

**Exit criteria:** recall@5 ≥ 0.8; RAG stories win ≥ 60% against no-RAG stories (gpt-oss-120b judge).

| Slice | What to build | Key tests and edge cases |
|---|---|---|
| 1. Ingestion | Prefect OSS flows: Gutenberg (**catalogue CSV + `/robot/harvest` or a mirror only; never scrape**), Standard Ebooks, TinyStories | Licence recorded for each source |
| 2. Cleaning | NFKC, boilerplate stripping, language ID, MinHash dedupe (≥ 0.8), grade tags, safety filter, scene chunks of 80–150 words, theme/moral tags; DVC + data cards | Duplicates removed; unsafe text dropped |
| 3. Schema | Migration **0003**: `tale_chunks` (vector(384), tsvector, HNSW + GIN), `story_bibles` | Expand-only |
| 4. Retriever | Query understanding (gpt-oss-20b, strict JSON) → pgvector top 20 + `ts_rank` top 20 → RRF → bge-reranker → top 3 → compression (cached in Redis) | Empty results; age-band filter |
| 5. Story bible | Written after planning; used by the planner (and by `continue_story` in V8) | Consistency |
| 6. Evaluation + MCP | 50-query gold set, `evaluation/rag_eval.py` (recall@5, MRR), the judge comparison; MCP `search_tales` | Exit-criteria numbers |

**Learn:** legal data access, ETL, embeddings, hybrid retrieval, vector indexes, data versioning.

### V5: Smart eyes, image → text and image → speech

**Exit criterion:** a drawing becomes a matching story, live. All six conversions working.

| Slice | What to build | Key tests and edge cases |
|---|---|---|
| 1. Vision | `VisionClient`: `GroqVision` (`qwen/qwen3.8-27b`, `reasoning_effort="none"`, strict JSON; ≤ 3 images, 20 MB) + `Florence2Vision` fallback; `POST /v1/images/describe` | Large or wrong-type uploads (413/415); unsafe drawings |
| 2. Drawing → story | Character sheet → planner | The story matches the drawing |
| 3. Read aloud | Image → caption → TTS | — |
| 4. Style dataset | Scrapy: Smithsonian Open Access + Wikimedia Commons (robots.txt, AutoThrottle, PD/CC0 only, pHash dedupe, Florence-2 captions) → 30–100 images | The licence filter is enforced |
| 5. MCP | `describe_drawing`, `read_page_aloud` | — |

**Learn:** VLMs, ethical scraping, image data cleaning.

### V6: My own storyteller, fine-tuning I and distillation

**Exit criteria:** valid JSON ≥ 98%; judge score ≥ 90% of gpt-oss-20b's; the fallback planner works live.

| Slice | What to build | Notes |
|---|---|---|
| 1. SFT data | `build_sft.py`: teacher outputs + TinyStories prompts → `sft.jsonl` (5–10k records), train/validation/test split with no overlap; data card | Needs the V3 generator's output |
| 2. T0 | nanoGPT on TinyStories (understanding pre-training) | Local RTX 4050 |
| 3. T1 | QLoRA SFT of **Qwen3-4B-Instruct-2507** (Unsloth: 4-bit, r=16, α=16, lr 2e-4, sequence length 1,536); MLflow; checkpoint to the HF Hub every 100–200 steps | Local (Unsloth Docker/WSL2) or Colab |
| 4. Evaluation | JSON validity, readability, judge; base vs fine-tuned vs Groq | — |
| 5. Serve | Merge, push to the Hub, `LocalLLM` in `@spaces.GPU`; `LLM_FALLBACK`; UI toggle "use StoryWeaver-mini" | ADR-0001 still applies |

**Learn:** SFT, QLoRA, distillation, evaluation, model registry.

### V7: Aligned and styled, fine-tuning II

**Exit criteria:** DPO wins ≥ 60% against SFT; the style LoRA improves consistency without lowering CLIP score.

| Slice | What to build |
|---|---|
| 1. Preference data | Chosen/rejected pairs plus filtered HH-RLHF/UltraFeedback; labelled in local Argilla (Docker) |
| 2. T2 DPO | TRL, β=0.1, 1–2k pairs, on Colab/Kaggle |
| 3. T3 style LoRA | SDXL LoRA on Colab/Kaggle (not possible on 6 GB); CLIP score + SigLIP consistency |
| 4. T4 stretch | gpt-oss-20b QLoRA attempt on a T4; record the result in an ADR, even if it fails |
| 5. Optional WER | Groq Whisper vs faster-whisper (`jiwer`) on LibriVox / Common Voice |
| 6. Results | Results table in the README |

**Learn:** preference alignment, image LoRA, multimodal evaluation.

### V8: Interactive and safe

**Exit criteria:** the red-team suite passes at **100%** with the Groq guards **and** with the fallback guards; voice interrupt works live.

| Slice | What to build | Key tests and edge cases |
|---|---|---|
| 1. `continue_story` | Voice → STT → planner with the story bible → regenerate only the remaining scenes; `POST /v1/stories/{id}/continue` (409 while generating); MCP `continue_story` | 409 path; only the remaining scenes change |
| 2. Safety hardening | Stricter system prompt, topic allow-list, output length caps, image negative prompts + safety checker | — |
| 3. Red-team gate | `tests/fixtures/redteam.jsonl` (100+ prompts: Base64, other languages, role-play jailbreaks) as a **CI gate** | Must be 100% |
| 4. Fallback guards | `LLMJudgeGuard` (fine-tuned Qwen3-4B + policy) and `RuleBasedGuard`, tested with the Groq guards off | Same 100% bar |
| 5. Rate limiting | Per-client limit of 10 stories/hour (Upstash); UI messages for 429 and quota exhaustion (the "busy" auto-wait, if not already done) | — |

**Learn:** defence in depth, adversarial testing, rate limiting.

### V9: Production-grade

**Exit criteria:** dashboard live; load-test report written; a story completes with Groq **and** the GPU disabled, using fallbacks.

| Slice | What to build |
|---|---|
| 1. Observability | Langfuse traces (turn it on); Prometheus metrics → Grafana Cloud (job latency, queue depth, Groq quota usage, fallbacks, ZeroGPU seconds); dashboard JSON in `infra/grafana/`; alerts |
| 2. Resilience | Circuit breakers on Groq, the blob store and Neon; the fallback matrix tested; `keepalive.yml` cron before demos |
| 3. Performance | Redis caches (embeddings, chunk summaries, repeated TTS phrases); Locust load test within the free quotas; p50/p95 time to first page |
| 4. MCP v2 | Streamable HTTP on the Space with an API key; Claude Desktop connects to the live service |
| 5. End-to-end verification | The ten steps in plan §15 on the live site |

**Learn:** observability, circuit breakers, graceful degradation, load testing.

### V10: Polish and portfolio

| Slice | What to build |
|---|---|
| 1. Export | PDF or EPUB with audio links; story gallery |
| 2. Multilingual (optional) | Check Whisper language support and Kokoro voices |
| 3. Portfolio | README with the architecture diagram, ADRs, model and data cards, evaluation table, privacy notice, and a 2-minute demo video |

---

## 14. Free-tier budgets and limits

**Groq free tier (per organisation; config in `routing/quotas.py`; the router applies a 0.9 margin, never below 1):**

| Model | RPM | RPD | TPM | TPD | Other |
|---|---|---|---|---|---|
| `openai/gpt-oss-20b` (planner) | 30 | 1,000 | 8,000 | 200,000 | — |
| `openai/gpt-oss-120b` (repair, teacher) | 30 | 1,000 | 8,000 | 200,000 | — |
| `qwen/qwen3.8-27b` (vision, V5) | 30 | 1,000 | 8,000 | 200,000 | — |
| `whisper-large-v3-turbo` (V2) | 20 | 2,000 | — | — | 7,200 audio-s/hour; 28,800 audio-s/day |
| `canopylabs/orpheus-v1-english` (V2) | 10 | 100 | 1,200 | **3,600** | ≤ 200 characters per request |
| `meta-llama/llama-prompt-guard-2-22m` | 30 | 14,400 | 15,000 | 500,000 | — |
| `openai/gpt-oss-safeguard-20b` | **3** | 1,000 | **2,000** | 200,000 | The real bottleneck per minute |

**Practical capacity:**
- About **50–60 stories a day** (limited by the planner's tokens per day).
- About **one story per 1–2 minutes** (limited by safeguard's per-minute limit).
- Orpheus covers about 2–3 narrated stories a day; Kokoro covers the rest.

**Other free tiers:**
- Neon: 1 GB; scales to zero after 5 minutes idle.
- Upstash: 500K commands a month; about 20–30 per story.
- Vercel Hobby: 300 s function maximum; non-commercial.
- HF ZeroGPU: GPU quota per caller (anonymous ≈ 2 min/day, free account ≈ 5 min/day); 60 s per call; sleeps after 48 h idle.
- Langfuse Hobby: 50K units a month.
- Grafana Cloud free: 10K series.
