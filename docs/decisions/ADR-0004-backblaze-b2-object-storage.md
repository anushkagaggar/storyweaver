# ADR-0004: Backblaze B2 instead of Cloudflare R2 for narration audio

- **Status:** Accepted (13 Nov 2026, V2 slice 3).
- **Deciders:** project owner

## Context

V2 stores one MP3 per scene (about 150 KB, so about 1 MB per story). Readers fetch them from the browser.
The plan chose Cloudflare R2. StoryWeaver is a portfolio project with a free, no-card rule
(it is never distributed or sold).

R2's free tier needs an "R2 subscription" added through a checkout flow
([Cloudflare docs](https://developers.cloudflare.com/r2/get-started/)), which asks for a payment method
before the first bucket can be created.

## Decision

Use **Backblaze B2** in production, through the same `S3BlobStore` that talks to SeaweedFS locally
(`storage/blob.py`, boto3, S3 API):

- 10 GB free storage, and sign-up without a card
  ([Backblaze](https://www.backblaze.com/blog/free-isnt-always-free-a-guide-to-free-cloud-tiers/)).
  That is roughly 10,000 narrated stories.
- One **private** bucket (`storyweaver-audio`, region `us-east-005`) and an application key limited to
  that bucket. The Space reads it from **Secrets** `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`. Its
  **Variables** are `S3_ENDPOINT_URL=https://s3.us-east-005.backblazeb2.com`, `S3_REGION=us-east-005` and
  `S3_BUCKET`.
- Readers get **presigned GET URLs** (SigV4, `AUDIO_URL_TTL_SECONDS`, default 1 hour) in
  `GET /v1/stories/{id}`. The UI polls that endpoint anyway, so it always has fresh URLs. Nothing is public.
- Keys are `stories/{story_id}/audio/scene-{idx}.mp3`. A retried job overwrites its own key.

Verified live on 13 Nov 2026 from the API container:
- An upload with the bucket-scoped key worked.
- A presigned GET returned 200 `audio/mpeg` with the same bytes.
- An unsigned GET returned 401, so the bucket is private.
- After delete, a GET returned 404.

## Alternatives considered

- **Cloudflare R2.** It needs a payment method, so it was rejected under the no-card rule. Its zero egress
  fees don't matter at this scale.
- **Supabase Storage.** About 1 GB free, and its signed URLs mostly go through its own SDK, not the S3 API.
  Rejected.
- **Postgres `bytea`, served by the API.** This costs nothing, but it puts audio bytes into Neon's 0.5 GB
  free tier and routes every play through the Space. Rejected.

## Consequences

- No code depends on the provider. Moving to R2 or any S3 store later only changes the `S3_*` settings.
- botocore ≥ 1.36 adds CRC checksums to uploads by default. The client sends them only when an operation
  requires one, so S3-compatible stores that don't accept them still work.
- **Set the bucket's lifecycle to "Keep only the last version".** B2 keeps every version by default, so
  overwritten or deleted audio would otherwise still use storage.
- Free downloads are capped per day (reported as 1 GB/day without a card, about 1,000 story plays). That is
  far beyond a portfolio's traffic.
- Generated narration is stored. A child's recorded voice is still never stored (plan §6.6).
