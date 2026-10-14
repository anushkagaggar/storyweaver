# StoryWeaver: Detailed Build Plan

> A voice-first, illustrated children's audiobook generator that converts between **text, image and speech**.
> Learning goals:
> - your own **MCP server**
> - **data collection** (APIs, scraping, datasets, synthetic data)
> - **LLM training** (SFT/QLoRA, distillation, DPO, image LoRA)
> - **RAG**
> - **system design**
>
> Plan date: **12 Oct 2026**. Free-tier facts were checked against official docs on this date (sources in §16). Items marked *(unverified)* still need checking.

---

## 0. Ground rules for this project

1. **Groq first.** Every AI task that Groq's **free tier** offers runs on Groq: text, vision, speech-to-text, text-to-speech and safety.
2. **Open weights only where Groq can't help.** That means:
   - image generation,
   - embeddings and re-ranking,
   - the fine-tuned model you train yourself (Groq only hosts custom LoRAs for Enterprise customers),
   - offline fallbacks.
3. **No gated models.** Nothing that needs an access request or approval.
4. **Everything free.** Free tiers and open-source only, with no paid plans.
5. **Local before live.** Every version is tested in Docker on your PC. The frontend then deploys to **Vercel** and the backend to the **Hugging Face Spaces free tier**.
6. **Python** for all AI and backend code (**FastAPI**). **Next.js + TypeScript** only for the Vercel UI.
7. **Every external model sits behind an interface, with a fallback.** Free tiers and Preview models can change or vanish without warning. For example, Groq removed Llama from its free tier on 16 Aug 2026 and shut down Llama Guard 4 on 5 Mar 2026.

---

## 1. Your development machine (detected 12 Oct 2026)

| Item | Value | Consequence |
|---|---|---|
| GPU | NVIDIA **RTX 4050 Laptop, 6 GB VRAM**, driver 610.88, CUDA 13.3 | Local SD 1.5 / SDXL (with offload), Kokoro, faster-whisper, QLoRA of models up to about 4B. **No SDXL LoRA training and no gpt-oss-20b training on it.** |
| iGPU | AMD Radeon 780M | Make sure Docker, Python and Ollama use the NVIDIA GPU |
| CPU | Ryzen 7 7840HS (8 cores / 16 threads) | Good for Docker, CPU fallbacks and data pipelines |
| RAM | 16 GB | One GPU model at a time; CPU offload with care |
| Disk | **C: 18 GB free**, D: 241 GB free | **Everything on D:**: repo, datasets, model caches, Docker/WSL disk |
| Software | Python 3.10.11, Docker 28.5.2, Git 2.51, Node 24.13 | Install **uv**, then **Python 3.12** through uv. Ollama is optional (offline fallback only). |

**One-time machine setup (V0)**
```powershell
# Move the Docker Desktop disk image to D: (Settings → Resources → Advanced → Disk image location)
setx HF_HOME "D:\hf"
setx OLLAMA_MODELS "D:\ollama"
setx UV_CACHE_DIR "D:\uv-cache"
winget install --id=astral-sh.uv
uv python install 3.12
# GPU check inside Docker (WSL2 backend):
docker run --rm --gpus=all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark
```

---

## 2. Model matrix (Groq free tier first)

### 2.1 Groq models used (free-tier limits are **per organisation**, not per API key)

| Task | Groq model ID | Status | Free limits (RPM / RPD / TPM / TPD) | Notes |
|---|---|---|---|---|
| Story planner (main LLM) | `openai/gpt-oss-20b` | Production | 30 / 1K / 8K / 200K | **Strict JSON-schema** structured outputs; fast |
| Teacher, judge, hard tasks | `openai/gpt-oss-120b` | Production | 30 / 1K / 8K / 200K | Synthetic data, LLM-as-judge, story repair |
| Vision (image → text) | `qwen/qwen3.8-27b` | **Preview** | 30 / 1K / 8K / 200K | Up to 3 images per request, about 2,048 tokens each, 20 MB maximum; strict JSON schema; set `reasoning_effort="none"` for speed. Licence of the open weights *(unverified)* |
| Speech → text | `whisper-large-v3-turbo` | Production | 20 / 2K, plus 7,200 audio-seconds per hour and 28,800 per day | `whisper-large-v3` if accuracy matters more |
| Text → speech | `canopylabs/orpheus-v1-english` | **Preview** | 10 / 100 / 1.2K / **3.6K** | Keep each request **≤ 200 characters**; 6 voices. Tiny daily budget, so used for the first few stories each day only |
| Prompt-injection guard | `meta-llama/llama-prompt-guard-2-22m` | **Preview** | 30 / 14.4K / 15K / 500K | Hosted by Groq, so no approval needed |
| Content-safety policy check | `openai/gpt-oss-safeguard-20b` | **Preview** | Docs say **3 RPM / 1K RPD / 2K TPM / 200K TPD** (a third-party blog says 30 RPM; use the docs figure) | You write the child-safety policy yourself; Groq names it as the replacement for Llama Guard 4 |

**Before V1:**
- Open the Groq console → **Settings → Limits** and record your organisation's actual limits in `docs/groq-limits.md`.
- Enable **Zero Data Retention** under **Settings → Data Controls**. Groq says all customers can do this; whether it applies to the free tier isn't stated, so check *(unverified)*.

### 2.2 Open-weight models (only where Groq has nothing, or as a fallback)

| Task | Model | Licence | Where it runs | Why it isn't on Groq |
|---|---|---|---|---|
| Text → image | **SDXL base 1.0** (+ IP-Adapter, + your style LoRA); **SD 1.5** for fast local development | OpenRAIL++-M / OpenRAIL-M; IP-Adapter Apache-2.0 | HF ZeroGPU Space; your RTX 4050 locally | Groq doesn't do image generation |
| Text embeddings | `BAAI/bge-small-en-v1.5` | MIT | CPU (Space + local) | Groq has no embeddings API |
| Re-ranker | `BAAI/bge-reranker-base` | MIT | CPU | Not on Groq |
| Image embeddings (character consistency) | `google/siglip-base-patch16-224` | Apache-2.0 | CPU / ZeroGPU | Not on Groq |
| Image self-check (cheap) | `microsoft/Florence-2-base` | MIT | ZeroGPU / local | Saves Groq vision tokens: 5 images × 2,048 tokens would exceed 8K TPM |
| **Your fine-tuned model** | **Qwen3-4B-Instruct-2507**, distilled from gpt-oss-120b | Apache-2.0 | ZeroGPU Space / local (Ollama GGUF) | Groq hosts custom LoRAs only for Enterprise |
| Stretch fine-tune (same family as the Groq planner) | `openai/gpt-oss-20b` QLoRA | Apache-2.0 | Colab/Kaggle T4. Unsloth says about 14 GB; one report says it fails on T4 *(unverified)* | Same reason |
| TTS fallback | **Kokoro-82M** | Apache-2.0 | CPU | Covers the tiny Orpheus quota |
| STT fallback | **faster-whisper small** (int8) | MIT | CPU | Offline use, or when quota runs out |
| Safety fallback | Block list + fine-tuned Qwen3-4B as an LLM judge using the same policy | Apache-2.0 | ZeroGPU / local | Used if Groq Preview guard models vanish |
| Image safety | Diffusers `StableDiffusionSafetyChecker` + Florence-2 caption check | Model licence | ZeroGPU / local | Not on Groq |

