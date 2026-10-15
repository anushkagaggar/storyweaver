from typing import Any

import pytest
from pydantic import ValidationError

from storyweaver.domain.readability import count_syllables, flesch_kincaid_grade
from storyweaver.domain.story import Story, character_seed, idea_name_words, story_problems
from storyweaver.llm.schemas import PlannedStory

pytestmark = pytest.mark.unit


def _story(data: dict[str, Any]) -> Story:
    return PlannedStory.model_validate(data).to_story(age_band="4-6", story_key="story-1")


def test_valid_story_has_no_problems(planned_story: dict[str, Any]) -> None:
    story = _story(planned_story)

    assert story_problems(story, expected_scenes=5) == []
    assert story.age_band == "4-6"
    assert all(c.seed == character_seed("story-1", c.id) for c in story.characters)


def test_seeds_are_deterministic_and_distinct() -> None:
    assert character_seed("s1", "pip") == character_seed("s1", "pip")
    assert character_seed("s1", "pip") != character_seed("s2", "pip")
    assert character_seed("s1", "pip") != character_seed("s1", "glow")
    assert 0 <= character_seed("s1", "pip") < 2**31


def test_wrong_scene_count(planned_story: dict[str, Any]) -> None:
    problems = story_problems(_story(planned_story), expected_scenes=6)

    assert any("exactly 6 scenes" in p for p in problems)


def test_scene_indices_must_be_consecutive(planned_story: dict[str, Any]) -> None:
    planned_story["scenes"][2]["idx"] = 7

    assert any("idx values" in p for p in story_problems(_story(planned_story), 5))


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("Too short.", "has 2 words"),
        ("Too short.", "Add 9 more short sentences"),
        ("word " * 39, "has 39 words; it must have 40-120 words. Add 4 more short sentences"),
        ("word " * 121, "has 121 words"),
        ("word " * 121, "Remove about 8 sentences"),
    ],
)
def test_scene_word_limits(planned_story: dict[str, Any], text: str, fragment: str) -> None:
    planned_story["scenes"][0]["text"] = text

    problems = story_problems(_story(planned_story), 5)

    assert any(fragment in p and "Scene 1" in p for p in problems)


def test_unknown_character_and_speaker(planned_story: dict[str, Any]) -> None:
    planned_story["scenes"][1]["characters"] = ["pip", "ghost"]
    planned_story["scenes"][1]["lines"].append({"speaker": "ghost", "text": "Boo"})

    problems = story_problems(_story(planned_story), 5)

    assert any("unknown character ids ['ghost']" in p for p in problems)
    assert any("unknown speakers ['ghost']" in p for p in problems)


def test_duplicate_and_reserved_character_ids(planned_story: dict[str, Any]) -> None:
    planned_story["characters"][1]["id"] = "pip"
    duplicate = story_problems(_story(planned_story), 5)
    planned_story["characters"][1]["id"] = "narrator"
    reserved = story_problems(_story(planned_story), 5)

    assert any("unique" in p for p in duplicate)
    assert any("reserved" in p for p in reserved)


def test_reading_level_too_high(planned_story: dict[str, Any]) -> None:
    hard = (
        "Notwithstanding considerable apprehension regarding nocturnal circumstances, the extraordinarily "
        "conscientious dragon contemplated innumerable philosophical considerations concerning illumination, "
        "simultaneously experiencing overwhelming trepidation, consternation and indescribable uncertainty "
        "throughout the interminable, unfathomable evening hours."
    )
    for scene in planned_story["scenes"]:
        scene["text"] = hard

    problems = story_problems(_story(planned_story), 5)

    assert any("Flesch-Kincaid grade" in p for p in problems)


def test_field_limits_raise_validation_error(planned_story: dict[str, Any]) -> None:
    planned_story["characters"][0]["id"] = "Pip With Spaces"

    with pytest.raises(ValidationError):
        _story(planned_story)


@pytest.mark.parametrize(("word", "syllables"), [("cat", 1), ("dragon", 2), ("butterfly", 3), ("little", 2)])
def test_count_syllables(word: str, syllables: int) -> None:
    assert count_syllables(word) == syllables


def test_flesch_kincaid_orders_easy_below_hard() -> None:
    easy = "The cat sat on the mat. The dog ran to the sun."
    hard = "Extraordinarily complicated institutional considerations necessitate comprehensive deliberation."

    assert flesch_kincaid_grade(easy) < 4 < flesch_kincaid_grade(hard)
    assert flesch_kincaid_grade("") == 0.0


def test_idea_name_words_skip_common_words() -> None:
    assert idea_name_words("a story about Tom and Jerry!") == {"tom", "jerry"}
    assert idea_name_words("Peppa Pig goes to the beach") == {"peppa", "pig", "beach"}


def test_banned_name_words_only_check_character_names(planned_story: dict[str, Any]) -> None:
    story = _story(planned_story)

    assert story_problems(story, 5, banned_name_words=frozenset({"dark"})) == []  # only in scene text
    problems = story_problems(story, 5, banned_name_words=frozenset({"pip"}))
    assert any("Character name 'Pip'" in p for p in problems)
