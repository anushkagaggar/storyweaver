"""Opt-in live test against Groq (plan §12: one live test per model). Spends real free-tier quota.

Run:  LIVE_GROQ_API_KEY=... uv run pytest -m live tests/live -s
"""

import os

import pytest
from groq import AsyncGroq

from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.domain.story import story_problems
from storyweaver.llm.planner import StoryPlanner, StoryRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_GROQ_API_KEY"), reason="LIVE_GROQ_API_KEY not set"),
]


IDEAS = [
    "a little dragon who is scared of the dark",
    "a hedgehog who wants to learn to swim",
    "two friends who share one umbrella in the rain",
]


@pytest.mark.parametrize("idea", IDEAS)
async def test_planner_produces_a_valid_story_on_groq(idea: str) -> None:
    client = AsyncGroq(api_key=os.environ["LIVE_GROQ_API_KEY"], max_retries=0, timeout=120.0)
    planner = StoryPlanner(
        GroqLLM(client, reasoning_effort="low"),
        planner_model="openai/gpt-oss-20b",
        repair_model="openai/gpt-oss-120b",
        max_tokens=4000,
        temperature=0.7,
    )

    result = await planner.plan(StoryRequest(prompt=idea), story_key="live")

    story = result.story
    assert story_problems(story, expected_scenes=5) == []
    usage = [(c.model, c.prompt_tokens, c.completion_tokens, round(c.latency_ms)) for c in result.calls]
    print(f"\nTitle: {story.title}\nRepaired: {result.repaired}\nCalls: {usage}")
    print(f"Characters: {[c.id for c in story.characters]}")
    print(f"Scene 1 ({len(story.scenes[0].text.split())} words): {story.scenes[0].text}")
    await client.close()