**Excluded:**
- *Gated:* Llama Guard, ShieldGemma, Llama 3.x, Gemma.
- *Non-commercial or research-only:* Qwen2.5-3B, Qwen2.5-VL-3B, SDXL-Turbo, FLUX.1-dev, XTTS-v2.
- *GPL:* Piper.
- *Trains on free-tier data:* the Gemini free tier.

### 2.3 Groq quota budget per story (why the routing rules exist)

| Step | Groq calls per story | Tokens / audio | Daily capacity on free tier (approx.) |
|---|---|---|---|
| STT | 1 (≤ 60 s audio) | 60 audio-seconds | about 480 stories (28,800 s/day) |
| Prompt Guard | 1–2 | tiny | thousands |
| Safeguard (input + full story) | 2 | about 1.5K | **3 RPM**, so at most about 1.5 stories/min; about 500/day |
| Planner (gpt-oss-20b) | 1 (+1 repair) | about 3K | **about 60 stories/day** (200K TPD); about 2/min (8K TPM) |
| Vision (drawing mode only) | 1 | about 2.5K | about 80 drawings/day |
| Orpheus TTS | about 10–15 (≤ 200 characters each) | about 1.2K | **about 2–3 stories/day**, then Kokoro |

**Result:** about **50–60 stories a day** is the free-tier ceiling. The planner's tokens per day are the bottleneck. That's plenty for a portfolio demo.

The **quota-aware router** (§6.3):
- enforces these limits *before* calling Groq,
- picks a fallback when a limit is near,
- chooses the TTS engine **once per story**, so the voices stay consistent within a story.

---

## 3. Hosting and infrastructure (all free)

| Concern | Service | Free-tier facts (verified 12 Oct 2026) | Gotchas |
|---|---|---|---|
| Frontend | **Vercel Hobby** | 300 s function maximum; 100 GB transfer; 1M invocations | **Non-commercial only**; never proxy long jobs through it |
| Backend | **HF ZeroGPU Gradio Space** (`gr.Server`: FastAPI REST + Gradio GPU endpoints, ADR-0001) | Free accounts: **up to 2 ZeroGPU Spaces** (needs a **verified email and an account at least 30 days old**). Docker and CPU Gradio Spaces need PRO. | Sleeps after 48 h idle; ephemeral disk; GPU quota charged **per caller** (anonymous ≈ 2 min/day, free account ≈ 5 min/day); 60 s default per GPU call; PyTorch only |
| Database + vectors | **Neon Postgres + pgvector** | 1 GB per project, no card | Scales to zero after 5 minutes (cold start) |
| Cache, rate-limit counters | **Upstash Redis** | 500K commands/month, 250 MB | Watch the command count |
| Images and audio | **Cloudflare R2** | 10 GB, free egress | Card requirement *(unverified)* |
| LLM tracing | **Langfuse Hobby** | 50K units/month | 30-day data access |
| Metrics and logs | **Grafana Cloud free** | 10K series, 50 GB logs, 14 days | — |
| Experiment tracking | **MLflow** (local) | Open source | — |
| Data versioning | **DVC + DagsHub** | Free; private repos are non-commercial only | Storage quota conflicts between pages *(unverified)* |
| Training GPU | Your RTX 4050 + **Colab** (≤ 12 h, limits unpublished) + **Kaggle** (about 30 GPU-h/week, *third-party figure*) | — | Checkpoint often |
| CI/CD | **GitHub Actions** (public repo, free) | — | — |
| Local stack | Docker Desktop + Compose (WSL2, GPU) | Free for personal use | Celery is unsupported on Windows, so everything runs in Linux containers |

**Dropped:**
- **Qdrant Cloud:** suspended after 7 days idle, deleted after 28.
- **Prefect Cloud:** may need a card; run Prefect OSS locally.
- **Argilla on HF:** needs a Docker Space (PRO only); run it locally in Docker.
- **Gemini free tier:** trains on your data.
- **OpenRouter free:** 50 requests/day.
- **HF Inference Providers:** $0 credits for free users.
- **MinIO (local S3):** community images no longer published; `minio/minio` is gone from Docker Hub (found 13 Oct 2026). Replaced by **SeaweedFS** (`chrislusf/seaweedfs`, Apache-2.0), S3 API on port 8333.

---

## 4. Architecture

```
                         ┌──────────────────────────── Vercel (Hobby) ────────────────────────────┐
 Parent/child browser ──►│ Next.js + TS UI  ·  Route handlers (short calls only):                  │
                         │  POST /api/stories → 202 · GET /api/jobs/:id · GET /api/stories/:id     │
                         │  (holds STORYWEAVER_API_KEY server-side; never in the browser)          │
                         └───────────────┬─────────────────────────────────────────────────────────┘
                                         │ HTTPS + API key
                         ┌───────────────▼──────────── HF ZeroGPU Space ──────────────────────────┐
                         │ gr.Server (FastAPI): REST /v1 + Gradio GPU API (ADR-0001); MCP in V9    │
                         │  api/ → orchestrator/ → services → adapters                              │
                         │  • in-process async job worker (jobs table in Neon, idempotent keys)     │
                         │  • ModelRouter (quota-aware, Upstash token buckets mirroring Groq limits)│
                         │  • @spaces.GPU via Gradio API only: SDXL, Florence-2, fine-tuned Qwen3   │
                         │  • CPU: bge embeddings/reranker, SigLIP, Kokoro, faster-whisper fallback │
                         └──┬─────────────┬──────────────┬─────────────┬──────────────┬────────────┘
                            ▼             ▼              ▼             ▼              ▼
                     Groq API      Neon Postgres    Upstash Redis   Cloudflare R2   Langfuse/Grafana
              (gpt-oss, qwen-vl,    + pgvector      (rate limits,   (images, audio,  (traces, metrics,
               whisper, orpheus,   (stories, jobs,   cache)          presigned URLs)   logs)
               guards)              RAG chunks)

 Offline (your PC + Colab/Kaggle): Prefect OSS flows · Scrapy · DVC · Groq teacher (batched, quota-paced)
                                   · Unsloth/TRL/diffusers · MLflow · Argilla (local Docker)
 Local mirror (docker compose): api(GPU) · web · postgres+pgvector · redis · seaweedfs (S3) · [ollama optional]
```

### 4.1 Interfaces (the same code runs locally and in production; only `.env` differs)

| Interface | Production adapter(s) | Local / fallback adapter(s) |
|---|---|---|
| `LLMClient` | `GroqLLM(gpt-oss-20b / 120b)` | `LocalLLM(fine-tuned Qwen3-4B)`, `OllamaLLM` |
| `VisionClient` | `GroqVision(qwen3.8-27b)` | `Florence2Vision` |
| `STTEngine` | `GroqWhisper` | `FasterWhisper` |
| `TTSEngine` | `GroqOrpheus` (within quota) | `Kokoro` |
| `SafetyClassifier` | `GroqPromptGuard` + `GroqSafeguard` | `RuleBasedGuard` + `LLMJudgeGuard` |
| `ImageGenerator` | `SDXLZeroGPU` | `SD15Local`, `PlaceholderImage` |
| `Embedder` / `Reranker` | `BGESmall` / `BGEReranker` | same |
| `BlobStore` | `S3BlobStore` → Cloudflare R2 endpoint | `S3BlobStore` → local SeaweedFS endpoint (same adapter, different `.env`) |
| `JobQueue` | `AsyncPgJobQueue` | same, on local Postgres |
| `RateLimiter` | `UpstashTokenBucket` | `RedisTokenBucket` |

