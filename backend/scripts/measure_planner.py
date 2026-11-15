"""Measure how the live planner handles side characters (plan V3 carry-over).

Runs the real planner (Groq, from .env) on ideas that invite one-scene guests (an owl who gives directions,
a fish who says hello), and records for each story:

- the first call's validation problems, and whether it needed the repair call;
- whether it was accepted in the end;
- the declared characters, and any extras (speakers who aren't declared).

Run from backend/ (spends roughly 2-4k planner tokens per idea of the free tier's 200k a day, which
production shares):

    uv run python scripts/measure_planner.py --label baseline --out <a scratch dir>/planner-baseline.jsonl
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

# MEASURE_SRC points at another checkout's backend/src, to measure an older planner with this venv.
sys.path.insert(0, os.environ.get("MEASURE_SRC") or str(Path(__file__).resolve().parents[1] / "src"))

from groq import AsyncGroq

from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.config import Settings
from storyweaver.domain.story import NARRATOR, Story
from storyweaver.llm.planner import PlanningError, StoryPlanner, StoryRequest
from storyweaver.routing.llm import RoutedLLM
from storyweaver.routing.router import ModelRouter, QuotaExhaustedError
from storyweaver.routing.store import InMemoryQuotaStore

IDEAS = [
    "A little fox gets lost in the woods and a wise owl helps her find the way home.",
    "A boy goes fishing with his grandpa and meets a talking fish.",
    "A shy girl's first day at school, where her teacher and a new friend help her.",
    "A bunny looks for the moon and asks the animals of the farm where it went.",
    "A small turtle wants to win a race against a fast hare.",
    "A kitten visits the market and every shopkeeper gives her something.",
    "A dragon who is afraid of the dark, and a firefly who shows him the night is pretty.",
    "A little bear's birthday picnic with all his forest friends.",
    "A girl and her dog take a train ride and meet the kind conductor.",
    "A penguin who wants to fly asks a seagull and an eagle for lessons.",
    "A baby elephant loses her trumpet sound and the jungle animals help her find it.",
    "A robot who wants to be a gardener, and an old snail who teaches him patience.",
    "A mouse throws a tea party and a grumpy cat shows up.",
    "Two sisters build a snowman and it comes to life for one night.",
    "A duckling crosses the busy pond, helped by a frog, a heron and a big swan.",
]


class RecordingPlanner(StoryPlanner):
    """Remembers each call's validation problems, so the first call's can be reported."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.evaluations: list[list[str]] = []

    def _evaluate(self, content: str, request: StoryRequest, story_key: str):  # type: ignore[no-untyped-def]
        story, problems = StoryPlanner._evaluate(content, request, story_key)
        self.evaluations.append(problems)
        return story, problems


def extras_of(story: Story) -> dict[str, list[int]]:
    """Undeclared speakers, and the scenes they speak in."""
    declared = {c.id for c in story.characters} | {NARRATOR}
    found: dict[str, list[int]] = {}
    for scene in story.scenes:
        for speaker in sorted({line.speaker for line in scene.lines} - declared):
            found.setdefault(speaker, []).append(scene.idx)
    return found


def kind_of(problem: str) -> str:
    for marker, kind in (
        ("unknown speakers", "unknown_speaker"),
        ("unknown character ids", "unknown_scene_character"),
        ("words; it must have", "scene_length"),
        ("Flesch-Kincaid", "reading_grade"),
        ("extra", "extras"),
    ):
        if marker in problem:
            return kind
    return "other"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=len(IDEAS))
    parser.add_argument("--pause", type=float, default=25.0, help="seconds between ideas (planner TPM)")
    args = parser.parse_args()

    settings = Settings()
    if settings.groq_api_key is None:
        raise SystemExit("GROQ_API_KEY is not set.")
    groq = AsyncGroq(api_key=settings.groq_api_key.get_secret_value(), max_retries=0, timeout=120.0)
    llm = RoutedLLM(
        GroqLLM(groq, reasoning_effort=settings.planner_reasoning_effort),
        ModelRouter(InMemoryQuotaStore(), margin=settings.quota_safety_margin),
    )
    planner = RecordingPlanner(
        llm,
        planner_model=settings.planner_model_id,
        repair_model=settings.teacher_model_id,
        max_tokens=settings.planner_max_tokens,
        temperature=settings.planner_temperature,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    first_kinds: Counter[str] = Counter()
    repaired = failed = 0
    with args.out.open("w", encoding="utf-8") as out:
        for n, idea in enumerate(IDEAS[: args.limit], start=1):
            if n > 1:
                await asyncio.sleep(args.pause)
            record: dict[str, object] = {"label": args.label, "idea": idea}
            while True:
                planner.evaluations = []
                started = time.perf_counter()
                try:
                    result = await planner.plan(StoryRequest(prompt=idea), story_key=f"measure-{n}")
                    story: Story | None = result.story
                    record["accepted"] = True
                except QuotaExhaustedError as exc:  # the local per-minute estimate; wait and redo the idea
                    await asyncio.sleep(exc.retry_after_seconds + 1)
                    continue
                except PlanningError as exc:
                    story = None
                    record["accepted"] = False
                    record["final_problems"] = exc.problems
                    failed += 1
                break
            record["seconds"] = round(time.perf_counter() - started, 1)
            record["first_problems"] = planner.evaluations[0] if planner.evaluations else []
            record["repaired"] = len(planner.evaluations) > 1
            repaired += int(len(planner.evaluations) > 1)
            first_kinds.update({kind_of(p) for p in record["first_problems"]})  # type: ignore[union-attr]
            if story is not None:
                record["characters"] = [f"{c.id} ({c.species})" for c in story.characters]
                record["extras"] = extras_of(story)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            status = "ok" if record["accepted"] else "FAILED"
            print(
                f"[{n:2d}] {status:6s} repaired={record['repaired']!s:5s} {record['seconds']:5.1f}s "
                f"chars={record.get('characters')} extras={record.get('extras')}"
            )
            for problem in record["first_problems"]:  # type: ignore[union-attr]
                print(f"       first call: {problem}")

    total = min(args.limit, len(IDEAS))
    print(f"\n{args.label}: {total} ideas, {repaired} repaired, {failed} failed after repair.")
    print("Stories whose first call had each problem kind:", dict(first_kinds))
    await groq.close()


if __name__ == "__main__":
    asyncio.run(main())
