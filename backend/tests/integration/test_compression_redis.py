"""The summary cache against a real Redis (TEST_REDIS_URL, e.g. redis://localhost:6379/15)."""

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.asyncio import Redis

from storyweaver.llm.client import ChatMessage, LLMResult
from storyweaver.rag.compression import ContextCompressor, RedisSummaryCache, cache_key
from storyweaver.rag.retriever import RetrievedChunk

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL not set")
    client = Redis.from_url(url)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


class CountingLLM:
    def __init__(self, *notes: str) -> None:
        self.notes = notes
        self.calls = 0

    async def complete_json(self, *, messages: list[ChatMessage], **_: Any) -> LLMResult:
        self.calls += 1
        body = json.dumps({"summaries": [{"n": n, "summary": s} for n, s in enumerate(self.notes, start=1)]})
        return LLMResult(body, "m", 100, 50, 1.0, "stop")


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        id=uuid.uuid4(),
        tale_id="gutenberg:1",
        position=0,
        title="T",
        theme=None,
        moral=None,
        age_band="4-6",
        text=text,
        score=1.0,
    )


async def test_values_round_trip_and_missing_keys_are_left_out(redis: Redis) -> None:
    cache = RedisSummaryCache(redis)

    await cache.set_many({"a": "A café note.", "b": "Another."})
    found = await cache.get_many(["b", "missing", "a"])

    assert found == {"a": "A café note.", "b": "Another."}  # unicode intact, the missing key absent


async def test_empty_requests_touch_nothing(redis: Redis) -> None:
    cache = RedisSummaryCache(redis)

    assert await cache.get_many([]) == {}
    await cache.set_many({})

    assert await redis.dbsize() == 0


async def test_every_note_expires(redis: Redis) -> None:
    cache = RedisSummaryCache(redis, ttl_seconds=600)

    await cache.set_many({"a": "note"})

    assert 0 < await redis.ttl("a") <= 600


async def test_a_compressor_over_real_redis_summarises_once_then_serves_from_the_cache(redis: Redis) -> None:
    llm = CountingLLM("The dragon fears the dark.", "The owl tells night stories.")
    compressor = ContextCompressor(llm, RedisSummaryCache(redis), model="m")  # type: ignore[arg-type]
    dragon = _chunk("A tiny dragon was afraid of the dark and hid in his cave all night long.")
    owl = _chunk("An old owl lived in a hollow tree and told the forest stories at night.")

    first = await compressor.compress([dragon, owl])
    second = await compressor.compress([owl, dragon])  # another order: still hits

    assert [i.source for i in first.items] == ["model", "model"]
    assert [(i.summary, i.source) for i in second.items] == [
        ("The owl tells night stories.", "cache"),
        ("The dragon fears the dark.", "cache"),
    ]
    assert llm.calls == 1
    assert {key.decode() for key in await redis.keys("rag:summary:*")} == {cache_key(dragon), cache_key(owl)}
