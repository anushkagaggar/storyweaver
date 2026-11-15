# V2 release checklist

Talking Storybook: speech → text → story → speech. Backend first, frontend second, so the live site never
calls a backend that lacks the V2 API.

V2 adds narration audio, which is the first thing StoryWeaver *stores*. That means one new account
(Backblaze B2) and five new Space settings. Everything else is unchanged from [V1](release-v1.md).

## 1. Backblaze B2 (free tier, no card)

R2 needs a card, so narration audio goes to B2 (ADR-0004). The free tier is 10 GB of storage and 1 GB of
downloads a day; a narrated scene is roughly 15-40 KB of MP3.

1. Sign up at <https://www.backblaze.com/cloud-storage>, verify the email, and enable B2.
2. **Buckets** → **Create a Bucket**:
   - Name: `storyweaver-audio` (globally unique, so add a suffix if it is taken).
   - Files in bucket are: **Private**. The backend hands out presigned GET URLs; the bucket is never public.
   - Default encryption and object lock: off.
3. Open the bucket and read the **Endpoint** line, e.g. `s3.us-west-004.backblazeb2.com`. It gives two
   settings at once:
   - `S3_ENDPOINT_URL` = `https://s3.us-west-004.backblazeb2.com` (the whole URL, with scheme)
   - `S3_REGION` = `us-west-004` (**the middle segment**, not an AWS region)

   There is no separate "region" field in the console: the region is part of the endpoint. The default in
   `config.py` is `us-east-1`, which is only correct for real AWS; leaving it against a B2 endpoint fails
   SigV4 signing with `403 SignatureDoesNotMatch`, and only in production, because SeaweedFS does not check.
4. **Application Keys** → **Add a New Application Key**:
   - Allow access to: **this bucket only** (not "all buckets").
   - Type of access: **Read and Write**.
   - Copy `keyID` → `S3_ACCESS_KEY_ID` and `applicationKey` → `S3_SECRET_ACCESS_KEY`. The secret is shown
     **once**; if it is lost, make a new key.

To read the endpoint from the API instead of the console (PowerShell):

```powershell
$pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("<keyID>:<applicationKey>"))
(Invoke-RestMethod -Uri "https://api.backblazeb2.com/b2api/v3/b2_authorize_account" `
  -Headers @{Authorization = "Basic $pair"}).apiInfo.storageApi.s3ApiUrl
```

## 2. HF Space settings

Space → Settings → **Variables and secrets**. Variables are public, so credentials are Secrets only.

| Name | Kind | Value |
|---|---|---|
| `S3_ACCESS_KEY_ID` | **Secret** (new) | B2 `keyID`, scoped to the one bucket |
| `S3_SECRET_ACCESS_KEY` | **Secret** (new) | B2 `applicationKey` |
| `S3_ENDPOINT_URL` | **Variable** (new) | `https://s3.<region>.backblazeb2.com` |
| `S3_REGION` | **Variable** (new) | `<region>`, the middle segment of the endpoint |
| `S3_BUCKET` | **Variable** (new) | the bucket name |
| everything from V1 | unchanged | `DATABASE_URL`, `REDIS_URL`, `STORYWEAVER_API_KEY`, `GROQ_API_KEY`, `APP_ENV`, `ALLOWED_ORIGINS` |

`TTS_ENGINE` was initially left unset on the Space, so it stayed `auto`: Orpheus while its daily quota
lasts, then Kokoro. **Since 15 Nov 2026 it is set to `kokoro`** (Variable), because `auto` made narration
take many minutes per story — see the pacing follow-up below. Removing the Variable restores the nicer
Orpheus voice at that cost. The local Docker stack defaults it to `kokoro` too, because local and
production share one Groq organisation and Orpheus's free tier is about one story a day.

Saving a secret restarts the Space. Until the `v2.*` tag is deployed it runs V1 and ignores them.

## 3. Release

1. **Local gate** (plan §11): `uv run pytest -m unit` in `backend/`, `uv run pytest` in `mcp_server/`,
   frontend lint/typecheck/test/build, then the Docker stack and `bash scripts/local_smoke.sh .env`
   (48/48). Plus the two exit criteria below.
