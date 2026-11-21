"""data_pipeline/flows/synth_generate.py: the synthetic SFT generator (V3), with the real StoryPlanner and a
scripted LLM. It lives outside backend/ (plan §5 layout) but runs on the backend's code and venv."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from storyweaver.llm.client import ChatMessage, LLMResult, LLMUnavailableError
from storyweaver.llm.planner import StoryPlanner
from storyweaver.routing.router import QuotaExhaustedError

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[3] / "data_pipeline" / "flows" / "synth_generate.py"
_spec = importlib.util.spec_from_file_location("synth_generate", _PATH)
assert _spec is not None and _spec.loader is not None
synth = importlib.util.module_from_spec(_spec)
sys.modules["synth_generate"] = synth
_spec.loader.exec_module(synth)

TEACHER = "openai/gpt-oss-120b"
DAY = "2026-11-21"


class ScriptedLLM:
    """Answers with queued story JSON, or raises a queued exception."""

    def __init__(self, *script: str | Exception, tokens: int = 3000) -> None:
        self.script = list(script)
        self.tokens = tokens
        self.calls = 0

    async def complete_json(self, *, model: str, messages: list[ChatMessage], **_: Any) -> LLMResult:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(item, model, self.tokens // 3, self.tokens - self.tokens // 3, 5.0, "stop")


def ideas(n: int) -> list[Any]:
    return [synth.Idea(prompt=f"A little fox number {i} wants to fly.", num_scenes=5) for i in range(n)]


def setup(tmp_path: Path, *script: str | Exception) -> tuple[Any, Any, ScriptedLLM, list[float]]:
    inner = ScriptedLLM(*script)
    llm = synth.CountingLLM(inner)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    # As in main(): the planner calls PatientLLM, which waits out per-minute limits inside the call.
    patient = synth.PatientLLM(llm, sleep=sleep)
    planner = StoryPlanner(
        patient, planner_model=TEACHER, repair_model=TEACHER, max_tokens=4000, temperature=0.7
    )
    return planner, llm, inner, sleeps


async def run(
    tmp_path: Path, planner: Any, llm: Any, sleeps: list[float], items: list[Any], **kw: Any
) -> Any:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    options = {"target": 100, "daily_tokens": 120_000} | kw
    return await synth.generate(
        iter(items),
        planner,
        llm,
        synth.Store(tmp_path),
        teacher=TEACHER,
        sleep=sleep,
        day=lambda: DAY,
        **options,
    )


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()] if path.exists() else []


def test_the_idea_space_is_large_fixed_and_varied() -> None:
    first = list(synth.combined_ideas())
    assert len(first) == len(synth.HEROES) * len(synth.PLACES) * len(synth.WISHES) >= 30_000
    assert len({idea.id for idea in first}) == len(first)
    assert [i.prompt for i in first[:20]] == [i.prompt for i in list(synth.combined_ideas())[:20]]
    assert {idea.num_scenes for idea in first} == {5, 6}
    assert all(idea.prompt.endswith(".") and idea.prompt[0].isupper() for idea in first[:100])


async def test_kept_records_are_chat_pairs_from_the_teacher(
    tmp_path: Path, planned_story: dict[str, Any]
) -> None:
    planner, llm, _, sleeps = setup(tmp_path, json.dumps(planned_story, indent=2))

    summary = await run(tmp_path, planner, llm, sleeps, ideas(1))

    assert (summary.kept, summary.rejected, summary.tokens) == (1, 0, 3000)
    (record,) = lines(tmp_path / "records.jsonl")
    system, user, assistant = record["messages"]
    assert (system["role"], user["role"], assistant["role"]) == ("system", "user", "assistant")
    assert "Exactly 5 scenes" in system["content"]
    assert "<idea>\nA little fox number 0 wants to fly.\n</idea>" in user["content"]
    assert json.loads(assistant["content"]) == planned_story and "\n" not in assistant["content"]
    assert record["teacher"] == TEACHER and record["label"] == "synthetic" and not record["repaired"]
    assert json.loads((tmp_path / "state.json").read_text("utf-8")) == {"days": {DAY: 3000}}


async def test_a_second_run_resumes_after_the_last_finished_idea(
    tmp_path: Path, planned_story: dict[str, Any]
) -> None:
    story = json.dumps(planned_story)
    planner, llm, inner, sleeps = setup(tmp_path, story, story)
    await run(tmp_path, planner, llm, sleeps, ideas(3), target=2)
    # A crash mid-write leaves half a line: ignored, and that idea simply runs again.
    with (tmp_path / "records.jsonl").open("a", encoding="utf-8") as out:
        out.write('{"id": "half')

    planner, llm, inner, sleeps = setup(tmp_path, story)
    summary = await run(tmp_path, planner, llm, sleeps, ideas(3), target=10)

    assert summary.kept == 1 and inner.calls == 1
    kept = [r["id"] for r in lines_lenient(tmp_path / "records.jsonl")]
    assert kept == [idea.id for idea in ideas(3)]
    assert json.loads((tmp_path / "state.json").read_text("utf-8"))["days"][DAY] == 9000


def lines_lenient(path: Path) -> list[dict[str, Any]]:
    found = []
    for line in path.read_text("utf-8").splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


async def test_stops_when_todays_budget_would_be_passed(
    tmp_path: Path, planned_story: dict[str, Any]
) -> None:
    story = json.dumps(planned_story)
    planner, llm, inner, sleeps = setup(tmp_path, story, story, story)

    summary = await run(
        tmp_path, planner, llm, sleeps, ideas(3), daily_tokens=synth.TOKENS_PER_IDEA_RESERVE + 2000
    )

    assert summary.kept == 1 and inner.calls == 1
    assert "today's budget used" in summary.stopped_because


async def test_waits_out_a_short_quota_and_stops_on_a_daily_one(
    tmp_path: Path, planned_story: dict[str, Any]
) -> None:
    story = json.dumps(planned_story)
    planner, llm, _inner, sleeps = setup(
        tmp_path,
        QuotaExhaustedError(retry_after_seconds=40),
        story,
        QuotaExhaustedError(retry_after_seconds=50_000),
    )

    summary = await run(tmp_path, planner, llm, sleeps, ideas(3))

    assert sleeps == [41] and summary.kept == 1
    assert "quota used up" in summary.stopped_because
    assert len(lines(tmp_path / "records.jsonl")) == 1 and not (tmp_path / "rejects.jsonl").exists()


async def test_a_repair_held_up_by_the_minute_limit_still_runs(
    tmp_path: Path, planned_story: dict[str, Any]
) -> None:
    # Live, 21 Nov 2026: the repair call hit the per-minute limit and the whole idea started over.
    planner, llm, inner, sleeps = setup(
        tmp_path, "{not json", QuotaExhaustedError(retry_after_seconds=40), json.dumps(planned_story)
    )

    summary = await run(tmp_path, planner, llm, sleeps, ideas(1))

    assert sleeps == [41] and inner.calls == 3
    (record,) = lines(tmp_path / "records.jsonl")
    assert record["repaired"] and record["tokens"] == summary.tokens == 6000


async def test_a_story_that_fails_after_repair_is_rejected_once(
    tmp_path: Path, planned_story: dict[str, Any]
) -> None:
    planner, llm, inner, sleeps = setup(tmp_path, "{not json", "{still not json")

    summary = await run(tmp_path, planner, llm, sleeps, ideas(1))

    assert (summary.kept, summary.rejected, summary.tokens) == (0, 1, 6000)
    (reject,) = lines(tmp_path / "rejects.jsonl")
    assert reject["id"] == ideas(1)[0].id and reject["problems"]

    planner, llm, inner, sleeps = setup(tmp_path)
    again = await run(tmp_path, planner, llm, sleeps, ideas(1))
    assert inner.calls == 0 and again.stopped_because == "no ideas left"


async def test_gives_up_for_the_day_when_groq_keeps_failing(tmp_path: Path) -> None:
    down = [LLMUnavailableError("503") for _ in range(synth.MAX_CONSECUTIVE_PROVIDER_ERRORS)]
    planner, llm, _inner, sleeps = setup(tmp_path, *down)

    summary = await run(tmp_path, planner, llm, sleeps, ideas(2))

    assert "kept failing" in summary.stopped_because and summary.kept == 0
    assert sleeps == [synth.PROVIDER_ERROR_PAUSE_SECONDS] * (synth.MAX_CONSECUTIVE_PROVIDER_ERRORS - 1)
