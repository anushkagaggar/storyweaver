import pytest

from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.routing.tts import orpheus_usage
from storyweaver.speech.tts import TTSRateLimitedError, TTSUnavailableError
from tts_fakes import FakeTTS, make_router

pytestmark = pytest.mark.unit

# With the 0.9 margin: 3,240 characters and 90 requests a day; 1,080 characters and 9 requests a minute.
SENTENCE = "The little fox ran over the hill to find a friend. "  # 51 characters


def _story(characters: int) -> list[str]:
    scene = (SENTENCE * 20)[: characters // 5]
    return [scene] * 5


def test_usage_counts_requests_and_characters_as_tokens() -> None:
    usage = orpheus_usage(["Hi.", "Hello there."])
    assert (usage.requests, usage.tokens) == (2, 15)


async def test_a_story_that_fits_today_uses_orpheus_and_books_the_whole_day_budget() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"))
    assert await router.choose(_story(2000)) == "orpheus"
    # 3,240 - ~2,000 left: a second story of the same size no longer fits, so it gets Kokoro from the start.
    assert await router.choose(_story(2000)) == "kokoro"
    assert await router.choose(["A tiny line."]) == "orpheus"  # a small narration still fits


async def test_forcing_the_quota_to_zero_falls_back_to_kokoro() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"))
    assert await router.choose(_story(3200)) == "orpheus"  # 3,200 of 3,240 characters used
    assert await router.choose([SENTENCE * 2]) == "kokoro"  # 102 more no longer fit


async def test_kokoro_mode_never_touches_orpheus() -> None:
    orpheus = FakeTTS("orpheus")
    router, _ = make_router(orpheus, FakeTTS("kokoro"), mode="kokoro")
    assert await router.choose(_story(500)) == "kokoro"
    await router.synthesize("kokoro", "Hi.", "af_heart")
    assert orpheus.calls == []


async def test_orpheus_mode_without_quota_raises() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"), mode="orpheus")
    await router.choose(_story(3200))
    with pytest.raises(QuotaExhaustedError):
        await router.choose([SENTENCE * 2])


async def test_no_orpheus_configured_means_kokoro() -> None:
    router, _ = make_router(None, FakeTTS("kokoro"))
    assert await router.choose(["Hi."]) == "kokoro"


async def test_orpheus_requests_are_paced_per_minute() -> None:
    orpheus = FakeTTS("orpheus")
    router, sleep = make_router(orpheus, FakeTTS("kokoro"))
    for _ in range(12):  # 9 requests a minute allowed
        await router.synthesize("orpheus", "Short line.", "hannah")
    assert len(orpheus.calls) == 12
    assert sleep.waits and all(wait > 0 for wait in sleep.waits)


async def test_orpheus_characters_are_paced_per_minute() -> None:
    router, sleep = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"))
    chunk = "x" * 200
    for _ in range(7):  # 1,080 characters a minute: the sixth chunk must wait
        await router.synthesize("orpheus", chunk, "hannah")
    assert sleep.waits


async def test_pacing_gives_up_after_the_max_wait() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"), max_wait=0)
    for _ in range(9):
        await router.synthesize("orpheus", "Hi.", "hannah")
    with pytest.raises(QuotaExhaustedError):
        await router.synthesize("orpheus", "Hi.", "hannah")


async def test_orpheus_429_cools_down_so_new_narrations_choose_kokoro() -> None:
    orpheus = FakeTTS("orpheus", errors=[TTSRateLimitedError(30)])
    router, _ = make_router(orpheus, FakeTTS("kokoro"))
    assert await router.choose(["Hi."]) == "orpheus"
    with pytest.raises(TTSRateLimitedError):
        await router.synthesize("orpheus", "Hi.", "hannah")
    assert await router.choose(["Hi."]) == "kokoro"


async def test_orpheus_unavailable_cools_down_too() -> None:
    orpheus = FakeTTS("orpheus", errors=[TTSUnavailableError("down")])
    router, _ = make_router(orpheus, FakeTTS("kokoro"))
    with pytest.raises(TTSUnavailableError):
        await router.synthesize("orpheus", "Hi.", "hannah")
    assert await router.choose(["Hi."]) == "kokoro"


def test_needs_an_engine() -> None:
    with pytest.raises(ValueError, match="at least one engine"):
        make_router(None, None)


async def test_requiring_orpheus_books_its_budget_or_raises() -> None:
    router, _ = make_router(FakeTTS("orpheus"), FakeTTS("kokoro"))
    assert await router.choose(["Hi."], require="orpheus") == "orpheus"
    await router.choose(_story(3200))
    with pytest.raises(QuotaExhaustedError):
        await router.choose([SENTENCE * 2], require="orpheus")
    assert await router.choose(["Hi."], require="kokoro") == "kokoro"


async def test_requiring_an_engine_that_is_not_configured_is_unavailable() -> None:
    router, _ = make_router(None, FakeTTS("kokoro"))
    with pytest.raises(TTSUnavailableError):
        await router.choose(["Hi."], require="orpheus")
