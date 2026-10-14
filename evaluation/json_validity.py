"""V1 exit criterion (plan §10): the planner's JSON is valid for at least 95% of 50 prompts.

A prompt passes when the final story meets the schema and every content rule (5/6 scenes, 40-120 words per
scene, reading grade <= 4, declared characters), allowing the planner's single repair call, exactly as in
production. Uses the real Groq models through the quota router, so it respects free-tier limits; when a
day's quota runs out it stops cleanly and a re-run resumes where it left off.

Run from the repo root (spends real free-tier quota, roughly 2-7K tokens per prompt):
    uv run --directory backend python ../evaluation/json_validity.py
Needs GROQ_API_KEY (read from the environment or the repo's .env).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from groq import AsyncGroq  # noqa: E402
from storyweaver.adapters.groq_llm import GroqLLM  # noqa: E402
from storyweaver.llm.client import LLMError, LLMRateLimitedError  # noqa: E402
from storyweaver.llm.planner import PlanningError, StoryPlanner, StoryRequest  # noqa: E402
from storyweaver.routing.llm import RoutedLLM  # noqa: E402
from storyweaver.routing.router import ModelRouter, QuotaExhaustedError  # noqa: E402
from storyweaver.routing.store import InMemoryQuotaStore  # noqa: E402

PROMPTS = ROOT / "evaluation" / "data" / "prompts_v1.txt"
RESULTS = ROOT / "evaluation" / "results" / "json_validity_v1.jsonl"
SUMMARY = ROOT / "evaluation" / "results" / "json_validity_v1.md"
TARGET = 0.95
LONG_WAIT_SECONDS = 15 * 60  # a wait longer than this means the daily quota is spent: stop and resume later


@dataclass
class Outcome:
    index: int
    prompt: str
    num_scenes: int
    valid: bool
    repaired: bool
    problems: list[str]
    tokens: int
    seconds: float
    finished_at: str


def groq_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("GROQ_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        raise SystemExit("GROQ_API_KEY is not set (environment or .env).")
    return key


def load_prompts() -> list[str]:
    lines = PROMPTS.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def load_done() -> dict[int, Outcome]:
    if not RESULTS.exists():
        return {}
    done = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            outcome = Outcome(**json.loads(line))
            done[outcome.index] = outcome
    return done


async def run_one(planner: StoryPlanner, index: int, prompt: str) -> Outcome:
    scenes = 5 if index % 2 == 0 else 6
    start = time.monotonic()
    while True:
        try:
            result = await planner.plan(
                StoryRequest(prompt=prompt, num_scenes=scenes), story_key=f"eval-{index}"
            )
        except QuotaExhaustedError as exc:
            if exc.retry_after_seconds > LONG_WAIT_SECONDS:
                raise
            print(f"    quota: waiting {exc.retry_after_seconds:.0f}s")
            await asyncio.sleep(exc.retry_after_seconds + 1)
            continue
        except LLMRateLimitedError as exc:
            await asyncio.sleep((exc.retry_after_seconds or 30) + 1)
            continue
        except PlanningError as exc:
            tokens = sum(c.prompt_tokens + c.completion_tokens for c in exc.calls)
            return Outcome(index, prompt, scenes, False, True, exc.problems, tokens, _since(start), _now())
        tokens = sum(c.prompt_tokens + c.completion_tokens for c in result.calls)
        return Outcome(index, prompt, scenes, True, result.repaired, [], tokens, _since(start), _now())


def _since(start: float) -> float:
    return round(time.monotonic() - start, 1)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_summary(prompts: list[str], done: dict[int, Outcome]) -> None:
    outcomes = [done[i] for i in sorted(done)]
    valid = sum(o.valid for o in outcomes)
    first_try = sum(o.valid and not o.repaired for o in outcomes)
    tokens = sum(o.tokens for o in outcomes)
    rate = valid / len(outcomes) if outcomes else 0.0
    complete = len(outcomes) == len(prompts)
    verdict = ("PASS" if rate >= TARGET else "FAIL") if complete else "IN PROGRESS"
    lines = [
        "# V1 JSON-validity eval",
        "",
        f"- **Result:** {verdict} ({valid}/{len(outcomes)} valid = {rate:.0%}; target {TARGET:.0%} of "
        f"{len(prompts)} prompts)",
        f"- **Valid on the first try:** {first_try}/{len(outcomes)}; after the one repair: "
        f"{valid - first_try}",
        f"- **Tokens used:** {tokens:,} (about {tokens // max(1, len(outcomes)):,} per prompt)",
        f"- **Last updated:** {_now()}",
        "",
        "| # | Pages | Valid | Repaired | Tokens | Prompt | Problems |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in outcomes:
        problems = "; ".join(o.problems)[:160]
        lines.append(
            f"| {o.index + 1} | {o.num_scenes} | {'yes' if o.valid else 'NO'} "
            f"| {'yes' if o.repaired else 'no'} | {o.tokens:,} | {o.prompt} | {problems} |"
        )
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main(limit: int | None) -> None:
    prompts = load_prompts()
    done = load_done()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    client = AsyncGroq(api_key=groq_key(), max_retries=0, timeout=120.0)
    llm = RoutedLLM(GroqLLM(client, reasoning_effort="low"), ModelRouter(InMemoryQuotaStore()))
    planner = StoryPlanner(
        llm,
        planner_model="openai/gpt-oss-20b",
        repair_model="openai/gpt-oss-120b",
        max_tokens=4000,
        temperature=0.7,
    )
    print(f"{len(done)}/{len(prompts)} already done; running the rest.")
    try:
        for index, prompt in enumerate(prompts):
            if index in done:
                continue
            if limit is not None and limit <= 0:
                print("Reached --limit; re-run to continue.")
                break
            limit = None if limit is None else limit - 1
            try:
                outcome = await run_one(planner, index, prompt)
            except QuotaExhaustedError as exc:
                print(f"Daily quota spent ({exc}). Re-run later to resume.")
                break
            except LLMError as exc:
                print(f"Groq unavailable ({exc}). Re-run later to resume.")
                break
            done[index] = outcome
            with RESULTS.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(outcome), ensure_ascii=False) + "\n")
            mark = "ok" if outcome.valid else "INVALID"
            print(
                f"[{index + 1:2d}/{len(prompts)}] {mark:7s} repaired={outcome.repaired!s:5s} "
                f"{outcome.tokens:5d} tok {outcome.seconds:5.1f}s  {prompt}"
            )
    finally:
        await client.close()
        write_summary(prompts, done)
        print(f"Summary: {SUMMARY.relative_to(ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None, help="run at most N new prompts this time")
    asyncio.run(main(parser.parse_args().limit))
