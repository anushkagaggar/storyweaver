# Data protection impact assessment (DPIA)

*Draft, 13 Nov 2026, written at V2 (voice in, narration out). Method: the ICO's DPIA steps and the UK Age
Appropriate Design Code (Children's Code), kept proportionate to a portfolio project. Not legal advice; no lawyer
has reviewed it. Parent-facing summary: [`privacy.md`](privacy.md).*

## 1. Why a DPIA

StoryWeaver is used by a parent for children aged 4 to 6, and V2 adds **voice recordings**, and the 2025 US
COPPA amendments treat voiceprints as personal information (plan §6.6). It
sends text and voice to US AI providers. Plan §6.6 requires a DPIA before any public use.

**Scope and status.** It is a portfolio project, never distributed or sold, with a single operator and no
accounts. It is **not offered to the public**. If that changes, this DPIA must be redone and legally reviewed
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
| Per-visitor story counters (V3) | The visitor's IP, forwarded by the Vercel route handler as `X-Client-IP` | Stop one visitor using up the shared Groq and GPU quota (5 stories an hour, 15 a day) | Redis, keyed by an HMAC-SHA256 of the IP with the API key as secret (not reversible by hashing every IPv4 address); counters expire within a day. The IP itself is never stored or logged | Upstash. No content |
| Request logs | System | Operations | Platform logs (Vercel, Hugging Face) and app logs | Our code logs job ids, story ids, status and error classes. It does not log ideas, transcripts, story text or audio (checked 13 Nov 2026 by reading every logging call) |
| Traces | System | Debugging | Only if Langfuse keys are set; metadata only unless `LANGFUSE_CAPTURE_CONTENT=true` | Langfuse |

**Not collected:** names, emails, accounts, cookies, analytics, IP addresses (beyond what Vercel and Hugging
Face log as infrastructure; our own counters hold only a keyed hash, above), children's own voices by design
(the parent records the idea).

**Access control.** There are no user accounts. A story is readable by anyone with its UUID link (a capability
URL; random v4 UUIDs). The browser never holds the backend API key: only Vercel server code calls the backend.
Audio files are in a private bucket and are reachable only by presigned links that expire after an hour.

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
| 5 | A story link is guessed or shared | Low | Low | Random v4 UUIDs; no listing; content only after the output safety check | Low |
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
| Default settings / data minimisation | No accounts; recordings never stored; Langfuse content off by default |
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

## 7. Sign-off

Reviewed by the project owner on ____ . Residual risk is accepted for private portfolio use only. Review again
at V3 (pictures), V8 (red-team suite) and before any public launch.
