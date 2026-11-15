import pytest

from storyweaver.routing.limits import UNIDENTIFIED, GpuBudget, LimitExceededError, VisitorLimiter
from storyweaver.routing.store import InMemoryQuotaStore

pytestmark = pytest.mark.unit

SECRET = "test-secret"  # pragma: allowlist secret
OTHER_SECRET = "other"  # pragma: allowlist secret


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _limiter(store: InMemoryQuotaStore, clock: Clock) -> VisitorLimiter:
    return VisitorLimiter(store, per_hour=5, per_day=15, secret=SECRET, clock=clock)


async def test_a_visitor_gets_five_stories_an_hour_and_others_are_unaffected() -> None:
    store, clock = InMemoryQuotaStore(), Clock()
    limiter = _limiter(store, clock)
    for _ in range(5):
        await limiter.acquire("203.0.113.7")
    with pytest.raises(LimitExceededError) as caught:
        await limiter.acquire("203.0.113.7")
    assert caught.value.window == "hour" and 0 < caught.value.retry_after_seconds <= 3600
    await limiter.acquire("198.51.100.2")  # another visitor still can


async def test_the_daily_limit_applies_across_hours() -> None:
    store, clock = InMemoryQuotaStore(), Clock()
    limiter = _limiter(store, clock)
    for _ in range(3):  # 5 stories in each of three separate hours: 15
        for _ in range(5):
            await limiter.acquire("203.0.113.7")
        clock.now += 2 * 3600
    with pytest.raises(LimitExceededError) as caught:
        await limiter.acquire("203.0.113.7")
    assert caught.value.window == "day"


def test_ips_are_never_stored_in_the_clear() -> None:
    limiter = _limiter(InMemoryQuotaStore(), Clock())
    key = limiter.visitor_key("203.0.113.7")
    assert "203" not in key and len(key) == 32
    assert key == limiter.visitor_key(" 203.0.113.7 ")
    other_secret = VisitorLimiter(InMemoryQuotaStore(), per_hour=1, per_day=1, secret=OTHER_SECRET)
    assert other_secret.visitor_key("203.0.113.7") != key  # keyed: can't be reversed without the secret
    assert limiter.visitor_key(None) == limiter.visitor_key("") == UNIDENTIFIED


async def test_counters_hold_hashed_keys_only() -> None:
    store = InMemoryQuotaStore()
    await _limiter(store, Clock()).acquire("203.0.113.7")
    assert store.counters and not any("203.0.113.7" in key for key in store.counters)


async def test_gpu_budget_books_an_estimate_and_settles_the_real_time() -> None:
    store, clock = InMemoryQuotaStore(), Clock()
    budget = GpuBudget(store, seconds_per_day=30, clock=clock)
    first = await budget.reserve(12)
    await budget.settle(first, 4)  # it only took 4 s: 26 s left
    second = await budget.reserve(12)
    await budget.settle(second, 20)  # 24 s used
    with pytest.raises(LimitExceededError):
        await budget.reserve(12)  # 24 + 12 > 30
    refused = await budget.reserve(6)
    await budget.settle(refused, 0)  # refused before running: refunded
    await budget.reserve(6)
