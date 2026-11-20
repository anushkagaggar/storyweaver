"""Speech -> picture: what to draw from a spoken or typed request (no models, no LLM)."""

import pytest

from storyweaver.orchestrator.picture_request import (
    MAX_TITLE_CHARS,
    picture_caption,
    picture_subject,
    picture_title,
)
from storyweaver.routing.limits import VisitorLimiter
from storyweaver.routing.store import InMemoryQuotaStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("said", "subject"),
    [
        ("draw a purple dragon", "a purple dragon"),
        ("Can you draw me a purple dragon, please?", "a purple dragon"),
        ("please could you paint a picture of a happy whale", "a happy whale"),
        ("I want a drawing of two bunnies eating cake!", "two bunnies eating cake"),
        ("Hey, let's draw a castle in the clouds", "a castle in the clouds"),
        ("um so can you make a picture of a robot dog", "a robot dog"),
        ("show me a rainbow unicorn thank you", "a rainbow unicorn"),
        ("A red bus", "a red bus"),
        ("draw Pip the penguin", "Pip the penguin"),  # names keep their capital
    ],
)
def test_the_request_words_are_taken_out(said: str, subject: str) -> None:
    assert picture_subject(said) == subject


@pytest.mark.parametrize(
    "said", ["draw", "please?", "draw me please", "can you draw", "!!!", "12 34", "x" * 201]
)
def test_nothing_to_draw_is_none(said: str) -> None:
    assert picture_subject(said) is None


def test_title_and_caption() -> None:
    assert picture_title("a purple dragon") == "A purple dragon"
    assert picture_caption("a purple dragon") == "A purple dragon."
    assert picture_caption("a dragon!") == "A dragon!"
    long = picture_title("a very " + "long " * 40 + "dragon")
    assert len(long) <= MAX_TITLE_CHARS and long.endswith("…")


async def test_picture_counters_are_separate_from_stories() -> None:
    store = InMemoryQuotaStore()
    stories = VisitorLimiter(store, per_hour=1, per_day=5, secret="s")
    pictures = VisitorLimiter(store, per_hour=1, per_day=5, secret="s", namespace="pictures")
    await stories.acquire("alice")
    await pictures.acquire("alice")  # a story doesn't use up the picture allowance, or the other way round
