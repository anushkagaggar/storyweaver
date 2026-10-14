"""The Redis Lua reservation script, against a real Redis (TEST_REDIS_URL, e.g. redis://localhost:6379/15)."""

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from storyweaver.routing.quotas import ModelLimits
from storyweaver.routing.store import Blocked, RedisQuotaStore, Reservation, Usage

pytestmark = pytest.mark.integration

MODEL = "test/model"
NOW = 1_000_000_040.0  # 20 s into a minute window
LIMITS = ModelLimits(rpm=2, tpm=1_000).limits()


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


async def test_reserve_counts_and_blocks_atomically(redis: Redis) -> None:
    store = RedisQuotaStore(redis)

    first = await store.reserve(MODEL, LIMITS, Usage(requests=1, tokens=600), NOW)
    second = await store.reserve(MODEL, LIMITS, Usage(requests=1, tokens=600), NOW)  # tokens would be 1200

    assert isinstance(first, Reservation)
    assert isinstance(second, Blocked)
    assert second.limit.unit == "tokens" and second.retry_after_seconds == 40
    # The blocked call must not have incremented anything.
    assert int(await redis.get(first.keys["requests/60s"]) or 0) == 1
    assert int(await redis.get(first.keys["tokens/60s"]) or 0) == 600


async def test_counters_expire_after_the_window(redis: Redis) -> None:
    store = RedisQuotaStore(redis)

    reservation = await store.reserve(MODEL, LIMITS, Usage(requests=1, tokens=10), NOW)

    assert isinstance(reservation, Reservation)
    ttl = await redis.ttl(reservation.keys["requests/60s"])
    assert 40 <= ttl <= 45  # 40 s left in the window + 5 s grace


async def test_adjust_corrects_only_token_counters(redis: Redis) -> None:
    store = RedisQuotaStore(redis)
    reservation = await store.reserve(MODEL, LIMITS, Usage(requests=1, tokens=900), NOW)
    assert isinstance(reservation, Reservation)

    await store.adjust(reservation, "tokens", -700)

    assert int(await redis.get(reservation.keys["tokens/60s"]) or 0) == 200
    assert int(await redis.get(reservation.keys["requests/60s"]) or 0) == 1
    assert isinstance(await store.reserve(MODEL, LIMITS, Usage(requests=1, tokens=700), NOW), Reservation)


async def test_cooldown(redis: Redis) -> None:
    store = RedisQuotaStore(redis)

    await store.set_cooldown(MODEL, 2.5, NOW)

    assert 2.0 < await store.cooldown_remaining(MODEL, NOW) <= 2.5
    assert await store.cooldown_remaining("other/model", NOW) == 0.0
