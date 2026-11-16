# Data protection impact assessment (DPIA)

*Draft, 13 Nov 2026, written at V2 (voice in, narration out); V3 sign-in, owners and share links added 16 Nov 2026. Method: the ICO's DPIA steps and the UK Age
Appropriate Design Code (Children's Code), kept proportionate to a portfolio project. Not legal advice; no lawyer
has reviewed it. Parent-facing summary: [`privacy.md`](privacy.md).*

## 1. Why a DPIA

StoryWeaver is used by a parent for children aged 4 to 6, and V2 adds **voice recordings**, and the 2025 US
COPPA amendments treat voiceprints as personal information (plan §6.6). It
sends text and voice to US AI providers. Plan §6.6 requires a DPIA before any public use.

**Scope and status.** It is a portfolio project, never distributed or sold, with a single operator. From V3
(16 Nov 2026) parents **must sign in with Google** to make or read stories. It is **not offered to the public**. If that changes, this DPIA must be redone and legally reviewed
(plan §6.6, "No public launch without legal review").

## 2. The processing

| Data | Source | Purpose | Stored? | Recipients (all US) |
|---|---|---|---|---|
| Voice recording (up to 60 s) | Parent's microphone | Turn the spoken idea into text | **No.** In memory only (browser, Vercel function, Space), then dropped | Groq Whisper; or faster-whisper on the Space CPU when `STT_ENGINE=local`, the quota is used up, or Groq fails |
| Story idea (≤ 500 chars) | Typed, or transcript | Safety check, story planning | Yes, `stories.prompt_text` in Neon, up to 30 days (or until deleted) | Groq (Prompt Guard, safeguard, planner) |
| Story text, characters | Generated | Show the story; second safety check | Yes, Neon | Groq (safeguard check) |
| Narration audio (MP3 per page) | Generated from story text | Playback | Yes, private Backblaze B2 bucket, keyed by story id | Groq Orpheus (text in, audio out) or Kokoro on the Space; B2 stores the result |
| Page pictures (PNG per page) | Generated from the scene's image prompt | Show the page | Yes, the same private B2 bucket, under the story's prefix, so deletion and the 30-day expiry remove them | Generated on the Space (placeholder on CPU; GPU models from V3 steps 3-4); B2 stores the result |
| Quota counters | System | Stay inside free-tier limits | Redis, minutes to a day | Upstash. No content |
| Google sign-in (V3) | The parent signs in at Google with scope `openid` only | Keep each parent's stories private to them | Google's account id (`sub`) is only ever held in memory on the Vercel server, which turns it into a **pseudonymous id** (HMAC-SHA256 under `AUTH_SECRET`, which only Vercel holds). Neon stores that id as `stories.owner_id`. No email, name or picture is requested or stored | Google (US) runs the sign-in; Vercel; Neon |
| Session cookie (V3) | Set after sign-in | Remember the signed-in parent | `sw_session`: httpOnly, SameSite=Lax, Secure in production, 7 days; a signed JWT holding only the pseudonymous id. A strictly necessary cookie (no analytics or tracking) | Stays in the parent's browser |
| Per-parent story counters (V3) | The pseudonymous id | Stop one parent using up the shared Groq and GPU quota (5 stories an hour, 15 a day) | Redis, keyed by a further HMAC of the id with the API key as secret; counters expire within a day | Upstash. No content |
| Share links (V3) | The owner presses "Share this story" | Let the owner show one story to someone else | Not stored: the link carries the story id and an expiry 15 minutes ahead, signed with a key derived from `USER_TOKEN_SECRET`. It is never renewed, however often it is opened or forwarded; picture and audio links on the shared page expire with it | Whoever the owner sends it to |
| Request logs | System | Operations | Platform logs (Vercel, Hugging Face) and app logs | Our code logs job ids, story ids, status and error classes. It does not log ideas, transcripts, story text or audio (checked 13 Nov 2026 by reading every logging call) |
| Traces | System | Debugging | Only if Langfuse keys are set; metadata only unless `LANGFUSE_CAPTURE_CONTENT=true` | Langfuse |

**Not collected:** names, emails, profile pictures, Google's own account id (beyond the moment of sign-in),
analytics or tracking cookies, IP addresses (beyond what Vercel and Hugging Face log as infrastructure; our code
no longer forwards or counts them), children's own voices by design (the parent records the idea).

**Access control (V3).** Every story has an owner. The Vercel server sends each backend call with a 60-second
JWT (`X-User-Token`, HS256 under `USER_TOKEN_SECRET`, held only by Vercel and the Space) naming the signed-in
parent; the backend checks it and serves, lists, shares or deletes a story only for its owner, answering 404 to
anyone else so ids can't be probed. Callers with only the API key (the MCP server, scripts) act as a separate
"operator" owner and can't see parents' stories. Stories made before sign-in have no owner and can't be opened;
they expire within 30 days. A share link lets anyone read one story, read-only, until 15 minutes after the owner
made it. The browser never holds the API key or either secret. Pictures and audio are in a private bucket,
reachable only by presigned links that expire after an hour (or with the share link, if sooner).

**Data flow of a recording.** Browser → `POST /api/speech/transcribe` (Vercel, body capped at 4 MB, read into
memory) → Space `/v1/speech/transcribe` (decoded in memory with PyAV; never written to disk) → Groq or
faster-whisper → text returned. Nothing in this path writes the audio to a file, database or log.

## 3. Necessity and proportionality

- **Voice is optional.** Typing works everywhere. Voice exists so a parent can dictate an idea while busy.
- **Minimum retained.** The recording isn't kept at all. The idea text is kept only because the story record
  needs it (idempotency and regeneration), which the open items below should shorten.
- **No profiling, no training, no advertising, no sale.**
- **Lawful basis (UK/EU GDPR framing).** Consent of the parent operating the app, given by choosing to record.
  Legitimate use for the typed idea and story. For a real launch the parent consent flow would need design.

## 4. Risks and mitigations

| # | Risk | Likelihood | Severity | Mitigation today | Residual |
|---|---|---|---|---|---|
| 1 | A child's voice reaches a third party (Groq, US) | Medium | High | Recording is never stored by us; Groq Zero Data Retention is on; parent-operated; `STT_ENGINE=local` keeps voice on the Space; a notice sits beside the mic button (`/privacy`) | **Low to medium** (a US transfer remains, see risk 7) |
| 2 | Groq keeps requests (text and audio) for its standard period (up to 30 days) | Low | Medium | **Global and Inference-API Zero Data Retention are enabled** (screenshot of Groq Console → Data Controls, 13 Nov 2026) | Low. Re-check after any change to the Groq organisation |
| 3 | The parent names the child or gives personal details in an idea | Medium | Medium | Safety policy refuses personal data, URLs and brands in input; stories use invented characters; notice says not to enter names | Low to medium: rules can miss a first name |
| 4 | Stories and ideas are kept longer than needed | Medium | Medium | **Slice 5.5:** "Delete this story" on every story (`DELETE /v1/stories/{id}`: audio files first, then rows) and automatic expiry after `STORY_RETENTION_DAYS` (30) by an hourly sweep; interrupted deletions are finished by the sweep; narration jobs can't leave audio behind (row lock) | Low |
| 5 | A story is read by someone other than its owner | Low | Medium | V3: owner-only reads, lists and deletes, checked on the backend against a signed per-request token; share links are owner-made, read-only and expire 15 minutes after creation (never renewed); `noindex` and `no-referrer` on shared pages | Low. A forwarded share link works for anyone until it expires (by design) |
| 11 | Sign-in adds personal data and a new recipient (Google) | Certain | Low | Scope `openid` only; Google's id never stored (pseudonymous HMAC instead); no email, name or picture; session cookie holds only that id | Low. Google learns that the person used StoryWeaver |
| 12 | A session or user token is stolen or forged | Low | Medium | httpOnly, SameSite=Lax session cookie, Secure in production; state, nonce and PKCE on sign-in; state-changing routes refuse other origins; backend tokens live 60 s and are signed with a secret the browser and the MCP server never hold | Low |
| 6 | Harmful or unsuitable generated content reaches a child | Low | High | Input checks (rules, Prompt Guard, safeguard policy), a full second check of the story before it is shown, fail-closed | Low; red-team suite arrives in V8 |
| 7 | Data leaves the UK/EU (all providers are US-based) | Certain | Medium | Disclosed in the notice. No special transfer mechanism is in place | Medium for a real launch |
| 8 | Logs capture content | Low | High | Logging reviewed; Langfuse content capture off by default; the 422 error body doesn't echo input | Low. Re-check at each new logging call |
| 9 | Credentials leak (API key, B2 key, Groq key) | Low | High | Secrets only in Space Secrets and Vercel env; key never reaches the browser (smoke test checks the page); B2 key limited to one bucket | Low |
| 10 | Provider outage or withdrawal changes where data goes | Medium | Low | Fallback engines run on our own server (faster-whisper, Kokoro), so data does not flow to another third party | Low |

## 5. Children's Code (UK) standards, checked briefly

| Standard | Position |
|---|---|
| Best interests of the child | Stories are gentle, end happily and pass two safety checks |
| Age-appropriate application | Aimed at 4 to 6; the **parent** operates it. No child-facing data entry |
| Transparency | `privacy.md` in plain language, and a short `/privacy` page linked beside the mic button (slice 5.5) |
| Detrimental use of data | No profiling, ads, nudges or sale |
| Default settings / data minimisation | Sign-in asks Google for `openid` only and keeps a pseudonymous id, not the account id; recordings never stored; Langfuse content off by default; stories private to their owner, sharing opt-in and time-limited |
| Data sharing | Only the processors in §2, none for their own purposes (subject to action 1) |
| Geolocation, parental controls, nudge techniques, connected toys | Not applicable / not used |
| Online tools (data subject rights) | Self-service "Delete this story" on every story; automatic expiry after 30 days |

## 6. Actions

| # | Action | Owner | Needed before |
|---|---|---|---|
| 1 | ~~Check Groq Console → Data Controls and enable Zero Data Retention~~ **Done: enabled, confirmed 13 Nov 2026** | Project owner | — |
| 2 | Decide whether production sets `STT_ENGINE=local` (voice never leaves the Space) | Project owner | Same |
| 3 | ~~Add `DELETE /v1/stories/{id}` and automatic expiry~~ **Done in slice 5.5** (30 days; B2 bucket lifecycle must be "Keep only the last version" so deleted files don't linger as old versions) | Code + owner (B2 setting) | — |
| 4 | ~~Link the notice from the app and show a one-line note beside "Say your idea"~~ **Done in slice 5.5** (`/privacy`) | Code | — |
| 5 | Re-read every new logging call and trace; keep content out | Code review | Each release |
| 6 | Legal review, and consent design if ever opened to the public | Owner | Public launch |
| 7 | Keep `AUTH_SECRET` stable: changing it gives every parent a new pseudonymous id, so their stories stop being theirs (they still expire on schedule) | Owner | Each secret rotation |

## 7. Sign-off

Reviewed by the project owner on ____ . Residual risk is accepted for private portfolio use only. Review again
at V3 (pictures), V8 (red-team suite) and before any public launch.
