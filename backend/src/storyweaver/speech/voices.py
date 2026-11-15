"""Voice catalogue and per-story voice assignment (plan V2).

The narrator always has the same warm voice for an engine. Characters get distinct voices from that
engine's pool, chosen deterministically from the story key, so a story always sounds the same and a
character keeps one voice for the whole story. (The TTS engine is chosen once per story, so voices never
mix engines.)
"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from storyweaver.domain.story import NARRATOR
from storyweaver.speech.tts import EngineName


@dataclass(frozen=True)
class Voice:
    id: str
    engine: EngineName
    label: str
    accent: str  # "en-us" or "en-gb" (Kokoro needs it for pronunciation)


ORPHEUS_VOICES: tuple[Voice, ...] = (
    Voice("hannah", "orpheus", "Hannah (female)", "en-us"),
    Voice("autumn", "orpheus", "Autumn (female)", "en-us"),
    Voice("diana", "orpheus", "Diana (female)", "en-us"),
    Voice("austin", "orpheus", "Austin (male)", "en-us"),
    Voice("daniel", "orpheus", "Daniel (male)", "en-us"),
    Voice("troy", "orpheus", "Troy (male)", "en-us"),
)

# A subset of Kokoro v1.0 voices, picked for clarity (hexgrad/Kokoro-82M VOICES.md quality grades).
KOKORO_VOICES: tuple[Voice, ...] = (
    Voice("af_heart", "kokoro", "Heart (US female)", "en-us"),
    Voice("af_bella", "kokoro", "Bella (US female)", "en-us"),
    Voice("af_nicole", "kokoro", "Nicole (US female)", "en-us"),
    Voice("bf_emma", "kokoro", "Emma (UK female)", "en-gb"),
    Voice("am_michael", "kokoro", "Michael (US male)", "en-us"),
    Voice("am_fenrir", "kokoro", "Fenrir (US male)", "en-us"),
    Voice("am_puck", "kokoro", "Puck (US male)", "en-us"),
    Voice("bm_george", "kokoro", "George (UK male)", "en-gb"),
    Voice("bm_fable", "kokoro", "Fable (UK male)", "en-gb"),
)

NARRATOR_VOICE: dict[EngineName, str] = {"orpheus": "hannah", "kokoro": "af_heart"}
CATALOGUE: dict[EngineName, tuple[Voice, ...]] = {"orpheus": ORPHEUS_VOICES, "kokoro": KOKORO_VOICES}


def voice(engine: EngineName, voice_id: str) -> Voice:
    for candidate in CATALOGUE[engine]:
        if candidate.id == voice_id:
            return candidate
    raise KeyError(f"Unknown {engine} voice {voice_id!r}")


def _pool(engine: EngineName, story_key: str) -> list[str]:
    """The engine's non-narrator voices in a story-specific, deterministic order."""
    narrator = NARRATOR_VOICE[engine]
    return sorted(
        (v.id for v in CATALOGUE[engine] if v.id != narrator),
        key=lambda vid: hashlib.sha256(f"{story_key}:{vid}".encode()).hexdigest(),
    )


def assign_voices(engine: EngineName, character_ids: Iterable[str], story_key: str) -> dict[str, str]:
    """{speaker: voice id} for the narrator and every character.

    Characters get distinct voices while the pool lasts (5 for Orpheus, 8 for Kokoro; stories have at most
    6 characters), then voices repeat.
    """
    pool = _pool(engine, story_key)
    voices = {NARRATOR: NARRATOR_VOICE[engine]}
    for index, character_id in enumerate(sorted(set(character_ids))):
        voices[character_id] = pool[index % len(pool)]
    return voices


def with_guest_voices(
    engine: EngineName, voices: Mapping[str, str], guests: Iterable[str], story_key: str
) -> dict[str, str]:
    """`voices` plus a voice for each guest (an undeclared speaker), preferring voices nobody uses yet.

    Computed from the whole story's guests, so a guest keeps its voice on every page without being stored.
    With 3 main characters Orpheus has exactly 2 voices left, one per allowed guest; beyond that they repeat.
    """
    result = dict(voices)
    used = set(result.values())
    pool = _pool(engine, story_key)
    free = [vid for vid in pool if vid not in used] or pool
    for index, guest in enumerate(sorted(set(guests) - set(result))):
        result[guest] = free[index % len(free)]
    return result
