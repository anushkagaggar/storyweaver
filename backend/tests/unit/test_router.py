import pytest

from storyweaver.routing.quotas import GROQ_FREE_TIER, ModelLimits, limits_for
from storyweaver.routing.router import ModelRouter, QuotaExhaustedError
from storyweaver.routing.store import InMemoryQuotaStore, Reservation, Usage

pytestmark = pytest.mark.unit

GPT20 = "openai/gpt-oss-20b"
GPT120 = "openai/gpt-oss-120b"
SAFEGUARD = "openai/gpt-oss-safeguard-20b"


class Clock:
    def __init__(self, now: float = 1_000_000_040.0) -> None:  # 20 s into a minute window
        self.now = now

    def __call__(self) -> float:
        return self.now


def _router(clock: Clock, margin: float = 0.9) -> tuple[ModelRouter, InMemoryQuotaStore]:
    store = InMemoryQuotaStore()
    return ModelRouter(store, margin=margin, clock=clock), store


def test_limits_table_matches_documented_free_tier() -> None:
    assert limits_for(GPT20) == ModelLimits(rpm=30, rpd=1_000, tpm=8_000, tpd=200_000)
    assert limits_for(SAFEGUARD).rpm == 3 and limits_for(SAFEGUARD).tpm == 2_000
    assert len(GROQ_FREE_TIER) == 7
    with pytest.raises(KeyError, match=r"quotas\.py"):
        limits_for("some/unknown-model")


def test_margin_scales_limits_and_never_reaches_zero() -> None:
    limits = {limit.name: limit.amount for limit in ModelLimits(rpm=3, tpm=2_000).limits(0.1)}

    assert limits == {"requests/60s": 1, "tokens/60s": 200}


async def test_requests_per_minute_limit_with_margin() -> None:
    clock = Clock()
    router, _ = _router(clock)  # safeguard RPM 3 * 0.9 -> 2

    await router.acquire([SAFEGUARD], Usage(requests=1, tokens=100))
    await router.acquire([SAFEGUARD], Usage(requests=1, tokens=100))
    with pytest.raises(QuotaExhaustedError) as info:
        await router.acquire([SAFEGUARD], Usage(requests=1, tokens=100))

    assert info.value.retry_after_seconds == 40  # until the minute window ends
    assert "requests/60s" in str(info.value)

    clock.now += 40  # next window
    assert isinstance(await router.acquire([SAFEGUARD], Usage(requests=1, tokens=100)), Reservation)


async def test_settle_returns_unused_tokens() -> None:
    router, _ = _router(Clock())  # gpt-oss-20b TPM 8000 * 0.9 -> 7200

    first = await router.acquire([GPT20], Usage(requests=1, tokens=4_800))
    with pytest.raises(QuotaExhaustedError):
        await router.acquire([GPT20], Usage(requests=1, tokens=4_800))

    await router.settle(first, actual_tokens=2_000)

    assert isinstance(await router.acquire([GPT20], Usage(requests=1, tokens=4_800)), Reservation)


async def test_release_tokens_keeps_request_count() -> None:
    router, store = _router(Clock())
    reservation = await router.acquire([GPT20], Usage(requests=1, tokens=1_000))

    await router.release_tokens(reservation)

    assert store.counters[reservation.keys["tokens/60s"]] == 0
    assert store.counters[reservation.keys["requests/60s"]] == 1


async def test_falls_back_to_next_candidate_when_blocked() -> None:
    router, _ = _router(Clock())
    await router.acquire([GPT20], Usage(requests=1, tokens=7_000))

    reservation = await router.acquire([GPT20, GPT120], Usage(requests=1, tokens=4_000))

    assert reservation.model == GPT120


async def test_cooldown_skips_model_until_it_expires() -> None:
    clock = Clock()
    router, _ = _router(clock)
    await router.cooldown(GPT20, 12)

    assert (await router.acquire([GPT20, GPT120], Usage(requests=1, tokens=10))).model == GPT120
    with pytest.raises(QuotaExhaustedError) as info:
        await router.acquire([GPT20], Usage(requests=1, tokens=10))
    assert info.value.retry_after_seconds == 12

    clock.now += 12
    assert (await router.acquire([GPT20], Usage(requests=1, tokens=10))).model == GPT20


async def test_daily_limit_blocks_until_the_day_ends() -> None:
    clock = Clock()
    router, store = _router(clock, margin=1.0)
    day_start = int(clock.now // 86_400) * 86_400
    store.counters[f"quota:{GPT120}:requests:86400:{day_start}"] = 1_000  # gpt-oss-120b RPD used up

    with pytest.raises(QuotaExhaustedError) as info:
        await router.acquire([GPT120], Usage(requests=1, tokens=1))

    assert "requests/86400s" in str(info.value)
    assert info.value.retry_after_seconds == day_start + 86_400 - clock.now


async def test_shortest_wait_across_candidates_is_reported() -> None:
    clock = Clock()
    router, _ = _router(clock)
    await router.cooldown(GPT20, 300)
    await router.cooldown(GPT120, 15)

    with pytest.raises(QuotaExhaustedError) as info:
        await router.acquire([GPT20, GPT120], Usage(requests=1, tokens=1))

    assert info.value.retry_after_seconds == 15
    assert len(info.value.reasons) == 2
