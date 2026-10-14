"""Story domain model (plan §6.1) and the content rules a planned story must satisfy.

Structure is enforced by Pydantic; content rules (scene count, words per scene, reading level, character
references) are checked by `story_problems()`, which returns human-readable problems so the planner can
send them back to the LLM in a single repair call.
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from storyweaver.domain.readability import flesch_kincaid_grade

AgeBand = Literal["4-6"]
SceneCount = Literal[5, 6]

NARRATOR = "narrator"
MIN_SCENE_WORDS = 40
MAX_SCENE_WORDS = 120
MAX_FK_GRADE = 4.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Character(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=40)
    species: str = Field(min_length=1, max_length=40)
    visual: str = Field(
        min_length=10, max_length=300, description="Fixed visual description used in every image"
    )
    seed: int = Field(ge=0, le=2**31 - 1, description="Fixed image seed, assigned server-side")
    voice: str | None = Field(default=None, description="TTS voice id, assigned in V2")


class Line(_Model):
    speaker: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=600)


class Scene(_Model):
    idx: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)
    image_prompt: str = Field(min_length=10, max_length=600)
    characters: list[str]
    lines: list[Line] = Field(min_length=1)


class Story(_Model):
    title: str = Field(min_length=1, max_length=80)
    age_band: AgeBand
    moral: str = Field(min_length=1, max_length=200)
    characters: list[Character] = Field(min_length=1, max_length=6)
    setting: str = Field(min_length=1, max_length=300)
    scenes: list[Scene] = Field(min_length=1)

    @property
    def full_text(self) -> str:
        return "\n\n".join(scene.text for scene in self.scenes)


def word_count(text: str) -> int:
    return len(text.split())


def reading_grade(text: str) -> float:
    """Flesch-Kincaid grade level of `text`."""
    return flesch_kincaid_grade(text)


def character_seed(story_key: str, character_id: str) -> int:
    """Deterministic per-character image seed (plan §6.5), stable for a given story."""
    digest = hashlib.sha256(f"{story_key}:{character_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def story_problems(story: Story, expected_scenes: int) -> list[str]:
    """Content rules from plan §6.1. Returns an empty list when the story is acceptable."""
    problems: list[str] = []

    if len(story.scenes) != expected_scenes:
        problems.append(f"The story must have exactly {expected_scenes} scenes; it has {len(story.scenes)}.")

    indices = [scene.idx for scene in story.scenes]
    if indices != list(range(1, len(story.scenes) + 1)):
        problems.append(f"Scene idx values must be 1, 2, 3, ... in order; got {indices}.")

    character_ids = [character.id for character in story.characters]
    if len(set(character_ids)) != len(character_ids):
        problems.append(f"Character ids must be unique; got {character_ids}.")
    if NARRATOR in character_ids:
        problems.append(f"'{NARRATOR}' is reserved for the narrator and cannot be a character id.")
    known = set(character_ids)

    for scene in story.scenes:
        words = word_count(scene.text)
        if not MIN_SCENE_WORDS <= words <= MAX_SCENE_WORDS:
            problems.append(
                f"Scene {scene.idx} text has {words} words; it must have "
                f"{MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} words."
            )
        unknown_characters = sorted(set(scene.characters) - known)
        if unknown_characters:
            problems.append(f"Scene {scene.idx} lists unknown character ids {unknown_characters}.")
        unknown_speakers = sorted({line.speaker for line in scene.lines} - known - {NARRATOR})
        if unknown_speakers:
            problems.append(
                f"Scene {scene.idx} has lines from unknown speakers {unknown_speakers}; "
                f"use '{NARRATOR}' or a character id."
            )

    grade = reading_grade(story.full_text)
    if grade > MAX_FK_GRADE:
        problems.append(
            f"The story reads at Flesch-Kincaid grade {grade:.1f}; it must be at most {MAX_FK_GRADE:.0f}. "
            "Use shorter sentences and simpler words."
        )

    return problems
