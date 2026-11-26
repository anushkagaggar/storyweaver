"""Opt-in live test of context compression against Groq (gpt-oss-120b). Spends a little free-tier quota (one
call of about 800 tokens).

Run:  LIVE_GROQ_API_KEY=... uv run pytest -m live tests/live/test_compression_live.py -s
"""

import os
import uuid

import pytest
from groq import AsyncGroq

from storyweaver.adapters.groq_llm import GroqLLM
from storyweaver.rag.compression import HARD_MAX_WORDS, ContextCompressor, InMemorySummaryCache, copies_source
from storyweaver.rag.retriever import RetrievedChunk

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("LIVE_GROQ_API_KEY"), reason="LIVE_GROQ_API_KEY not set"),
]

PASSAGES = [
    (
        "Now it came to pass that a poor woodcutter lived at the edge of a great forest with his "
        "two children, a boy named Hansel and a girl named Gretel. There was little bread in the "
        "house, and one night, when the children lay awake, they heard their stepmother say that in "
        "the morning they must be led deep into the wood and left there. Hansel rose softly, "
        "slipped out of doors, and filled his pockets with white pebbles that shone like silver in "
        "the moonlight. Then he crept back to bed and comforted his weeping sister."
    ),
    (
        "A little mouse, running about in the grass, ran over the paws of a sleeping lion. The lion "
        "woke in a rage and caught her in his great claw. Pray spare me, cried the mouse, and some "
        "day I will repay your kindness. The lion laughed at the thought of so small a creature "
        "helping him, but he let her go. Not long after, the lion was caught in a hunter's net, and "
        "roared so loud that the mouse heard him. She ran to the place and gnawed the ropes until "
        "the great beast was free."
    ),
    (
        "In a village by the sea there lived a lonely lighthouse keeper who had no one to talk to "
        "but the gulls. Every evening he climbed the winding stair, lit the great lamp, and watched "
        "the ships go by. One stormy night a small boat was tossed upon the rocks, and in it sat a "
        "shivering child, who had been sent out to sea by mistake. The keeper wrapped her in his "
        "coat, gave her hot soup, and from that night they were friends, and the lamp burned "
        "brighter than ever."
    ),
]


async def test_the_model_writes_short_notes_that_are_not_copies() -> None:
    client = AsyncGroq(api_key=os.environ["LIVE_GROQ_API_KEY"], max_retries=0, timeout=90.0)
    chunks = [
        RetrievedChunk(
            id=uuid.uuid4(),
            tale_id=f"gutenberg:{i}",
            position=0,
            title="A Tale",
            theme=None,
            moral=None,
            age_band="4-6",
            text=text,
            score=1.0,
        )
        for i, text in enumerate(PASSAGES)
    ]
    cache = InMemorySummaryCache()
    try:
        compressor = ContextCompressor(GroqLLM(client, reasoning_effort="low"), cache)

        first = await compressor.compress(chunks)
        second = await compressor.compress(chunks)
    finally:
        await client.close()

    for item in first.items:
        words = len(item.summary.split())
        print(f"\n[{item.source}] {words} words (passage {len(item.chunk.text.split())}): {item.summary}")
    print(f"\ntokens={first.tokens} calls={first.model_calls}")
    assert [item.source for item in first.items] == ["model"] * 3
    for item in first.items:
        assert 5 <= len(item.summary.split()) <= HARD_MAX_WORDS
        assert not copies_source(item.summary, item.chunk.text)
    assert first.tokens < 2000
    assert [item.source for item in second.items] == ["cache"] * 3 and second.model_calls == 0
