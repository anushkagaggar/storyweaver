# Groq limits (free tier)

Free-tier limits apply **per organisation** (Groq docs: "Rate limits apply at the organization level"), so every
API key in the account shares them. The quota-aware router (`routing/quotas.py`, V1) must use these numbers.

- **Recorded on:** 14 Oct 2026
- **Sources:**
  1. Groq docs, [Rate limits](https://console.groq.com/docs/rate-limits), the base limits table.
  2. **This account's live response headers** (`x-ratelimit-limit-requests` = RPD, `x-ratelimit-limit-tokens` =
     TPM), read with one tiny request per model on 14 Oct 2026. They matched the docs exactly for every model
     checked, which confirms this table is the free-tier limit for this account.
- **Zero Data Retention enabled (Settings → Data Controls):** _not yet checked_

| Model ID | RPM | RPD | TPM | TPD | Audio s/h | Audio s/day | Live headers | Used for |
|---|---|---|---|---|---|---|---|---|
| `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 | — | — | ✅ RPD 1000, TPM 8000 | Planner (V1) |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 | — | — | ✅ RPD 1000, TPM 8000 | Repair, teacher, judge |
| `qwen/qwen3.8-27b` | 30 | 1,000 | 8,000 | 200,000 | — | — | ✅ RPD 1000, TPM 8000 | Vision (V5) |
| `whisper-large-v3-turbo` | 20 | 2,000 | — | — | 7,200 | 28,800 | ✅ RPD 2000 | Speech → text (V2) |
| `canopylabs/orpheus-v1-english` | 10 | 100 | 1,200 | 3,600 | — | — | ⚠️ not readable: needs terms acceptance | Text → speech (V2) |
| `meta-llama/llama-prompt-guard-2-22m` | 30 | 14,400 | 15,000 | 500,000 | — | — | ✅ RPD 14400, TPM 15000 | Prompt-injection guard (V1) |
| `openai/gpt-oss-safeguard-20b` | **3** | 1,000 | **2,000** | 200,000 | — | — | ✅ RPD 1000, TPM 2000 | Content-safety check (V1) |

RPM and TPD are not exposed in headers; those columns come from the docs table only.

## Notes

- **Orpheus TTS needs terms acceptance** before first use: the API returns `model_terms_required`. The org admin
  accepts once at <https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english>. Needed for V2,
  not V1. It is a self-service click-through, not an access request; check the terms fit plan rule 3 ("no gated
  models") before accepting.
- **Tightest limits for V1:** gpt-oss-safeguard-20b allows only **3 requests and 2,000 tokens per minute**, and the
  planner allows 8,000 tokens per minute. A story uses about 2K planner tokens (about 7K with a repair) and two
  safeguard calls, so at most about 1–2 stories per minute.
- **Daily ceiling:** planner TPD 200,000 ÷ ~2–3K tokens per story ≈ 60–100 stories a day without repairs, fewer
  with repairs (live test: 2 of 3 stories needed no repair).
- Groq can change free-tier limits without notice. Re-check the headers (or Settings → Limits in the console)
  before each release, and update `quotas.py` together with this file.