---

## 5. Repository layout (`D:\lenovo\work\storyweaver`)

```
storyweaver/
├── plan.md                         # this file
├── README.md
├── .gitignore  .env.example  .pre-commit-config.yaml
├── backend/                        # → HF Space repo (pushed by CI)
│   ├── app.py                      # Space entrypoint: gr.Server (REST + Gradio GPU API), started with launch()
│   ├── README.md                   # HF Space card (sdk: gradio, hardware: zero-a10g/zerogpu)
│   ├── pyproject.toml  uv.lock
│   ├── requirements.txt            # exported from uv.lock (HF installs this)
│   ├── Dockerfile                  # LOCAL mirror of the Space runtime (CUDA + Python 3.12)
│   └── src/storyweaver/
│       ├── config.py               # pydantic-settings; all env vars
│       ├── domain/                 # story.py, character.py, scene.py, job.py (Pydantic)
│       ├── api/                    # main.py, routers/{stories,jobs,speech,images,health}.py,
│       │                           # errors.py (RFC 7807), deps.py, middleware.py (request-id)
│       ├── orchestrator/           # story_service.py, continue_service.py, pipeline.py
│       ├── routing/                # model_router.py, quotas.py (Groq limit table), fallbacks.py
│       ├── llm/                    # prompts/, planner.py, repair.py, schemas.py
│       ├── rag/                    # embedder.py, retriever.py, reranker.py, story_bible.py
│       ├── speech/                 # stt.py, tts.py, voices.py, audio_utils.py
│       ├── vision/                 # image_gen.py, consistency.py, describe.py, image_check.py
│       ├── safety/                 # policy.md, input_guard.py, output_guard.py, image_guard.py
│       ├── jobs/                   # queue.py, worker.py, state_machine.py
│       ├── storage/                # db.py, models.py, repositories.py, blob.py
│       ├── adapters/               # groq_*.py, sdxl.py, kokoro.py, faster_whisper.py, s3_blob.py
│       └── observability/          # logging.py, metrics.py, tracing.py
│   └── migrations/                 # Alembic
├── frontend/                       # → Vercel
│   ├── app/ (Next.js App Router)   # page.tsx, story/[id]/page.tsx, api/*/route.ts
│   ├── components/                 # MicRecorder, StoryBook, PageView, AudioPlayer, DrawingUpload
│   ├── lib/api.ts                  # typed client (types generated from FastAPI OpenAPI)
│   └── tests/ (Vitest, Playwright)
├── mcp_server/                     # FastMCP: stdio (local) → Streamable HTTP (V9)
│   ├── server.py  client.py  tests/
├── data_pipeline/
│   ├── flows/                      # gutenberg.py, standard_ebooks.py, tinystories.py, librivox.py,
│   │                               # illustrations.py, clean.py, index.py, synth_generate.py, build_sft.py
│   ├── scrapers/illustrations/     # Scrapy project
│   └── data/ (DVC-tracked; raw/, clean/, datasets/)
├── training/
│   ├── notebooks/                  # 01_sft_qwen3_4b.ipynb, 02_dpo.ipynb, 03_sdxl_style_lora.ipynb,
│   │                               # 04_gpt_oss_20b_qlora.ipynb (stretch), 00_nanogpt_tinystories.ipynb
│   ├── configs/*.yaml
│   └── export/                     # merge_and_push.py, to_gguf.py
├── evaluation/                     # judge.py, readability.py, json_validity.py, clip_score.py,
│                                   # character_consistency.py, wer.py, rag_eval.py, redteam.py, run.py
├── infra/
│   ├── docker-compose.yml          # api, web, postgres(pgvector), redis, seaweedfs (S3), argilla, mlflow
│   └── grafana/                    # dashboards as JSON
├── .github/workflows/              # ci.yml, deploy-backend.yml, deploy-frontend (Vercel Git), keepalive.yml
├── docs/
│   ├── architecture.md  groq-limits.md  privacy.md  dpia.md
│   ├── decisions/                  # ADR-0001-zerogpu-fastapi.md, ADR-0002-groq-first.md ...
│   ├── data-cards/  model-cards/
└── tests/                          # unit/, integration/, contract/, fixtures/redteam.jsonl
```

---

## 6. Core designs

### 6.1 Story JSON (the planner's contract; Groq strict JSON-schema mode)
```json
{
  "title": "Pip and the Night Lights",
  "age_band": "4-6",
  "moral": "Being brave means trying even when you're scared",
  "characters": [
    {"id": "pip", "name": "Pip", "species": "dragon",
     "visual": "small purple dragon, round belly, tiny yellow wings, green striped scarf",
     "voice": "narrator_female_1", "seed": 421337}
  ],
  "setting": "a cosy cave on a hill above a sleepy village",
  "scenes": [
    {"idx": 1, "text": "…", "image_prompt": "…", "characters": ["pip"],
     "lines": [{"speaker": "narrator", "text": "…"}, {"speaker": "pip", "text": "…"}]}
  ]
}
```
**Validation**
- Pydantic checks: 5–6 scenes; each scene 40–120 words; Flesch-Kincaid grade ≤ 4; character IDs referenced correctly.
- On failure: **one repair call** to gpt-oss-120b, then a clean error.

### 6.2 Database (Neon / local Postgres 16 + pgvector)
| Table | Key columns | Indexes |
|---|---|---|
| `stories` | id (uuid), prompt_text, age_band, status, tts_engine, created_at | status, created_at |
| `characters` | id, story_id FK, name, visual_desc, voice_id, seed, ref_image_key | story_id |
| `scenes` | id, story_id FK, idx, text, image_prompt, image_key, audio_key, status | unique (story_id, idx) |
| `jobs` | id, kind, story_id, status, attempts, last_error, payload_hash (unique), run_after | (status, run_after), payload_hash |
| `tale_chunks` | id, tale_id, title, theme, moral, age_band, text, embedding vector(384), tsv tsvector | HNSW on embedding, GIN on tsv, btree on theme |
| `story_bibles` | story_id PK, json, embedding vector(384) | — |

**Migrations:** Alembic.
- **Backward-compatible migrations only** (expand, then contract).
- The Space runs `alembic upgrade head` on start-up.

### 6.3 Quota-aware model routing (`routing/`)
- `quotas.py` mirrors Groq's limits from §2.1: RPM, RPD, TPM, TPD and audio-seconds.
- Before each call, `ModelRouter.reserve(model, est_tokens)` checks the token buckets in Upstash.
  - If the request would exceed a limit, it returns the fallback adapter.
- On a Groq `429`, it honours `retry-after` and records the event; on `5xx`, it trips a circuit breaker (V9).
- **TTS is chosen once per story:** Orpheus if the remaining TPD can cover the whole story, otherwise Kokoro.
- Every call is logged with model, tokens, latency and whether a fallback was used. This feeds Langfuse and Grafana.

### 6.4 Job flow (async, idempotent)
1. `POST /v1/stories` (with an `Idempotency-Key` header):
   - safety check on the input,
   - insert the story,
   - enqueue a `plan` job,
   - return **202** `{story_id, job_id}`.
2. The worker runs `plan`:
   - RAG retrieval,
   - planner (gpt-oss-20b),
   - safeguard check on the story text,
   - save scenes,
   - enqueue `tts:{scene}` and `image:{scene}` jobs. Scene 1 goes first.
