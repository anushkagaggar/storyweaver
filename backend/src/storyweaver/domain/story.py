"""Story domain model (plan §6.1) and the content rules a planned story must satisfy.

Structure is enforced by Pydantic; content rules (scene count, words per scene, reading level, character
references and guests) are checked by `story_problems()`, which returns human-readable problems so the
planner can send them back to the LLM in a single repair call.
"""

import hashlib
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from storyweaver.domain.readability import flesch_kincaid_grade

AgeBand = Literal["4-6"]
SceneCount = Literal[5, 6]

NARRATOR = "narrator"
MIN_SCENE_WORDS = 30  # 40 until V1.1: gpt-oss-20b naturally writes 40-50 words, so 40 failed stories
MAX_SCENE_WORDS = 120
TARGET_SCENE_WORDS = 65
WORDS_PER_SHORT_SENTENCE = 7
MAX_FK_GRADE = 4.0
# Guests: characters who speak or appear without being declared (a wise owl giving directions). They get a
# voice but no fixed look, so they are limited to a few scenes; anyone who recurs more must be declared.
MAX_EXTRAS = 2
MAX_EXTRA_SCENES = 2
CHARACTER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Character(_Model):
    id: str = Field(pattern=CHARACTER_ID.pattern)
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


def _length_fix(words: int) -> str:
    """Concrete repair advice in sentences, which models follow far better than word counts."""
    if words < MIN_SCENE_WORDS:
        sentences = max(2, math.ceil((TARGET_SCENE_WORDS - words) / WORDS_PER_SHORT_SENTENCE))
        return f"Add {sentences} more short sentences to it (aim for about {TARGET_SCENE_WORDS} words)."
    sentences = max(1, math.ceil((words - TARGET_SCENE_WORDS) / WORDS_PER_SHORT_SENTENCE))
    return f"Remove about {sentences} sentences from it (aim for about {TARGET_SCENE_WORDS} words)."


# Words too common to be a borrowed name; other words in an adapted idea are off-limits for character names.
_COMMON_WORDS = frozenset(
    "a an and are about at be by for from goes go has have her his in into is it its of on or our story the "
    "their them they to tale who with".split()
)


def idea_name_words(idea: str) -> frozenset[str]:
    """Words a character must not be named after when the idea was adapted (e.g. "Tom", "Jerry", "Peppa")."""
    return frozenset(w for w in re.findall(r"[a-z]+", idea.lower()) if len(w) >= 3 and w not in _COMMON_WORDS)


def _ref_key(ref: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", ref.lower()).strip("_")


def resolve_character_refs(story: Story) -> Story:
    """Map loose references to a declared character onto its id ("Pip", "PIP" -> "pip"; "Narrator").

    Without this a slip in the planner's casing would turn a main character into a guest with a different
    voice. Anything that still matches no declared character is left alone (it may be a guest).
    """
    aliases = {_ref_key(c.name): c.id for c in story.characters}
    aliases.update({_ref_key(c.id): c.id for c in story.characters})  # ids win over names
    aliases[NARRATOR] = NARRATOR

    def resolve(ref: str) -> str:
        return aliases.get(_ref_key(ref), ref)

    scenes = [
        scene.model_copy(
            update={
                "characters": list(dict.fromkeys(resolve(ref) for ref in scene.characters)),
                "lines": [line.model_copy(update={"speaker": resolve(line.speaker)}) for line in scene.lines],
            }
        )
        for scene in story.scenes
    ]
    return story.model_copy(update={"scenes": scenes})


def story_extras(story: Story) -> dict[str, list[int]]:
    """Guests (undeclared speakers or scene characters) and the scene numbers they appear in."""
    declared = {c.id for c in story.characters} | {NARRATOR}
    found: dict[str, list[int]] = {}
    for scene in story.scenes:
        refs = set(scene.characters) | {line.speaker for line in scene.lines}
        for ref in sorted(refs - declared):
            found.setdefault(ref, []).append(scene.idx)
    return found


def _extra_problems(story: Story, banned_name_words: frozenset[str]) -> list[str]:
    problems: list[str] = []
    extras = story_extras(story)
    for ref, scenes in extras.items():
        if not CHARACTER_ID.match(ref):
            problems.append(
                f"'{ref}' (scenes {scenes}) is not a character id. Use '{NARRATOR}', a declared character "
                "id, or a short lowercase id (letters, digits, underscores) for a one-scene guest."
            )
        elif len(scenes) > MAX_EXTRA_SCENES:
            problems.append(
                f"'{ref}' appears in scenes {scenes} but is not in \"characters\". A character in more than "
                f'{MAX_EXTRA_SCENES} scenes must be declared in "characters" with a visual description.'
            )
        borrowed = sorted(set(re.findall(r"[a-z]+", ref)) & banned_name_words)
        if borrowed:
            problems.append(
                f"Guest '{ref}' uses {borrowed} from the idea, which names a brand or trademarked character. "
                "Give it a new, original name."
            )
    if len(extras) > MAX_EXTRAS:
        problems.append(
            f'The story has {len(extras)} guests who are not in "characters" ({sorted(extras)}); at most '
            f'{MAX_EXTRAS} are allowed. Declare the important ones in "characters", and let the narrator '
            "tell what the others say."
        )
    return problems


def story_problems(
    story: Story, expected_scenes: int, *, banned_name_words: frozenset[str] = frozenset()
) -> list[str]:
    """Content rules from plan §6.1. Returns an empty list when the story is acceptable.

    `banned_name_words`: for ideas that named brands or trademarked characters, words from the idea that no
    character name may use, so the story gets original characters.
    """
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
    for character in story.characters:
        borrowed = sorted(set(re.findall(r"[a-z]+", character.name.lower())) & banned_name_words)
        if borrowed:
            problems.append(
                f"Character name '{character.name}' uses {borrowed} from the idea, which names a brand or "
                "trademarked character. Invent a new, original name and use it everywhere in the story."
            )
    for scene in story.scenes:
        words = word_count(scene.text)
        if not MIN_SCENE_WORDS <= words <= MAX_SCENE_WORDS:
            problems.append(
                f"Scene {scene.idx} text has {words} words; it must have "
                f"{MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} words. {_length_fix(words)}"
            )
    problems.extend(_extra_problems(story, banned_name_words))

    grade = reading_grade(story.full_text)
    if grade > MAX_FK_GRADE:
        problems.append(
            f"The story reads at Flesch-Kincaid grade {grade:.1f}; it must be at most {MAX_FK_GRADE:.0f}. "
            "Use shorter sentences and simpler words."
        )

    return problems