2. **Backend:** tag the tested `dev-v2` commit and push the tag. CI runs, then the deploy workflow
   uploads `backend/` to the Space. On start the Space runs `alembic upgrade head` against Neon, which is a
   no-op here: **V2 adds no migration.** The head is still `0002` (V1.1), because narration reuses columns
   `0001` already created (`scenes.audio_key`, `characters.voice_id`, `stories.tts_engine`).
3. **Verify the Space:**
   - `GET /health/ready` shows `database: ok`, `redis: ok`, `worker: running`.
   - `GET /v1/speech/voices` lists both engines.
   - `POST /v1/speech/transcribe` with a short recording returns its text.
   - A story reaches `ready` and its scenes get `audio_url`s that play (presigned B2 URLs).
4. **Frontend:** open a pull request `dev-v2` → `main` and merge it. Vercel deploys `frontend/`. No Vercel
   env change is needed: narration audio is fetched from presigned URLs, not proxied.
5. **Verify the live site:** record a spoken idea with the mic, get a narrated story, play a page's audio,
   and check `/privacy` and `/status`.

## 4. Exit criteria

### Spoken prompt → narrated story, live

Done in the browser on the production site (step 5 above).

### Orpheus → Kokoro fallback, by forcing the quota to zero

Verified locally on **14 Nov 2026** against the Docker stack, with `TTS_ENGINE=auto` (the production
setting) overridden for the run: `TTS_ENGINE=auto docker compose -f infra/docker-compose.yml --env-file .env up -d api`.

The daily budget is a Redis counter keyed
`quota:<model>:<unit>:<window seconds>:<window start>`. Orpheus is `tpd=3600` with the router's 0.9 margin,
so the effective limit is **3240 characters a day** (Groq counts one token per input character).

| Step | Command | Result |
|---|---|---|
| 1. Baseline, no `voice` | `POST /v1/speech/narrate {"text":"Goodnight, little dragon."}` | `200`, `x-tts-engine: orpheus`, `x-tts-voice: hannah` |
| 2. Force the budget to 0 | `redis-cli SET "quota:canopylabs/orpheus-v1-english:tokens:86400:$WIN" 999999` | counter read `25` first — step 1's own 25 characters, which proves Orpheus was really charged |
| 3. Repeat step 1 | identical request | `200`, `x-tts-engine: kokoro`, `x-tts-voice: af_heart`, audio plays |
| 4. Name an Orpheus voice | `{"text":"Hello.","voice":"hannah"}` | `429` + `Retry-After: 51650`, `code: tts_busy` |
| 5. Clean up | `redis-cli DEL "<the key>"` | Orpheus available again |

The decision chain in the Space logs:

```
INFO  speech  Narrated 25 characters with orpheus/hannah: 2.1 s, 17325 bytes.
WARN  router  Quota exhausted for ['canopylabs/orpheus-v1-english']:
              ['canopylabs/orpheus-v1-english: tokens/86400s limit 3240 reached']
INFO  tts     Orpheus can't cover this narration today; using Kokoro.
INFO  kokoro  Loaded kokoro-82m-fp32 in 7.9 s.
INFO  speech  Narrated 25 characters with kokoro/af_heart: 1.5 s, 12717 bytes.
```

Step 4 is the half that is easy to miss: when a request *names* an Orpheus voice, the router raises instead
of quietly substituting a Kokoro one. Mid-story that would change a character's voice, so the engine is
chosen once per story and a named voice is never silently swapped (plan §6.3).

Two things this release left undone — Orpheus pacing latency on `auto`, and the `tts_busy` wording —
are carried into **V3's task list in [`plan.md`](../plan.md) §10**, which is where work to do belongs.

## Release history, and two failures worth keeping

### `v2.0.0` — never deployed (CI red)

An MCP test asserted that a directory is unreadable while the size cap was patched to 50 bytes. A directory
stats as 4096 bytes on Linux against 0 on Windows, so the call hit the "too big" branch instead: green
locally, red on `ubuntu-latest`. The deploy job is gated on CI, so nothing was uploaded.

### `v2.0.1` — deployed and crashed the Space (14 Nov 2026, ~34 minutes)