3. `image:1` produces the character reference image. Later scenes use IP-Adapter with it.
4. The UI polls `GET /v1/stories/{id}` (every 1.5 s, with backoff) and shows each page when its text, audio and image are ready.
5. Retries use exponential backoff, up to 3 attempts. A job that still fails goes to `status=failed`, and the scene falls back to a placeholder image or Kokoro audio.

### 6.5 Character consistency
- A fixed `visual` string for each character goes into every image prompt.
- A fixed seed per character.
- An IP-Adapter reference image is generated in scene 1.
- One main character per image.
- After each image, Florence-2 writes a caption, and the caption is matched against the character's description (keywords plus SigLIP similarity).
- If the check fails, the image is regenerated, up to 2 times.

### 6.6 Child safety and privacy
- **Input checks, in order:**
  1. Block list and rules (free, fast).
  2. Prompt Guard 2 22M looks for prompt injection.
  3. gpt-oss-safeguard-20b checks the input against `safety/policy.md`.
- **Output checks:** gpt-oss-safeguard on the full story. Images get a negative-prompt list, the diffusers safety checker and a Florence-2 caption check.
- **What `safety/policy.md` covers:**
  - Allowed: mild peril only, kindness themes.
  - Not allowed: violence, death, romance, real people, brands, URLs or personal data.
  - The story always ends happily.
- **Privacy:**
  - A parent operates the app, and there are no accounts.
  - Audio is sent to Groq Whisper and **never stored**. It isn't logged or written to disk.
  - Groq Zero Data Retention is enabled.
  - `privacy.md` discloses that Groq processes data in the US.
  - A setting `STT_ENGINE=local` keeps voice processing entirely on the Space, using faster-whisper on CPU.
  - Stories hold no names unless the parent types one in.
- **Regulation:**
  - US: the COPPA amendments took effect 23 Jun 2025, with compliance due 22 Apr 2026. Voiceprints now count as personal information.
  - UK: the Children's Code applies, so write a DPIA.
  - No public launch without legal review.

### 6.7 MCP server (`mcp_server/`, FastMCP)
The MCP server is a **thin adapter over the FastAPI API**. It contains no business logic.

| Tool / Resource / Prompt | Signature | Conversion | Version |
|---|---|---|---|
| tool `create_story` | `(prompt: str, age_band: str="4-6", num_scenes: int=5) -> {story_id, job_id}` | text → text | V1 |
| tool `get_story` | `(story_id) -> story with page URLs` | — | V1 |
| tool `get_job_status` | `(job_id)` | — | V1 |
| resource `story://{id}` | Full Story JSON | — | V1 |
| prompt `bedtime_story` | `(favourite_animal, theme)` | — | V1 |
| tool `transcribe` | `(audio_path) -> text` | speech → text | V2 |
| tool `narrate` | `(text, voice) -> audio_url` | text → speech | V2 |
| resource `voices://list` | Voices for both engines | — | V2 |
| tool `illustrate` | `(prompt, character_ref?) -> Image` | text → image | V3 |
| tool `search_tales` | `(query, theme?) -> chunks` | RAG | V4 |
| tool `describe_drawing` | `(image_path) -> character` | image → text | V5 |
| tool `read_page_aloud` | `(image_path) -> audio_url` | image → speech | V5 |
| tool `continue_story` | `(story_id, direction)` | speech/text → story | V8 |

**Transport:** stdio locally (Claude Desktop) in V1–V8. In V9 it moves to Streamable HTTP mounted on the Space with an API key.

### 6.8 REST API (FastAPI, `/v1`)
| Method | Path | Success | Errors |
|---|---|---|---|
| POST | `/v1/stories` (JSON, or multipart with `audio`) | 202 | 400, 413, 415, 422, 429 |
| GET | `/v1/stories/{id}` | 200 | 404 |
| POST | `/v1/stories/{id}/continue` | 202 | 404, 409 (still generating), 429 |
| GET | `/v1/jobs/{id}` | 200 | 404 |
| POST | `/v1/speech/transcribe` | 200 | 413 (> 10 MB), 415, 422 (> 60 s or silent) |
| POST | `/v1/speech/narrate` | 200 | 422 |
| — | Image generation: Gradio API endpoint, not REST (ADR-0001). How the job worker triggers GPU work is decided in V3. | — | GPU quota exhausted |
| POST | `/v1/images/describe` | 200 | 413, 415 |
| GET | `/health/live`, `/health/ready` | 200 | 503 |

**Cross-cutting rules**
- An API key in the `X-API-Key` header, sent from the Vercel server side only.
- CORS allows only the Vercel domain and `localhost`.
- RFC 7807 problem+json error bodies.
- An `X-Request-ID` on every request.
- An `Idempotency-Key` on POSTs.
- Rate limit: 10 stories per hour per client (Upstash).
- Every handler is `async`.
- **GPU-bound work is never a REST route** (ADR-0001): it is a Gradio API endpoint on the same `gr.Server`, protected by the same `X-API-Key`, called with an HF token so ZeroGPU charges that account (anonymous calls get about 2 GPU-min/day).

### 6.9 Environment variables (`.env.example`; secrets go in HF Space **Secrets** and Vercel env, never committed)
```
APP_ENV=local|prod
STORYWEAVER_API_KEY=
GROQ_API_KEY=
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=  (Upstash rediss://... in prod)
BLOB_BACKEND=s3local|r2   S3_ENDPOINT_URL= S3_ACCESS_KEY_ID= S3_SECRET_ACCESS_KEY= (local)   R2_ACCOUNT_ID= R2_ACCESS_KEY_ID= R2_SECRET_ACCESS_KEY= R2_BUCKET=
LLM_PLANNER=groq:openai/gpt-oss-20b   LLM_TEACHER=groq:openai/gpt-oss-120b
LLM_FALLBACK=local:storyweaver-qwen3-4b
VISION=groq:qwen/qwen3.8-27b   STT_ENGINE=groq|local   TTS_ENGINE=auto|orpheus|kokoro
IMAGE_MODEL=sdxl|sd15|placeholder
LANGFUSE_PUBLIC_KEY= LANGFUSE_SECRET_KEY= LANGFUSE_HOST=
HF_TOKEN= (read/write, for pushing models; Space Secret)
ALLOWED_ORIGINS=https://storyweaver.vercel.app,http://localhost:3000
```

---

## 7. Data plan (free and properly licensed)

