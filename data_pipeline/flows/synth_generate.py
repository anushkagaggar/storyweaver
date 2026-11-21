"""Synthetic SFT data for StoryWeaver-mini (plan §7, started in V3, used in V6 by build_sft.py).

The teacher (gpt-oss-120b on Groq's free tier) plans stories for made-up ideas through the backend's own
`StoryPlanner`, so every kept record passed the same validation a live story does (5-6 scenes, 30-120
words each, Flesch-Kincaid grade <= 4, valid characters), with its one repair call if needed. Each record
is a chat-format pair: the planner's exact prompt -> the teacher's story JSON.

- **Quota-paced with the backend's own code.** Calls go through `RoutedLLM` + `ModelRouter` on the same
  Redis counters as the Space (set SYNTH_REDIS_URL, or REDIS_URL, to the Upstash URL), so this and live
  stories share Groq's per-organisation limits instead of racing them. A daily token budget of its own
  (--daily-tokens) leaves the rest of gpt-oss-120b's day for live repairs and fallbacks.
- **Resumable.** Every record (kept or rejected) is appended and flushed as it finishes, and a run skips
  the ideas already in either file; the day's token count lives in state.json. Stopping at any point,
  including a crash mid-line, loses at most the idea in flight.
- **Deterministic ideas, no tokens spent on them.** Hero x place x wish from fixed lists (31,500
  combinations) in a seeded order, so every run continues the same sequence. TinyStories-derived prompts
  join in V6 via --ideas.

Run daily from backend/ (it uses the backend's venv and code; GROQ_API_KEY from the environment or .env):

    uv run python ../data_pipeline/flows/synth_generate.py
    uv run python ../data_pipeline/flows/synth_generate.py --dry-run 5     # print the next ideas only

Scheduling: Windows Task Scheduler (see docs/data-cards/synthetic-planner-sft.md); Prefect takes over in V4
with the other data flows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from storyweaver.llm.client import (  # noqa: E402
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRateLimitedError,
    LLMResult,
    LLMUnavailableError,
)
from storyweaver.llm.planner import PlanningError, StoryPlanner, StoryRequest  # noqa: E402
from storyweaver.routing.router import QuotaExhaustedError  # noqa: E402

logger = logging.getLogger("synth_generate")

GENERATOR_VERSION = 1
DEFAULT_OUT = REPO / "data_pipeline" / "data" / "raw" / "synth"
DEFAULT_TARGET = 5_000  # the low end of the plan's 5-10k; build_sft.py adds TinyStories prompts in V6
# gpt-oss-120b allows 200k tokens a day per organisation (180k after the router's 10% margin). Live stories
# use it for repairs and as the planner's fallback, so the generator keeps to 120k and leaves them 60k.
DEFAULT_DAILY_TOKENS = 120_000
# A story costs ~3-4k tokens (prompt ~1k, story JSON ~2-3k); a repaired one roughly twice that. The next
# idea starts only if a repaired story would still fit today.
TOKENS_PER_IDEA_RESERVE = 8_000
SHORT_WAIT_LIMIT_SECONDS = 300  # longer than this is a daily window: stop and resume tomorrow
MAX_CONSECUTIVE_PROVIDER_ERRORS = 3
PROVIDER_ERROR_PAUSE_SECONDS = 60.0

IDEA_SEED = 20261121

HEROES = [
    "A shy little hedgehog",
    "A brave baby elephant",
    "A curious young fox",
    "A sleepy koala",
    "A small green dragon",
    "A cheerful penguin",
    "A tiny mouse",
    "A clumsy puppy",
    "A gentle giant tortoise",
    "A little robot",
    "A young owl",
    "A playful otter",
    "A busy bumblebee",
    "A kind old bear",
    "A fluffy white bunny",
    "A spotted ladybird",
    "A girl called Mia",
    "A boy called Sam",
    "Two little sisters",
    "A grandpa and his grandson",
    "A lonely cloud",
    "A friendly snail",
    "A baby whale",
    "A little red hen",
    "A wobbly kitten",
    "A proud peacock",
    "A small brown sparrow",
    "A happy hippo",
    "A quiet giraffe",
    "A noisy parrot",
    "A firefly",
    "A tiny teapot",
    "A lost mitten",
    "A little train",
    "A young beaver",
]

PLACES = [
    "in a sunny meadow",
    "at the seaside",
    "deep in the forest",
    "on a snowy mountain",
    "in a busy city park",
    "on a quiet farm",
    "in a garden full of flowers",
    "by a sparkling river",
    "under the sea",
    "in a cosy village",
    "at the edge of a pond",
    "in a big old library",
    "on a little island",
    "in the jungle",
    "at a market",
    "in a castle",
    "up in the clouds",
    "in the desert",
    "at school",
    "in a toy shop",
    "on a rainy day at home",
    "in an orchard",
    "at the zoo",
    "on a boat",
    "in a bakery",
    "at a birthday party",
    "by a lighthouse",
    "in a cave full of crystals",
    "at the top of a tall tree",
    "on the moon",
]

WISHES = [
    "wants to learn to whistle",
    "is afraid of the dark",
    "loses a favourite red ball",
    "wants to make a new friend",
    "finds a mysterious map",
    "wants to help a friend who is sad",
    "has to share a special toy",
    "is too small to reach something important",
    "wants to win a race",
    "makes a big mistake and wants to fix it",
    "finds a lost baby animal",
    "wants to see the stars up close",
    "is nervous about the first day somewhere new",
    "plans a surprise for a friend",
    "can't fall asleep",
    "wants to be the best at something",
    "learns to wait for a turn",
    "plants a seed and waits for it to grow",
    "hears a strange noise",
    "wants to fly",
    "tries a food for the very first time",
    "has a hiccup that won't go away",
    "builds something with friends",
    "wants to find the end of the rainbow",
    "says sorry after an argument",
    "looks for a new home",
    "is very curious about a locked door",
    "wants to help clean up a messy place",
    "finds that everyone has forgotten a birthday",
    "learns that being different is good",
]


@dataclass(frozen=True)
class Idea:
    prompt: str
    num_scenes: Literal[5, 6]
    age_band: Literal["4-6"] = "4-6"

    @property
    def id(self) -> str:
        key = f"{self.prompt}|{self.num_scenes}|{self.age_band}"
        return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

    def request(self) -> StoryRequest:
        return StoryRequest(prompt=self.prompt, age_band=self.age_band, num_scenes=self.num_scenes)


def combined_ideas(seed: int = IDEA_SEED) -> Iterator[Idea]:
    """Every hero x place x wish, in one seeded order (the same on every run, so runs continue it)."""
    total = len(HEROES) * len(PLACES) * len(WISHES)
    order = list(range(total))
    random.Random(seed).shuffle(order)  # noqa: S311 (a fixed order, not a secret)
    for n in order:
        hero, rest = divmod(n, len(PLACES) * len(WISHES))
        place, wish = divmod(rest, len(WISHES))
        prompt = f"{HEROES[hero]} {PLACES[place]} {WISHES[wish]}."
        yield Idea(prompt=prompt, num_scenes=6 if n % 3 == 0 else 5)


def ideas_from_file(path: Path) -> Iterator[Idea]:
    """JSONL lines with `prompt` (and optional `num_scenes` 5 or 6), e.g. TinyStories-derived prompts."""
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            if line.strip():
                item = json.loads(line)
                yield Idea(prompt=str(item["prompt"]), num_scenes=6 if item.get("num_scenes") == 6 else 5)


class CountingLLM:
    """Counts the tokens of every finished call (including a repair, or a call whose story was rejected)."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.tokens = 0

    async def complete_json(self, **kwargs: Any) -> LLMResult:
        result = await self._inner.complete_json(**kwargs)
        self.tokens += result.prompt_tokens + result.completion_tokens
        return result

    async def complete_text(self, **kwargs: Any) -> LLMResult:
        result = await self._inner.complete_text(**kwargs)
        self.tokens += result.prompt_tokens + result.completion_tokens
        return result