`S3_ENDPOINT_URL` was set to `s3.us-east-005.backblazeb2.com`, copied from the B2 console **without the
scheme**. boto3 refuses a scheme-less endpoint while the *client is being constructed*:

```
ValueError: Invalid endpoint: s3.us-east-005.backblazeb2.com
```

That construction happens in `build_services()` inside the app's lifespan, so the whole app failed to
start. The Space went to `RUNTIME_ERROR` and every route died — stories, health and the GPU endpoint — over
one feature's setting. Recovered by re-running the deploy workflow on `v1.0.1`.

Nothing in the gate could have caught it. CI never builds an S3 client with production values, and the
local stack uses `http://s3:8333`, which has a scheme. **A config-shaped risk needs a config-shaped check**:
before the next release that adds a setting, exercise it with the production value, for example

```powershell
docker compose -f infra/docker-compose.yml --env-file .env exec -T -e S3_ENDPOINT_URL='<the Space value>' `
  api python -c "from storyweaver.config import Settings; from storyweaver.services import build_blob_store; build_blob_store(Settings())"
```

Three changes in `v2.0.2` so it cannot repeat:

- a scheme-less endpoint is normalised to `https://…` (`config.py`), so the console value works as pasted;
- `build_blob_store` catches a store it cannot build, logs the endpoint, region and bucket (never the
  credentials) and returns `None`, so narration degrades instead of the app dying;
- `/health/ready` gained an `audio_storage` check (`ok` / `not_configured` / `misconfigured`) that reports
  the degraded state without failing readiness, so it is never silent.

A bad endpoint is deliberately **not** rejected in the validator: raising there makes `Settings()` itself
invalid, which fails at start-up just as hard as the crash it was meant to prevent.

### After release: narration failed for ~5 hours on wrong B2 credentials

Every `tts` job failed `upstream_unavailable` after three attempts, and the hourly expiry sweep logged
`Delete of stories/<id>/* failed: ClientError`. The cause was simply that the wrong values had been entered
for `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` in the Space Secrets. Re-entering them fixed it.

What made it slow to diagnose was ours, not Backblaze's: **every S3 failure was logged as
`type(exc).__name__`**, so a wrong key, a missing bucket and an unreachable host all read `ClientError`.
The code that identifies the fault sits in `exc.response["Error"]["Code"]` and was being discarded.

Two things that narrowed it down, worth reusing:

- Kokoro's `phonemizer` lines in the log prove synthesis ran, so the failure had to be *after* audio
  existed — which leaves the upload.
- The expiry sweep failing too meant it was not TTS-specific: a plain `delete` was failing against B2 as
  well.

Shipped in `v2.0.3`:

- `s3_error()` reports the code and status, e.g. `ClientError(SignatureDoesNotMatch, HTTP 403)`;
- `build_blob_store` logs what the process resolved — `endpoint=… region=… bucket=… key=...0001 (25 chars)`
  — the last 4 characters and length of the key id only, never the secret. A wrong key is then one glance
  away;
- every S3 setting is stripped of surrounding whitespace, since a console paste can carry a newline.

To test B2 settings without deploying, run the four operations the Space performs (`head_bucket`,
`put_object`, `delete_object`; a bucket-scoped key gets `AccessDenied` on `list_buckets`, which is expected
and unused).

### Lesson from `v2.0.0`: the gate runs on Windows, CI runs on Linux

For anything that touches the filesystem, run the CI steps on Linux before tagging:

```powershell
docker run --rm -v "<a copy of mcp_server>:/w" -w /w ghcr.io/astral-sh/uv:python3.12-bookworm-slim `
  sh -c "uv sync --frozen -q && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q"
```

Copy the folder first: mounting `mcp_server/` directly would let the container overwrite the Windows `.venv`.

## Rollback

- Frontend: Vercel → Deployments → **Instant Rollback** to the previous production deployment.
- Backend: re-run the deploy workflow on `v1.0.2`. V2 changes no schema, so V1 code runs against the same
  database with nothing to undo — the cleanest rollback StoryWeaver has had so far.
- Narration audio already in B2 is simply not read by V1 code; nothing needs deleting.
