"""Narration: turn scene lines (narrator + character dialogue) into one MP3 per scene (plan V2).

Each line is spoken in its speaker's voice (speech/voices.py), long lines are chunked for Orpheus, and the
pieces are joined with short pauses. The plan suggested pydub for joining; numpy concatenation plus a
PyAV MP3 encode does the same without pydub (which relies on the deprecated `audioop` module).
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from storyweaver.domain.story import NARRATOR, Scene
from storyweaver.routing.tts import TTSRouter
from storyweaver.speech.audio_utils import encode_mp3
from storyweaver.speech.tts import (
    TTS_SAMPLE_RATE,
    EngineName,
    TTSRateLimitedError,
    TTSUnavailableError,
    split_for_tts,
)
from storyweaver.speech.voices import CATALOGUE, NARRATOR_VOICE

logger = logging.getLogger(__name__)

PAUSE_BETWEEN_CHUNKS = 0.15
PAUSE_BETWEEN_LINES = 0.35
MAX_NARRATE_CHARS = 600


@dataclass(frozen=True)
class SpokenLine:
    voice: str
    text: str


@dataclass(frozen=True)
class NarratedAudio:
    mp3: bytes
    duration_seconds: float
    engine: EngineName


def _silence(seconds: float) -> NDArray[np.float32]:
    return np.zeros(int(seconds * TTS_SAMPLE_RATE), dtype=np.float32)


def scene_lines(scene: Scene, voices: Mapping[str, str]) -> list[SpokenLine]:
    """The scene's lines with voices; an unknown speaker is read by the narrator (never silently dropped)."""
    return [SpokenLine(voices.get(line.speaker, voices[NARRATOR]), line.text) for line in scene.lines]


class Narrator:
    def __init__(self, tts: TTSRouter) -> None:
        self._tts = tts

    async def narrate(self, lines: Sequence[SpokenLine], engine: EngineName) -> NarratedAudio:
        parts: list[NDArray[np.float32]] = []
        for line_index, line in enumerate(lines):
            if line_index:
                parts.append(_silence(PAUSE_BETWEEN_LINES))
            for chunk_index, chunk in enumerate(split_for_tts(line.text)):
                if chunk_index:
                    parts.append(_silence(PAUSE_BETWEEN_CHUNKS))
                parts.append((await self._tts.synthesize(engine, chunk, line.voice)).samples)
        samples = np.concatenate(parts) if parts else _silence(0.1)
        mp3 = await asyncio.to_thread(encode_mp3, samples, TTS_SAMPLE_RATE)
        return NarratedAudio(mp3=mp3, duration_seconds=len(samples) / TTS_SAMPLE_RATE, engine=engine)

    async def narrate_scene(
        self, scene: Scene, voices: Mapping[str, str], engine: EngineName
    ) -> NarratedAudio:
        return await self.narrate(scene_lines(scene, voices), engine)

    async def narrate_text(self, text: str, voice_id: str | None = None) -> tuple[NarratedAudio, str]:
        """One-off narration (POST /v1/speech/narrate): returns the audio and the voice used.

        A voice id picks its engine (an Orpheus voice requires Orpheus quota). Without one, the engine is
        chosen as for a story, and a failing Orpheus call falls back to Kokoro (there's no story whose
        voices must stay consistent).
        """
        if voice_id is not None:
            # Books Orpheus's budget or raises QuotaExhaustedError; never silently switches engine.
            engine = await self._tts.choose([text], require=_engine_of(voice_id))
            return await self.narrate([SpokenLine(voice_id, text)], engine), voice_id

        engine = await self._tts.choose([text])
        try:
            narrator = NARRATOR_VOICE[engine]
            return await self.narrate([SpokenLine(narrator, text)], engine), narrator
        except (TTSRateLimitedError, TTSUnavailableError) as exc:
            if engine != "orpheus":
                raise
            logger.warning("Orpheus failed (%s); narrating with Kokoro instead.", type(exc).__name__)
            narrator = NARRATOR_VOICE["kokoro"]
            return await self.narrate([SpokenLine(narrator, text)], "kokoro"), narrator


def _engine_of(voice_id: str) -> EngineName:
    for engine, voices in CATALOGUE.items():
        if any(v.id == voice_id for v in voices):
            return engine
    raise KeyError(f"Unknown voice {voice_id!r}")
