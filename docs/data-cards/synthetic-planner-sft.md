# Data card: synthetic planner SFT records

| | |
|---|---|
| **What** | Chat-format pairs for distilling the story planner into StoryWeaver-mini (V6, plan §8 T1): the planner's exact prompt (system + idea) → a validated story JSON |
| **Made by** | `data_pipeline/flows/synth_generate.py` (generator version 1), started 21 Nov 2026 (V3) |
| **Teacher** | `openai/gpt-oss-120b` on Groq's free tier (Apache-2.0 weights), `reasoning_effort=low`, temperature 0.7. Each record names its teacher; `--teacher` can add a second one later |
| **Ideas** | Made-up, from fixed lists: 35 heroes × 30 places × 30 wishes = 31,500 ideas in one seeded order (`IDEA_SEED`), 5 or 6 scenes. No personal data, no third-party text. TinyStories-derived prompts (CDLA-Sharing-1.0) join in V6 via `--ideas` |
| **Licence / label** | Our own synthetic data, labelled `"label": "synthetic"` in every record |
| **Where** | `data_pipeline/data/raw/synth/` (git-ignored; DVC from V4): `records.jsonl` (kept), `rejects.jsonl` (failed after the repair call), `state.json` (tokens a day) |
| **Target** | 5,000 kept records (`--target`); `build_sft.py` makes the train/validation/test split in V6 |

## Filters

Every kept story passed the backend's own `StoryPlanner` validation, the same as a live story:

- the strict JSON schema;
- 5–6 scenes of 30–120 words;
- Flesch-Kincaid grade ≤ 4;
- valid and declared characters.

A failed first answer gets one repair call from the same teacher, and `repaired: true` marks it. A story still failing after the repair goes to `rejects.jsonl` with its problems and is never retried.

No safety model runs per record. The ideas are written by us and gentle by construction, and the safeguard model's 3 requests a minute are shared with live stories. V6's `build_sft.py` samples the records through the output safety check, and the gpt-oss-120b judge, before training.

## Record

```json
{"id": "<first 16 hex of sha1(idea|scenes|age)>", "idea": "A fluffy white bunny in a cave full of crystals wants to find the end of the rainbow.",
 "age_band": "4-6", "num_scenes": 5,
 "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "...<idea>...</idea>"},
              {"role": "assistant", "content": "{\"title\":\"Bunny and the Rainbow Cave\",...}"}],
 "teacher": "openai/gpt-oss-120b", "repaired": false, "tokens": 2873,
 "created_at": "2026-11-21T10:36:20+00:00", "generator_version": 1, "label": "synthetic"}
```

## Pacing and cost (measured 21 Nov 2026, 6 records)

- **Tokens per story:** about 2.8–3.0k for a first-try story, and about 8k when it needs the repair call. 2 of 6 stories were repaired: the teacher put a side character (a crab, an owl) in 3 or more scenes without declaring it. The average was about 4.6k.
- **Pacing:** calls go through the backend's `RoutedLLM` and `ModelRouter`. With `SYNTH_REDIS_URL` set to the Space's Upstash URL, they book the same Redis counters as live stories, so the two share Groq's per-organisation limits rather than racing them. With no fallback model, every record comes from the named teacher.
  - Per-minute waits happen inside the call (`PatientLLM`), so a repair held up for 40 s still runs rather than the idea starting over.
  - A daily-window wait stops the run.
- **Daily budget:** 120k of gpt-oss-120b's 200k tokens a day (`--daily-tokens`). The rest is left for live repairs and the planner's fallback. That is about 26 records a day, so **5,000 records take about 6 months on this teacher alone**, not the plan's 2–4 weeks. V6 options to speed it up, recorded per record as `teacher`:
  - add a second teacher with its own quota (gpt-oss-20b on days the site is quiet, or the local Qwen3-4B for easy ideas);
  - lower the target;
  - cut repairs with a prompt line about side characters.

## Running it

From `backend/`, with the backend's venv and `.env` (`GROQ_API_KEY`, `SYNTH_REDIS_URL`):

```powershell
uv run python ..\data_pipeline\flows\synth_generate.py              # until today's budget or the target
uv run python ..\data_pipeline\flows\synth_generate.py --dry-run 5  # the next ideas, no calls
```

The generator is resumable. Every record is appended and fsynced as it finishes, and a run skips ideas already kept or rejected. A crash loses at most the idea in flight, and its half-written line is skipped.

**Daily, on this Windows machine** (Task Scheduler, free; Prefect takes over in V4 with the other flows).
Create the folder once (`mkdir ..\data_pipeline\dataaw\synth`), since the log is redirected before the run starts:

```powershell
schtasks /Create /TN "StoryWeaver synth" /SC DAILY /ST 03:00 /TR "cmd /c cd /d D:\lenovo\work\storyweaver\backend && uv run python ..\data_pipeline\flows\synth_generate.py >> ..\data_pipeline\data\raw\synth\run.log 2>&1"
```

Groq's daily windows are rolling, so any fixed time works. 03:00 is a quiet hour for the site.