| Dataset / source | Licence | How you get it | Used for | Version |
|---|---|---|---|---|
| **TinyStories** `roneneldan/TinyStories` | CDLA-Sharing-1.0 | HF `datasets` (streaming) | SFT seed prompts, style reference, nanoGPT | V4, V6 |
| **Project Gutenberg** (children's subjects) | Public domain (strip the PG licence header and footer) | **Official catalogue CSV + `/robot/harvest` (2 s wait) or a mirror.** Never scrape the website; it blocks bots. | RAG tale corpus | V4 |
| **Standard Ebooks** | CC0 *(unverified)* | OPDS feed / bulk download | RAG corpus | V4 |
| **LibriVox API** | Public domain *(unverified)* | REST API | WER test audio, narration pacing reference | V2, V7 |
| **Smithsonian Open Access** | CC0 | API | Illustration style images | V5 |
| **Wikimedia Commons API** | Licence per file; keep only PD/CC0 | Scrapy + API, robots.txt honoured | Illustration style images | V5 |
| **HH-RLHF** (Anthropic) | MIT | HF | DPO (helpful/harmless slice) | V7 |
| **UltraFeedback** (openbmb) | MIT | HF | DPO (filtered) | V7 |
| **Synthetic SFT data** | Your own (labelled synthetic) | gpt-oss-120b on Groq as the teacher, quota-paced | SFT / distillation | V6 |
| **Your preference labels** | Your own | Argilla (local Docker) | DPO | V7 |
| Common Voice (optional) | CC0 | **Mozilla Data Collective** account (no longer on HF since Oct 2025) | Whisper evaluation | V7 (optional) |

**Cleaning pipeline (`data_pipeline/flows/clean.py`)**
1. Unicode NFKC, whitespace fixes, strip Gutenberg boilerplate.
2. Language ID (English only).
3. Near-duplicate removal with MinHash LSH (`datasketch`, Jaccard ≥ 0.8).
4. Age filter: Flesch-Kincaid ≤ 4 for the generation corpus (classic tales are kept for RAG, tagged by grade).
5. Safety filter: rules, then gpt-oss-safeguard on a sample. Drop anything with violence beyond mild.
6. Split into scenes (80–150 words), tag theme and moral with gpt-oss-20b (batched, quota-paced).
7. Images: keep ≥ 768 px, dedupe by perceptual hash (`imagehash`), keep the licence metadata, caption with Florence-2.
8. Write a data card for every dataset (source, licence, counts, filters, date) and track it with DVC.

**Groq quota pacing for synthetic data**
- gpt-oss-120b allows about 200K tokens per day, so 5–10k SFT records take **about 2–4 weeks**.
- `synth_generate.py` is therefore:
  - **resumable** (checkpoint file),
  - **rate-limited** (same token-bucket code as the backend),
  - run daily by a local scheduled Prefect flow.
- To finish faster, add the local Qwen3-4B as a second teacher for easy cases, and record which teacher produced each record.
- **Start synthetic generation in V3**, so the data is ready for V6.

---

## 8. Training plan

| # | What | Base | Method | Compute | Success metric |
|---|---|---|---|---|---|
| T0 | Understand pre-training | nanoGPT | Train a small GPT on TinyStories for 30–60 min | Your RTX 4050 | Generates coherent short text; you can explain the loss curve |
| T1 | **StoryWeaver-mini planner** (distillation) | Qwen3-4B-Instruct-2507 | **QLoRA SFT** with Unsloth: 4-bit, r=16, α=16, lr 2e-4, 1–2 epochs, sequence length 1,536, batch 1 with grad-accum 8, gradient checkpointing | **RTX 4050** (Unsloth: about 3.5 GB minimum for 3B) via the `unsloth/unsloth` Docker image or WSL2; Colab T4 for longer runs | Valid JSON ≥ 98%; judge score ≥ 90% of gpt-oss-20b's; FK grade ≤ 4 |
| T2 | Alignment | T1 checkpoint | **DPO** (TRL), β=0.1, about 1–2k pairs | Colab / Kaggle T4 | Win rate ≥ 60% against T1 (gpt-oss-120b judge); 100% safety pass on the red-team set |
| T3 | Book illustration style | SDXL base | **LoRA** (diffusers `train_text_to_image_lora_sdxl`): 768–1024 px, batch 1, latents cached, 8-bit Adam, fp16, about 1,500 steps | **Colab / Kaggle T4** (not possible on 6 GB) | CLIP score not lower than base; people rate style consistency higher |
| T4 (stretch) | Same family as the Groq planner | gpt-oss-20b | QLoRA with Unsloth, `load_in_4bit=True`, sequence length 1,024 | Colab / Kaggle T4. Unsloth says about 14 GB; one report says it fails on T4. **Try it; skip it if it fails.** | Compare against Groq's gpt-oss-20b on the same evaluation |

**Rules for every run**
- Log the config, the DVC hash of the data, metrics and sample outputs to **MLflow**.
- Checkpoint to the **HF Hub** (a public or private model repo is free) every 100–200 steps, so Colab disconnects don't lose work.
- Write a model card for every model.

**Serving your model**
- Merge the LoRA into the base model and push it to the HF Hub.
- The Space loads it inside `@spaces.GPU` (bf16 or 4-bit).
- Export a GGUF for an optional local Ollama.
- It's used as the **fallback planner** and for **A/B comparisons** against Groq.

---

## 9. RAG pipeline (V4)

1. **Indexing:** scene chunks get bge-small embeddings (384 dimensions) and a Postgres `tsvector`, plus metadata (theme, moral, age band, grade). Everything goes into the `tale_chunks` table.
2. **Query understanding:** gpt-oss-20b (strict JSON) turns the prompt into `{themes[], characters[], moral, setting}`.
3. **Hybrid retrieval:** pgvector cosine top 20 + full-text `ts_rank` top 20, merged by reciprocal-rank fusion and filtered by `age_band`.
4. **Re-rank:** bge-reranker-base, keep the top 3.
5. **Context compression:** each chunk is reduced to a short summary (cached in Redis by chunk ID).
6. **Prompt assembly:** the retrieved tales are labelled "inspiration, don't copy"; the **story bible** is labelled "must stay consistent".
7. **Evaluation:** a 50-query hand-made gold set → recall@5 and MRR. The LLM judge (gpt-oss-120b) compares stories with and without RAG.

---

## 10. Versions: tasks, exit criteria, local tests, deployment

Every version follows the **release process in §11**. Each version lists **build tasks**, **what you learn**, and **exit criteria** (tested locally first, then live).

### V0: Foundations and deployment spike (week 1)
**Tasks**
- [ ] Machine setup (§1): Docker disk on D:, `HF_HOME`, uv, Python 3.12, GPU-in-Docker check.
- [ ] Accounts:
  - **HF** (check account age ≥ 30 days and a verified email),
  - Vercel,
  - **Groq** (record limits, enable Zero Data Retention),
  - Neon,
  - Upstash,
  - Cloudflare R2,
  - Langfuse,
  - Grafana Cloud,
  - GitHub (public repo).
- [ ] Monorepo skeleton (§5), pre-commit (ruff, mypy, detect-secrets), `.env.example`.
- [ ] Backend:
  - FastAPI `/health/live`, `/health/ready`, `/v1/echo`;
  - app built on `gr.Server` and started with `launch()` (required by ZeroGPU);
  - `@spaces.GPU` test function (a torch matmul) exposed as the Gradio API endpoint `gpu_check` (protected by `X-API-Key`);
  - `/v1/groq-check` (a 1-token gpt-oss-20b call).
- [ ] Frontend: Next.js page with a route handler calling the backend; shows "GPU OK / Groq OK".
- [ ] `infra/docker-compose.yml` with api (GPU), web, postgres, redis, SeaweedFS (S3; replaces MinIO, whose images are no longer published).
- [ ] CI: lint, type-check, tests, `next build`. Deploy workflows: push `backend/` to the HF Space; Vercel Git integration.
- [x] **Spike and ADR-0001:** custom FastAPI routes plus `@spaces.GPU` do **not** work (ZeroGPU's startup hook runs only inside `Blocks.launch()`, and GPU quota is charged per caller from Gradio request headers). **Decision: hybrid.** REST stays FastAPI; GPU work runs only as Gradio API endpoints on a `gr.Server`; the Next.js server calls them over Gradio's HTTP API with `X-API-Key` and an HF token. See ADR-0001.

**Learn:** Docker, environments, CI/CD, Spaces and ZeroGPU, the Groq API.

**Exit criteria:** the **live Vercel page shows "GPU OK" and "Groq OK"** from the live Space.

### V1: Text Storyteller + MCP v1 (weeks 2–3)
**Tasks**
- [ ] Domain models and Story JSON schema (§6.1).
- [ ] `GroqLLM` adapter (strict `json_schema`), planner prompt, repair step.
- [ ] `ModelRouter` with Groq limit table and Upstash/Redis token buckets (§6.3).
- [ ] SQLAlchemy 2 async models, Alembic migration 0001 (stories, characters, scenes, jobs).
- [ ] Async job queue and worker; `POST /v1/stories` → 202; `GET` jobs and stories; idempotency.
- [ ] Safety v1: rules + Prompt Guard 2 + gpt-oss-safeguard with `policy.md`.
- [ ] Next.js: prompt form → polling → storybook pages (text only).
- [ ] **MCP v1:** `create_story`, `get_story`, `get_job_status`, `story://{id}`, `bedtime_story`. Test in MCP Inspector (`mcp dev`) and Claude Desktop.
- [ ] Langfuse tracing on every LLM call.

**Learn:** prompting, structured outputs, REST (202, idempotency, RFC 7807), migrations, MCP basics, rate-limit design.

**Exit criteria:**
- the JSON is valid for ≥ 95% of 50 prompts;
- unsafe test prompts are refused;
- MCP tools work in Claude Desktop;
- deployed live.

### V2: Talking Storybook, speech ↔ text (week 4)
**Tasks**
- [ ] `GroqWhisper` (`whisper-large-v3-turbo`) + `FasterWhisper` fallback; ffmpeg normalisation to 16 kHz mono; reject audio > 60 s, > 10 MB, or silent.
- [ ] `GroqOrpheus` (split into sentences of ≤ 200 characters, concatenate with `pydub`) + `Kokoro` fallback; voice map in `voices.py`; TTS engine picked once per story.
- [ ] `BlobStore` (one `S3BlobStore`: SeaweedFS locally, R2 in prod) with presigned GET URLs.
- [ ] Frontend: `MicRecorder` (MediaRecorder API) and an audio player per page.
- [ ] Privacy: audio held only in memory; `docs/privacy.md`, `docs/dpia.md`.
- [ ] MCP: `transcribe`, `narrate`, `voices://list`.

**Learn:** speech pipelines, audio formats, object storage, privacy by design.

**Exit criteria:**
- spoken prompt → narrated story, live;
- Orpheus → Kokoro fallback shown by forcing the quota to 0.

### V3: Illustrated Storybook, text/speech → image (weeks 5–6)
**Tasks**
- [ ] `SDXLZeroGPU` (`@spaces.GPU(duration=…)`) and `SD15Local` (6 GB: fp16, `enable_model_cpu_offload`, VAE tiling).
- [ ] Character consistency (§6.5): visual string, seed, IP-Adapter reference from scene 1.
- [ ] Florence-2 image self-check with up to 2 retries.
- [ ] Pipeline ordering: scene 1 TTS and image first; the other scenes queued.
- [ ] Content-addressed image cache: `sha256(prompt|seed|model|lora)` → R2 key.
- [ ] `PlaceholderImage` fallback when the ZeroGPU quota is exhausted; UI message.
- [ ] Speech → image path ("draw a purple dragon").
- [ ] MCP: `illustrate`.
- [ ] **Start the synthetic data generator** (§7), running daily, quota-paced.

**Learn:** diffusion, VRAM management, async orchestration, caching, GPU as a scarce resource.

**Exit criteria:**
- first page in ≤ 15 s locally;
- the main character is recognisable in at least 4 of 5 scenes;
- the placeholder fallback works.

### V4: Story Memory, the RAG pipeline (weeks 7–8)
**Tasks**
- [ ] Prefect OSS flows: Gutenberg (catalogue + harvest), Standard Ebooks, TinyStories.
- [ ] Cleaning pipeline (§7), DVC tracking, data cards.
- [ ] Migration 0002: `tale_chunks`, `story_bibles`, HNSW and GIN indexes.
- [ ] Retriever: query understanding → hybrid search → RRF → re-rank → compression (§9).
- [ ] Story bible written after planning; used by the planner and by `continue_story` later.
- [ ] 50-query gold set; `evaluation/rag_eval.py`.
- [ ] MCP: `search_tales`.

**Learn:** legal data access, ETL, embeddings, hybrid retrieval, vector indexes, data versioning.

**Exit criteria:**
- recall@5 ≥ 0.8;
- RAG stories win ≥ 60% against no-RAG stories (gpt-oss-120b judge).

### V5: Smart Eyes, image → text and image → speech (weeks 9–10)
**Tasks**
- [ ] `GroqVision` (`qwen/qwen3.8-27b`, `reasoning_effort="none"`, strict JSON): child's drawing → character sheet → story.
- [ ] "Read this page aloud": image → caption (Florence-2 locally, or Groq vision) → TTS.
- [ ] Scrapy spider: Smithsonian Open Access + Wikimedia Commons; robots.txt on, AutoThrottle, licence filter, perceptual-hash dedupe, Florence-2 captions → `style_lora/` dataset (30–100 images).
- [ ] MCP: `describe_drawing`, `read_page_aloud`.
- [ ] ✅ **All six conversions working:** speech→text, text→speech, text→image, image→text, image→speech, speech→image.

**Learn:** VLMs, ethical scraping, image data cleaning.

**Exit criteria:** a drawing becomes a matching story, live.

### V6: My Own Storyteller, fine-tuning I and distillation (weeks 11–12)
**Tasks**
- [ ] `build_sft.py`: synthetic teacher outputs + TinyStories-derived prompts → chat-format `sft.jsonl` (5–10k records) with a train/validation/test split (no overlap); data card.
- [ ] T0 nanoGPT notebook.
- [ ] T1 QLoRA SFT of Qwen3-4B on your RTX 4050 (Unsloth Docker/WSL2) or Colab; MLflow; checkpoints to the HF Hub.
- [ ] Evaluation harness: JSON validity, readability, gpt-oss-120b judge; compare **base Qwen3-4B / fine-tuned / Groq gpt-oss-20b**.
- [ ] Serve: merge, push to HF Hub, `LocalLLM` adapter in `@spaces.GPU`; `LLM_FALLBACK` routes to it; UI toggle "use StoryWeaver-mini".

**Learn:** SFT, QLoRA, distillation, evaluation, model registry.

**Exit criteria:**
- valid JSON ≥ 98%;
- judge score ≥ 90% of gpt-oss-20b's;
- the fallback planner works live.

### V7: Aligned and Styled, fine-tuning II (weeks 13–14)
**Tasks**
- [ ] DPO pairs:
  - "chosen" from the best model,
  - "rejected" from degraded or base outputs,
  - a filtered slice of HH-RLHF/UltraFeedback,
  - labelled in **local Argilla**.
- [ ] T2 DPO (TRL) on Colab/Kaggle; evaluate.
- [ ] T3 SDXL style LoRA on Colab/Kaggle; load with `load_lora_weights`; CLIP score + character-consistency metric (SigLIP).
- [ ] T4 stretch: gpt-oss-20b QLoRA attempt; record the result in an ADR.
- [ ] Optional: WER evaluation of Groq Whisper vs faster-whisper on LibriVox / Common Voice clips (`jiwer`).
- [ ] Results table in the README.

**Learn:** preference alignment, image LoRA, multimodal evaluation.

**Exit criteria:**
- DPO wins ≥ 60% against SFT;
- the style LoRA improves consistency without lowering CLIP score.

### V8: Interactive and Safe (weeks 15–16)
**Tasks**
- [ ] `continue_story`: voice → STT → planner with the story bible → regenerate only the remaining scenes. Returns 409 while generating.
- [ ] Safety hardening:
  - stricter system prompt,
  - topic allow-list,
  - output length caps,
  - image negative prompts plus the safety checker.
- [ ] Red-team suite (`tests/fixtures/redteam.jsonl`, 100+ prompts including Base64, other languages and role-play jailbreaks) as a **CI gate**.
- [ ] `LLMJudgeGuard` fallback (fine-tuned Qwen3-4B + policy) and `RuleBasedGuard`, tested with the Groq guards turned off.
- [ ] Rate limiting per client (Upstash); UI messages for 429 and quota exhaustion.

**Learn:** defence in depth, adversarial testing, rate limiting.

**Exit criteria:**
- red-team suite passes at 100% with Groq guards and with the fallback guards;
- voice interrupt works live.

### V9: Production-grade (weeks 17–18)
**Tasks**
- [ ] Observability:
  - Langfuse traces,
  - Prometheus metrics → **Grafana Cloud** (job latency, queue depth, Groq quota usage, fallbacks used, ZeroGPU seconds),
  - dashboard JSON in `infra/grafana/`,
  - alerts.
- [ ] Resilience:
  - circuit breakers on Groq / R2 / Neon,
  - graceful 429/5xx handling,
  - fallback matrix tested,
  - `keepalive.yml` cron before demos (Space, Neon).
- [ ] Performance:
  - Redis caches (embeddings, chunk summaries, repeated TTS phrases),
  - **Locust** load test respecting the free quotas,
  - p50/p95 time to first page.
- [ ] **MCP v2:** Streamable HTTP on the Space with an API key; Claude Desktop connects to the live service.

**Learn:** observability, circuit breakers, graceful degradation, load testing.

**Exit criteria:**
- dashboard live;
- load-test report written;
- a story completes with Groq **and** the GPU disabled, using fallbacks.

### V10: Polish and portfolio (weeks 19–20)
- [ ] Export as PDF or EPUB (with audio links); story gallery.
- [ ] Optional multilingual (check Groq Whisper language support and Kokoro voices).
- [ ] README: architecture diagram, ADRs, model and data cards, evaluation table, privacy notice, 2-minute demo video.

---

## 11. Release process (every version)

1. **Local build:** `docker compose -f infra/docker-compose.yml --env-file .env up --build`. Use the local `.env`, which points at local postgres/redis/SeaweedFS and **the real Groq API**.
2. **Local gate (all must pass):**
   - `uv run pytest -m "unit or integration or contract"`
   - `npm run lint && npm run typecheck && npm run build && npx playwright test` (against localhost)
   - MCP tests (in-memory client); red-team suite from V8 onwards
   - the version's exit criteria, checked by hand
3. **Commit and tag:** `git tag vX.0`, then `git push --tags`.
4. **CI:**
   - re-runs the gate;
   - `deploy-backend.yml` pushes `backend/` to the HF Space repo with `huggingface_hub`. Secrets are set once in **Space Settings → Secrets**, not in Variables, which are public.
   - Vercel builds `frontend/` from the tag (production).
5. **Post-deploy smoke test** (`tests/e2e/smoke_prod.ts`): `/health/ready` returns 200, one short story is generated, and the page renders.
6. **Rollback:**
   - Vercel: **Instant Rollback**.
   - HF Space: `git revert`, or push the previous tag.
   - DB: migrations are backward-compatible only, so a rollback never needs a down-migration.

---

## 12. Testing strategy

| Layer | Tooling | What is covered |
|---|---|---|
| Unit | pytest, pytest-asyncio | Schema and repair, prompt builders, router/quota maths, token buckets, cache keys, idempotency, job state machine, safety rules, audio checks |
| Integration | pytest + docker compose (testcontainers) | API ↔ Postgres/pgvector/Redis/SeaweedFS S3; Groq **mocked** with `respx` (plus one live, opt-in test per model) |
| Contract | Snapshot of Story JSON + OpenAPI | Frontend types generated from OpenAPI; breaking-change check in CI |
| MCP | MCP SDK in-memory client | Every tool: success, bad input, API error |
| Frontend | Vitest, Testing Library, Playwright | Mic permission denied, polling, partial pages, error states |
| Evaluation | `evaluation/run.py` | JSON validity, FK grade, judge win rate, recall@5, CLIP score, consistency, WER |
| Safety | `redteam.py` | 100% pass required (CI gate from V8) |

**Edge cases to test**
- silent, empty or 2-minute audio;
- the wrong MIME type;
- a non-English prompt;
- an unsafe or injected prompt;
- a Groq 429 or 5xx;
- a Preview model removed (the model ID returns 404);
- ZeroGPU quota exhausted;
- a Neon cold start;
- a duplicate POST;
- `continue_story` while still generating (409);
- the R2 upload fails.

---

## 13. System design topics, by version
| Version | Concepts |
|---|---|
| V0 | Environments, containers, CI/CD, infrastructure constraints written up as ADRs |
| V1 | REST, 202 + polling, idempotency, migrations, **quota-aware routing**, token buckets |
| V2 | Object storage vs DB, presigned URLs, privacy by design |
| V3 | Async workers, pipeline parallelism, content-addressed caching, retries/backoff, scarce GPU |
| V4 | ETL, hybrid search, vector indexes (HNSW), RRF, data versioning |
| V5 | Polite crawling, licence-aware data lineage |
| V6–V7 | Model registry, reproducibility (DVC hash + MLflow run), A/B evaluation |
| V8 | Defence in depth, rate limiting, safety CI gate |
| V9 | Observability, circuit breakers, graceful degradation, load testing |

---

## 14. Risks and mitigations (researched 12 Oct 2026)

| # | Risk | Evidence | Mitigation |
|---|---|---|---|
| 1 | Groq **Preview** models (qwen3.8-27b, Orpheus, Prompt Guard 2, gpt-oss-safeguard) can be withdrawn at short notice | Groq models page; Llama Guard 4 shut down 5 Mar 2026; Llama removed from free/dev tiers 16 Aug 2026 (third-party) | Interfaces + local fallback for every Preview model; a contract test that fails loudly on model 404; ADR-0002 |
| 2 | Free-tier limits are small and **shared by the organisation**; safeguard is only 3 RPM | Groq rate-limits docs | Quota-aware router, caching, one safeguard call per story, quota message in the UI |
| 3 | Free-tier limit figures differ between sources | Docs vs third-party blogs | Treat your console's Limits page as the source of truth; config-driven limit table |
| 4 | Child voice sent to a third party (Groq, US) | COPPA amendment (voiceprints = personal info); Groq data docs | Zero Data Retention, no storage, parent-operated, privacy notice and DPIA, `STT_ENGINE=local` option |
| 5 | HF free accounts can't run Docker or CPU Spaces; ZeroGPU needs an account ≥ 30 days old | HF spaces-overview + ZeroGPU docs | ZeroGPU Gradio Space + FastAPI; check account age in V0 |
| 6 | Custom FastAPI routes + `@spaces.GPU` don't work on ZeroGPU (**confirmed in V0**: `No @spaces.GPU function detected during startup`) | V0 spike, `spaces` source | Hybrid design (ADR-0001): `gr.Server` + `launch()`; GPU only via Gradio API endpoints. Open for V3: GPU work started by the job worker has no caller to charge quota to |
| 7 | HF proxy timeouts and WebSocket problems | HF forum (user reports) | 202 + polling only; GPU calls < 60 s |
| 8 | Vercel 300 s limit, non-commercial | Vercel docs | Short route handlers only; portfolio use |
| 9 | 6 GB VRAM | Unsloth and diffusers docs | One model at a time, 4-bit, CPU offload; SDXL LoRA and gpt-oss-20b training on a T4 |
| 10 | Unsloth/bitsandbytes on Windows | Unsloth Windows docs | `unsloth/unsloth` Docker image or WSL2 |
| 11 | gpt-oss-20b QLoRA on T4 is borderline (about 14 GB; one report says it fails) | Unsloth tutorial vs Zenn report | Stretch goal only; main track is Qwen3-4B |
| 12 | Colab/Kaggle disconnects and unpublished limits | Colab FAQ | Checkpoint to HF Hub every 100–200 steps; runs < 3 h; `resume_from_checkpoint` |
| 13 | Synthetic data limited by Groq tokens per day (about 200K/day) | Groq limits | Start in V3; resumable, paced generator; local second teacher |
| 14 | Character drift (IP-Adapter weak with several characters) | arXiv 2510.10135, 2406.16537 | One main character per image, seed + template + IP-Adapter + Florence-2 check, style LoRA |
| 15 | Guard models can be bypassed (encodings, other languages) | promptfoo DB; arXiv 2504.11168 | Defence in depth; red-team CI gate; narrow topic scope |
| 16 | Gutenberg blocks scrapers | gutenberg.org robot policy | Catalogue CSV / harvest / mirror only |
| 17 | Celery unsupported on Windows | Celery FAQ | In-process async worker; Linux containers |
| 18 | C: drive nearly full | Detected (18 GB free) | Everything on D: |
| 19 | Free services idle out (Space after 48 h; Neon scales to zero; R2 card requirement unknown) | Provider docs | Keep-alive cron before demos; cold-start UI message; SeaweedFS S3 stays available locally |
| 20 | Licence traps | HF model cards | Only models in §2; licences recorded in model cards |
| 21 | Open-source infrastructure images withdrawn or relicensed (MinIO removed from Docker Hub, 2025) | `docker pull minio/minio` fails, 13 Oct 2026 | Pin exact image tags; reach storage only through the S3 API via one `S3BlobStore`, so the backing service (SeaweedFS, R2, or another S3-compatible store) can be swapped through `.env` |

---

## 15. End-to-end verification (V9, on the live Vercel site)
1. `GET /health/ready` returns 200; the Grafana dashboard is green.
2. Speak "a dragon who's scared of the dark": the transcript appears and page 1 plays in ≤ 20 s.
3. All scenes are illustrated with the same dragon, and each page has narration.
4. Say "make the dragon find a friend": only the remaining scenes are regenerated, consistent with the story bible.
5. Upload a drawing: you get a matching character and story.
6. Click "read this page aloud" on an image: you hear audio.
7. Claude Desktop (MCP over HTTP): `create_story` → `get_story` returns image and audio URLs.
8. Set the Groq key to an invalid value: the story still completes with the fine-tuned Qwen3-4B, Kokoro, faster-whisper and the fallback guards.
9. Disable the GPU path: the story completes with placeholder images.
10. The red-team suite passes at 100%. Langfuse shows a trace for every LLM call.

---

## 16. Sources (checked 12 Oct 2026)

**Groq**
- [Models](https://console.groq.com/docs/models)
- [Rate limits](https://console.groq.com/docs/rate-limits)
- [Deprecations](https://console.groq.com/docs/deprecations)
- [Structured outputs](https://console.groq.com/docs/structured-outputs)
- [Qwen3.8-27B](https://console.groq.com/docs/model/qwen/qwen3.8-27b)
- [Orpheus](https://console.groq.com/docs/model/canopylabs/orpheus-v1-english)
- [Your data / Zero Data Retention](https://console.groq.com/docs/your-data)
- [LoRA (Enterprise only)](https://console.groq.com/docs/lora)
- [Free-tier summary, third-party, Sep 2026](https://klymentiev.com/blog/groq-pricing)

**Hosting**
- [HF Spaces overview](https://huggingface.co/docs/hub/spaces-overview)
- [ZeroGPU](https://huggingface.co/docs/hub/spaces-zerogpu)
- [HF Inference pricing](https://huggingface.co/docs/inference-providers/pricing)
- [Vercel Hobby](https://vercel.com/docs/plans/hobby)
- [Vercel function limits](https://vercel.com/docs/functions/limitations)

**Data and infrastructure**
- [Neon](https://neon.com/pricing)
- [Upstash](https://upstash.com/pricing/redis)
- [Cloudflare R2](https://developers.cloudflare.com/r2/pricing/)
- [Langfuse](https://langfuse.com/pricing)
- [Grafana](https://grafana.com/pricing)
- [Qdrant](https://qdrant.tech/pricing/)
- [DagsHub](https://dagshub.com/pricing/)
- [Colab FAQ](https://research.google.com/colaboratory/faq.html)

**Training and tools**
- [Unsloth requirements](https://docs.unsloth.ai/get-started/beginner-start-here/unsloth-requirements)
- [Unsloth Windows](https://unsloth.ai/docs/get-started/install-and-update/windows-installation)
- [Unsloth gpt-oss tutorial](https://unsloth.ai/docs/models/gpt-oss-how-to-run-and-fine-tune/tutorial-how-to-fine-tune-gpt-oss)
- [Diffusers memory guide](https://huggingface.co/docs/diffusers/v0.33.1/optimization/memory)
- [Celery FAQ](https://docs.celeryq.dev/en/stable/faq.html)
- [Docker GPU](https://docs.docker.com/desktop/features/gpu/)

**Models**
- [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
- [Florence-2](https://huggingface.co/microsoft/Florence-2-base)
- [SD 1.5](https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5)
- [IP-Adapter](https://huggingface.co/h94/IP-Adapter)
- [SDXL-Turbo (excluded, non-commercial)](https://huggingface.co/stabilityai/sdxl-turbo)
- [Piper → GPL](https://github.com/OHF-Voice/piper1-gpl)

**Datasets and policy**
- [TinyStories](https://huggingface.co/datasets/roneneldan/TinyStories)
- [HH-RLHF](https://huggingface.co/datasets/Anthropic/hh-rlhf)
- [UltraFeedback](https://huggingface.co/datasets/openbmb/UltraFeedback)
- [Gutenberg robot policy](https://www.gutenberg.org/policy/robot_access.html)
- [Common Voice moved to MDC](https://discourse.mozilla.org/t/common-voice-dataset-segments-now-available-on-mdc/149060)

**Privacy and safety**
- [COPPA amendments](https://www.whitecase.com/insight-alert/unpacking-ftcs-coppa-amendments-what-you-need-know)
- [FTC voice guidance](https://www.fenwick.com/insights/publications/ftcs-new-coppa-guidance-on-recording-childrens-voices-five-tips-for-app-developers-and-toymakers-to-comply)
- [Guard-model bypass](https://arxiv.org/pdf/2504.11168v3)
- [IP-Adapter multi-character limits](https://arxiv.org/html/2510.10135v1)
