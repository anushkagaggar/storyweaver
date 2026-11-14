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

`TTS_ENGINE` is deliberately **not** set on the Space, so it stays `auto`: Orpheus while its daily quota
lasts, then Kokoro. (The local Docker stack defaults it to `kokoro` instead, because local and production
share one Groq organisation and Orpheus's free tier is about one story a day.)

Saving a secret restarts the Space. Until the `v2.*` tag is deployed it runs V1 and ignores them.

## 3. Release

1. **Local gate** (plan §11): `uv run pytest -m unit` in `backend/`, `uv run pytest` in `mcp_server/`,
   frontend lint/typecheck/test/build, then the Docker stack and `bash scripts/local_smoke.sh .env`
   (48/48). Plus the two exit criteria below.
2. **Backend:** tag the tested `dev-v2` commit `v2.0.0` and push the tag. CI runs, then the deploy workflow
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

**Follow-up:** step 4's message reads "That voice is busy right now. Please try again shortly" while
`Retry-After` is ~14 hours (correct for a daily window). The useful advice, "or choose another voice", is
there, but a quota-exhausted message distinct from the rate-limited one would read better.

## Rollback

- Frontend: Vercel → Deployments → **Instant Rollback** to the previous production deployment.
- Backend: re-run the deploy workflow on `v1.0.2`. V2 changes no schema, so V1 code runs against the same
  database with nothing to undo — the cleanest rollback StoryWeaver has had so far.
- Narration audio already in B2 is simply not read by V1 code; nothing needs deleting.
