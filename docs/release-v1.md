# V1 release checklist

Backend first, frontend second, so the live site never calls a backend that lacks the V1 API.

## 1. Accounts (free, no card)

### Neon Postgres

1. Sign up at <https://neon.com>, create a project (Postgres 16 or newer). Pick a US region: the HF Space runs in
   the US, and a nearby database keeps each query fast.
2. Dashboard → **Connect**. Turn **Connection pooling off** and copy the **direct** connection string. It looks
   like `postgresql://user:password@ep-xxx.region.aws.neon.tech/neondb?sslmode=require&channel_binding=require`. <!-- pragma: allowlist secret -->
   - Why direct: the pooled endpoint runs PgBouncer in transaction mode, which breaks asyncpg's prepared
     statements and Alembic migrations. One Space process with a small pool (3 + 2) does not need a pooler.
   - Paste it exactly as Neon shows it: the backend converts it for asyncpg automatically.
3. Neon scales to zero after 5 minutes idle; the first query after that takes a few seconds (plan §3).

### Upstash Redis

1. Sign up at <https://upstash.com>, create a **Redis** database (free plan), US region.
2. Database page → **Connect** → TCP: copy the URL that starts with **`rediss://`** (TLS), e.g.
   `rediss://default:<password>@<name>.upstash.io:6379`.
3. Free plan: 500K commands a month. StoryWeaver uses about 3 commands per LLM call (quota check, reserve,
   correct), so roughly 20-30 per story.

## 2. HF Space settings

Space → Settings → **Variables and secrets**. Never put these in Variables (public).

| Name | Kind | Value |
|---|---|---|
| `DATABASE_URL` | **Secret** (new) | Neon direct connection string |
| `REDIS_URL` | **Secret** (new) | Upstash `rediss://` URL |
| `STORYWEAVER_API_KEY` | Secret (exists) | unchanged |
| `GROQ_API_KEY` | Secret (exists) | unchanged |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Secret (optional) | only if tracing is wanted in production |
| `APP_ENV` | Variable (exists) | `prod` |
| `ALLOWED_ORIGINS` | Variable (exists) | the Vercel domain |

Saving secrets restarts the Space. Until `v1.0.0` is deployed it still runs V0 and ignores them.

## 3. Release

1. **Local gate** (plan §11): backend unit + integration tests, frontend lint/typecheck/test/build, MCP tests,
   Docker stack + `bash scripts/local_smoke.sh .env` (33/33). Plus the V1 JSON-validity eval
   (`evaluation/results/json_validity_v1.md`, target 95% of 50 prompts).
2. **Backend:** tag the tested `dev-v1` commit `v1.0.0` and push the tag. CI runs, then the deploy workflow
   uploads `backend/` to the Space. On start the Space runs `alembic upgrade head` against Neon.
3. **Verify the Space:**
   - `GET /health/ready` shows `database: ok`, `redis: ok`, `worker: running`.
   - `POST /v1/stories` with the production key, then `GET /v1/stories/{id}` until `ready`.
   - Gradio `gpu_check` still works (ADR-0001).
4. **Frontend:** open a pull request `dev-v1` → `main` and merge it. Vercel deploys `frontend/` to production.
   Vercel env needs no change (`STORYWEAVER_API_URL`, `STORYWEAVER_API_KEY`, `HF_TOKEN`).
5. **Verify the live site:** a story end to end in the browser; an unsafe idea is refused; `/status` is green.
6. **MCP (optional):** point Claude Desktop's `STORYWEAVER_API_URL` at the Space URL with the production key.

## Rollback

- Frontend: Vercel → Deployments → **Instant Rollback** to the previous production deployment.
- Backend: push the previous tag's code to the Space (re-run the deploy workflow on `v0.0.5`). Migration 0001
  only adds tables, so V0 code runs unchanged against the migrated database (expand/contract, plan §6.2).
