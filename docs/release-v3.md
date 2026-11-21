# V3 release checklist

Illustrated Storybook: text and speech → pictures, sign-in, single pictures and MCP `illustrate`. Backend first, frontend second.

The old site keeps working against the new backend, because a call with no user token is the "operator" owner. The new site, however, needs the V3 API.

## 1. Before tagging (local, done 21 Nov 2026 unless marked)

- [x] **Backend:** 403 unit and 80 integration tests pass. CI on Linux passed for `dev-v3` `5e424b9`.
- [x] **MCP:** 47 tests pass. **Frontend:** lint, typecheck and 63 Vitest tests pass.
- [x] **Smoke:** 71 of 71 pass against the rebuilt Docker stack.
- [x] **Production config, tested on the test Space** `gaggaranushka/storyweaver-test` (20 Nov 2026): `IMAGE_ENGINE=sdxl` and `HF_TOKEN`, the worker calling its own Space's `illustrate` route, and SDXL with the picture check on ZeroGPU.
- [x] **Next.js security upgrade:** `next` and `eslint-config-next` 16.4.6 (12 days old, a security exception to the two-week rule). The Windows install dropped `@emnapi/core` and `@emnapi/runtime` from the lock, which broke `npm ci` on Linux, so the lock was regenerated in `node:24-alpine`. Build and smoke (71/71) pass on 16.4.6.
- [x] **The V3 exit criteria** (plan §10). Locally the first page took **21 s** on 21 Nov 2026: text at 8.1 s after one planner repair, then the picture in 13 s (SD 1.5 at about 7.7 s plus the self-check on CPU at 5.5 s). Decided 21 Nov 2026: the "first page ≤ 15 s" criterion is measured on the ZeroGPU Space, where the check runs on the GPU. It is checked in step 4 below.
- [ ] **Pause the test Space.**

## 2. HF Space settings (production `gaggaranushka/storyweaver`)

| Name | Kind | Value |
|---|---|---|
| `USER_TOKEN_SECRET` | **Secret** (new) | 32+ random bytes; **the same value as on Vercel** |
| `HF_TOKEN` | **Secret** (new) | a **read-only** token of the account whose ZeroGPU quota draws the pictures |
| `IMAGE_ENGINE` | Variable (new) | `sdxl` |
| everything from V2 | unchanged | `DATABASE_URL`, `REDIS_URL`, `STORYWEAVER_API_KEY`, `GROQ_API_KEY`, `S3_*`, `APP_ENV`, `ALLOWED_ORIGINS` |

**Migrations** `0003`–`0005` run at start-up against Neon (`alembic upgrade head`). They are expand-only:
- `0003`: `scenes.image_status`;
- `0004`: `stories.owner_id`;
- `0005`: `stories.kind`.

Existing V2 stories become the operator's, and they expire as before.

## 3. Vercel env (production)

| Name | Value |
|---|---|
| `AUTH_SECRET` | 32+ random bytes (session cookie and pseudonymous ids) |
| `USER_TOKEN_SECRET` | the same value as the Space Secret |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | an OAuth client (Web application) in Google Cloud, scope `openid` only |
| `AUTH_URL` | `https://storyweaver-eight-woad.vercel.app` (no trailing slash) |

In the Google Cloud OAuth client, set **Authorised redirect URIs** to `https://storyweaver-eight-woad.vercel.app/api/auth/callback/google`.

The OAuth consent screen can stay in "Testing" with your own account as a test user. "In production" is needed only for strangers to sign in.

**These sign-in settings have not been tried on any deployed site yet.** This is the config-shaped risk this release carries, as `S3_ENDPOINT_URL` was in v2.0.1. Check it first: on the Vercel preview of `dev-v3`, sign in once (add the preview URL as a second redirect URI while testing).

## 4. Release

1. Tag the tested `dev-v3` commit `v3.0.0` and push the tag. CI runs, then the deploy workflow uploads `backend/` to the Space.
2. **Verify the Space:**
   - `/health/ready` reports `database`, `redis` and `worker` OK.
   - `GET /v1/gpu-route-check` returns 200.
   - A story reaches `ready` with pictures. This is the full worker path with a database, seen live for the first time.
   - **Exit criterion:** time the first page (text and picture). The target is ≤ 15 s.
3. Merge `dev-v3` → `main` (pull request). Vercel deploys.
4. **Verify the live site:**
   - sign in with Google;
   - make a story by voice and see its pictures;
   - draw "a purple dragon";
   - share a story and open the link signed out;
   - delete a story;
   - check `/status`.
5. Rollback: Vercel Instant Rollback, and push `v2.0.3` to the Space. The migrations are expand-only, so V2 runs on the V3 schema.