class PatientLLM:
    """Waits out a per-minute quota *inside* the call, so a repair blocked for 40 s still runs instead of the
    whole idea starting over (the first live run threw away ~5k tokens that way). A longer wait means a
    daily window: QuotaExhaustedError goes up and the run stops for the day."""

    def __init__(self, inner: LLMClient, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self._inner = inner
        self._sleep = sleep

    async def complete_json(self, **kwargs: Any) -> LLMResult:
        while True:
            try:
                return await self._inner.complete_json(**kwargs)
            except QuotaExhaustedError as exc:
                if exc.retry_after_seconds > SHORT_WAIT_LIMIT_SECONDS:
                    raise
                logger.info("Pacing: waiting %.0f s for the per-minute limit.", exc.retry_after_seconds)
                await self._sleep(exc.retry_after_seconds + 1)

    async def complete_text(self, **kwargs: Any) -> LLMResult:
        return await self._inner.complete_text(**kwargs)


class Planner(Protocol):
    def messages(self, request: StoryRequest) -> list[ChatMessage]: ...

    async def plan(self, request: StoryRequest, *, story_key: str) -> Any: ...


@dataclass
class Store:
    """records.jsonl (kept), rejects.jsonl (failed validation after repair) and state.json (tokens a day)."""

    directory: Path
    days: dict[str, int] = field(default_factory=dict)

    @property
    def records(self) -> Path:
        return self.directory / "records.jsonl"

    @property
    def rejects(self) -> Path:
        return self.directory / "rejects.jsonl"

    @property
    def state(self) -> Path:
        return self.directory / "state.json"

    def load(self) -> tuple[set[str], int]:
        """Ids already handled (kept or rejected), and how many were kept."""
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.state.exists():
            self.days = {str(k): int(v) for k, v in json.loads(self.state.read_text("utf-8"))["days"].items()}
        kept = _ids(self.records)
        return kept | _ids(self.rejects), len(kept)

    def append(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
            line = "\n" + line  # a crashed run's half line stays alone (and is skipped), never glued to this
        with path.open("a", encoding="utf-8", newline="\n") as out:
            out.write(line)
            out.flush()
            os.fsync(out.fileno())

    def add_tokens(self, day: str, tokens: int) -> None:
        self.days[day] = self.days.get(day, 0) + tokens
        temporary = self.state.with_suffix(".tmp")
        temporary.write_text(json.dumps({"days": self.days}, indent=1), encoding="utf-8")
        temporary.replace(self.state)  # atomic: a crash never leaves half a state file


def _ids(path: Path) -> set[str]:
    found: set[str] = set()
    if not path.exists():
        return found
    for line in path.read_text("utf-8").splitlines():
        try:
            found.add(str(json.loads(line)["id"]))
        except (ValueError, KeyError):
            continue  # the half-written last line of a crashed run: that idea runs again
    return found


@dataclass
class RunSummary:
    kept: int = 0
    rejected: int = 0
    tokens: int = 0
    stopped_because: str = ""


def today() -> str:
    return datetime.now(UTC).date().isoformat()


async def generate(
    ideas: Iterator[Idea],
    planner: Planner,
    llm: CountingLLM,
    store: Store,
    *,
    teacher: str,
    target: int,
    daily_tokens: int,
    sleep: Callable[[float], Any] = asyncio.sleep,
    day: Callable[[], str] = today,
) -> RunSummary:
    done, kept_before = store.load()
    summary = RunSummary()
    provider_errors = 0
    for idea in ideas:
        if kept_before + summary.kept >= target:
            summary.stopped_because = f"target of {target} records reached"
            break
        if idea.id in done:
            continue
        while True:  # one idea, retried after short waits
            spent_today = store.days.get(day(), 0)
            if spent_today + TOKENS_PER_IDEA_RESERVE > daily_tokens:
                summary.stopped_because = f"today's budget used ({spent_today:,} of {daily_tokens:,} tokens)"
                return summary
            before = llm.tokens
            try:
                result = await planner.plan(idea.request(), story_key=f"synth-{idea.id}")
            except QuotaExhaustedError as exc:  # PatientLLM already waited out the short ones
                store.add_tokens(day(), llm.tokens - before)
                summary.tokens += llm.tokens - before
                wait = exc.retry_after_seconds
                summary.stopped_because = f"Groq quota used up for now (retry in {wait:.0f} s)"
                return summary
            except (LLMRateLimitedError, LLMUnavailableError) as exc:
                store.add_tokens(day(), llm.tokens - before)
                summary.tokens += llm.tokens - before
                provider_errors += 1
                if provider_errors >= MAX_CONSECUTIVE_PROVIDER_ERRORS:
                    summary.stopped_because = f"Groq kept failing ({exc})"
                    return summary
                await sleep(PROVIDER_ERROR_PAUSE_SECONDS)
                continue
            except PlanningError as exc:
                spent = llm.tokens - before
                store.add_tokens(day(), spent)
                summary.tokens += spent
                store.append(store.rejects, _reject(idea, teacher, exc.problems, spent))
                summary.rejected += 1
                outcome = "rejected"
            except LLMError as exc:  # e.g. a request Groq refuses: record it so it isn't retried every day
                spent = llm.tokens - before
                store.add_tokens(day(), spent)
                summary.tokens += spent
                store.append(store.rejects, _reject(idea, teacher, [f"LLM error: {exc}"], spent))
                summary.rejected += 1
                outcome = "rejected"
            else:
                spent = llm.tokens - before
                store.add_tokens(day(), spent)
                summary.tokens += spent
                store.append(store.records, _record(idea, planner, result, teacher, spent))
                summary.kept += 1
                outcome = "kept"
            provider_errors = 0
            done.add(idea.id)
            logger.info("%s %s (%d tokens; today %d)", outcome, idea.id, spent, store.days.get(day(), 0))
            break
    else:
        summary.stopped_because = "no ideas left"
    return summary


def _record(idea: Idea, planner: Planner, result: Any, teacher: str, tokens: int) -> dict[str, Any]:
    story_json = json.dumps(json.loads(result.calls[-1].content), ensure_ascii=False, separators=(",", ":"))
    messages = [{"role": m.role, "content": m.content} for m in planner.messages(idea.request())]
    return {
        "id": idea.id,
        "idea": idea.prompt,
        "age_band": idea.age_band,
        "num_scenes": idea.num_scenes,
        "messages": [*messages, {"role": "assistant", "content": story_json}],
        "teacher": teacher,
        "repaired": bool(result.repaired),
        "tokens": tokens,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator_version": GENERATOR_VERSION,
        "label": "synthetic",
    }


def _reject(idea: Idea, teacher: str, problems: list[str], tokens: int) -> dict[str, Any]:
    return {
        "id": idea.id,
        "idea": idea.prompt,
        "num_scenes": idea.num_scenes,
        "teacher": teacher,
        "problems": problems,
        "tokens": tokens,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator_version": GENERATOR_VERSION,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="records to keep in all")
    parser.add_argument("--daily-tokens", type=int, default=DEFAULT_DAILY_TOKENS)
    parser.add_argument("--max-records", type=int, default=None, help="stop this run after this many")
    parser.add_argument("--ideas", type=Path, default=None, help="JSONL of {prompt, num_scenes?}")
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--teacher", default=None, help="a Groq model id (default: LLM_TEACHER)")
    parser.add_argument("--dry-run", type=int, metavar="N", default=0, help="print the next N ideas")
    parser.add_argument(
        "--local-quota",
        action="store_true",
        help="count quota in memory (testing only: the Space's counters won't see these calls)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    ideas = ideas_from_file(args.ideas) if args.ideas else combined_ideas()
    store = Store(args.out)
    if args.dry_run:
        done, kept = store.load()
        print(f"{kept} records kept so far; the next ideas:")
        upcoming = (idea for idea in ideas if idea.id not in done)
        for _, idea in zip(range(args.dry_run), upcoming, strict=False):
            print(f"  {idea.id}  {idea.num_scenes} scenes  {idea.prompt}")
        return

    from groq import AsyncGroq
    from redis.asyncio import Redis

    from storyweaver.adapters.groq_llm import GroqLLM
    from storyweaver.config import Settings
    from storyweaver.routing.llm import RoutedLLM
    from storyweaver.routing.router import ModelRouter
    from storyweaver.routing.store import InMemoryQuotaStore, QuotaStore, RedisQuotaStore

    class SynthSettings(Settings):
        # The Space's Upstash URL, so this and live stories book the same Groq counters (.env or env).
        synth_redis_url: str | None = None

    settings = SynthSettings()
    if settings.groq_api_key is None:
        raise SystemExit("GROQ_API_KEY is not set.")
    redis_url = settings.synth_redis_url or settings.redis_url
    redis: Redis | None = None
    quota: QuotaStore
    if args.local_quota:
        quota = InMemoryQuotaStore()
    elif redis_url:
        redis = Redis.from_url(redis_url)
        quota = RedisQuotaStore(redis)
    else:
        raise SystemExit("Set SYNTH_REDIS_URL (the Space's Upstash URL): live stories and this share quota.")

    teacher = args.teacher or settings.teacher_model_id
    groq = AsyncGroq(api_key=settings.groq_api_key.get_secret_value(), max_retries=0, timeout=180.0)
    llm = CountingLLM(GroqLLM(groq, reasoning_effort=args.reasoning_effort))
    # No fallbacks: every record must come from the named teacher (the plan records which teacher made it).
    routed = RoutedLLM(llm, ModelRouter(quota, margin=settings.quota_safety_margin), fallbacks={})
    planner = StoryPlanner(
        PatientLLM(routed),
        planner_model=teacher,
        repair_model=teacher,
        max_tokens=settings.planner_max_tokens,
        temperature=settings.planner_temperature,
    )
    target = args.target
    if args.max_records is not None:
        target = min(target, store.load()[1] + args.max_records)
    try:
        summary = await generate(
            ideas, planner, llm, store, teacher=teacher, target=target, daily_tokens=args.daily_tokens
        )
    finally:
        await groq.close()
        if redis is not None:
            await redis.aclose()
    _, kept = store.load()
    print(
        f"This run: {summary.kept} kept, {summary.rejected} rejected, {summary.tokens:,} tokens. "
        f"In all: {kept} of {args.target}. Stopped: {summary.stopped_because}."
    )


if __name__ == "__main__":
    asyncio.run(main())
