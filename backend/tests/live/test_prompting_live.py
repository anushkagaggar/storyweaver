"""Opt-in live test: the real planner given an idea plus inspiration notes (plan §9 step 6). Spends one
planner call of free-tier quota (about 3K tokens).

Run:  LIVE_GROQ_API_KEY=... uv run pytest -m live tests/live/test_prompting_live.py -s
"""

import os
import re

import pytest
from groq import AsyncGroq

from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.domain.story import story_problems
from storyweaver.llm.planner import StoryPlanner, StoryRequest
from storyweaver.rag.compression import copies_source
from storyweaver.rag.prompting import assemble_context

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_GROQ_API_KEY"), reason="LIVE_GROQ_API_KEY not set"),
]

NOTES = [
    "A mouse accidentally wakes a sleeping lion and begs for mercy, promising future help. The lion spares "
    "her. Later the lion is trapped in a hunter's net; the mouse hears his roars and chews the ropes free.",
    "A hare laughs at a slow tortoise and challenges him to a race. The hare naps halfway; the tortoise "
    "keeps walking and crosses the line first.",
    "A lonely lighthouse keeper rescues a cold child from a wrecked boat and they become friends.",
]


async def test_the_planner_uses_inspiration_without_echoing_it() -> None:
    client = AsyncGroq(api_key=os.environ["LIVE_GROQ_API_KEY"], max_retries=0, timeout=120.0)
    planner = StoryPlanner(
        GroqLLM(client, reasoning_effort="low"),
        planner_model="openai/gpt-oss-20b",
        repair_model="openai/gpt-oss-120b",
        max_tokens=4000,
        temperature=0.7,
    )
    request = StoryRequest(
        prompt="a tiny hedgehog who helps a big sleepy bear", context=assemble_context(notes=NOTES)
    )
    try:
        result = await planner.plan(request, story_key="live-inspiration")
    finally:
        await client.close()

    story = result.story
    text = " ".join(scene.text for scene in story.scenes)
    print(f"\nTitle: {story.title}\nMoral: {story.moral}\nCharacters: {[c.name for c in story.characters]}")
    tokens = sum(c.prompt_tokens + c.completion_tokens for c in result.calls)
    print(f"Repaired: {result.repaired}  tokens: {tokens}")
    print(f"Scene 1: {story.scenes[0].text}")
    assert story_problems(story, expected_scenes=5) == []
    assert not any(copies_source(text, note, run=6) for note in NOTES), "the story repeats a note's wording"
    assert not re.search(r"\b(tortoise|lighthouse)\b", text, re.IGNORECASE), (
        "a tale was retold, not borrowed from"
    )
