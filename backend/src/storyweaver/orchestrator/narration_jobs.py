"""Story narration as worker jobs (plan §6.4 step 3, V2).

Once a story passes its output safety check, `StoryNarration.prepare` picks the TTS engine for the whole
story (booking Orpheus's daily budget, or Kokoro) and assigns character voices. The plan job saves those and
enqueues one `tts` job per scene, scene 1 first, in the same transaction that marks the story READY. So the
text appears at once and each page's audio follows.

`NarrateSceneJob` speaks one scene, uploads the MP3 to object storage and records its key. Failures:
- a short Orpheus rate limit: deferred (no attempt used);
- a long one (Groq's daily limit, e.g. used up by another client of the same organisation): Kokoro now;
- an Orpheus outage: retried with backoff, then Kokoro on the last attempt (plan rule 5);
- anything else on the last attempt: the scene is marked audio_failed and the page stays readable.

A scene that falls back to Kokoro uses the story's Kokoro voice set, so it sounds different from the
Orpheus pages; that's the price of finishing the story.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.domain.story import NARRATOR, Line, Story
from storyweaver.jobs.worker import DeferJobError, JobContext, PermanentJobError, RetryableJobError
from storyweaver.orchestrator.narration import NarratedAudio, Narrator, scene_lines
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.speech.tts import EngineName, TTSRateLimitedError, TTSUnavailableError
from storyweaver.speech.voices import NARRATOR_VOICE, assign_voices
from storyweaver.storage.blob import BlobError, BlobStore, scene_audio_key
from storyweaver.storage.models import SceneStatus, StoryRow, StoryStatus
from storyweaver.storage.repositories import StoryRepository

logger = logging.getLogger(__name__)

MP3 = "audio/mpeg"
# A rate limit that clears within this many seconds is waited out; a longer one switches to Kokoro.
MAX_RATE_LIMIT_WAIT = 120.0


@dataclass(frozen=True)
class NarrationPlan:
    engine: EngineName
    voices: dict[str, str]  # speaker (character id or "narrator") -> voice id


class StoryNarration:
    """Engine and voice choice for whole stories, shared by the plan and tts jobs."""

    def __init__(self, narrator: Narrator) -> None:
        self.narrator = narrator

    async def prepare(self, story_id: uuid.UUID, story: Story) -> NarrationPlan | None:
        """Choose the engine once for the whole story. None when no engine can narrate it."""
        texts = [line.text for scene in story.scenes for line in scene.lines]
        try:
            engine = await self.narrator.tts.choose(texts)
        except (QuotaExhaustedError, TTSUnavailableError) as exc:
            logger.warning("Story %s will have no narration: %s", story_id, exc)
            return None
        character_ids = [c.id for c in story.characters]
        return NarrationPlan(engine, assign_voices(engine, character_ids, str(story_id)))


def stored_voices(row: StoryRow, engine: EngineName) -> dict[str, str]:
    """The story's saved voices; any character without one is read by the narrator."""
    voices = {NARRATOR: NARRATOR_VOICE[engine]}
    voices.update({c.char_key: c.voice_id for c in row.characters if c.voice_id})
    return voices


class NarrateSceneJob:
    """Worker handler for `tts` jobs. Holds no DB transaction while speech is synthesized."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], narrator: Narrator, blobs: BlobStore
    ) -> None:
        self._sessions = sessions
        self._narrator = narrator
        self._blobs = blobs

    async def __call__(self, context: JobContext) -> None:
        story_id, idx = context.story_id, context.payload.get("idx")
        if story_id is None or not isinstance(idx, int):
            raise PermanentJobError("invalid_job", "Narration job has no story or scene.")

        async with self._sessions() as session:
            repo = StoryRepository(session)
            row = await repo.get(story_id)
            scene = next((s for s in row.scenes if s.idx == idx), None) if row is not None else None
            if row is None or scene is None or row.status != StoryStatus.READY:
                raise PermanentJobError("not_found", f"Scene {idx} of story {story_id} is not available.")
            if scene.status == SceneStatus.AUDIO_READY:
                return  # duplicate delivery
            if row.tts_engine not in NARRATOR_VOICE:
                raise PermanentJobError("no_engine", f"Story {story_id} has no narration engine.")
            engine: EngineName = row.tts_engine  # narrowed by the check above
            voices = stored_voices(row, engine)
            lines = [Line.model_validate(line) for line in scene.lines]
            character_ids = [c.char_key for c in row.characters]

        audio = await self._speak(context, story_id, lines, engine, voices, character_ids)
        key = scene_audio_key(story_id, idx)
        try:
            await self._blobs.put(key, audio.mp3, MP3)
        except BlobError as exc:
            raise RetryableJobError(str(exc)) from exc

        if not await self._save(story_id, idx, key):
            # Deleted (or being deleted) while we were narrating: don't leave the file behind.
            await self._discard(key)
            logger.info("Story %s was deleted while narrating scene %d; audio discarded.", story_id, idx)
            return
        logger.info(
            "Story %s scene %d narrated with %s (%.1f s of audio).",
            story_id,
            idx,
            audio.engine,
            audio.duration_seconds,
        )

    async def _save(self, story_id: uuid.UUID, idx: int, key: str) -> bool:
        """Record the audio key, under the story's row lock and only while it is READY (story_deletion.py)."""
        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            story = await repo.lock(story_id)
            scene = await repo.get_scene(story_id, idx) if story is not None else None
            if story is None or scene is None or story.status != StoryStatus.READY:
                return False
            await repo.set_scene_audio(scene, audio_key=key, status=SceneStatus.AUDIO_READY)
            return True

    async def _discard(self, key: str) -> None:
        try:
            await self._blobs.delete(key)
        except BlobError as exc:  # the expiry sweep can't see it now; log so it can be found
            logger.error("Could not remove orphaned audio %s: %s", key, exc)

    async def _speak(
        self,
        context: JobContext,
        story_id: uuid.UUID,
        lines: Sequence[Line],
        engine: EngineName,
        voices: Mapping[str, str],
        character_ids: Sequence[str],
    ) -> NarratedAudio:
        try:
            return await self._narrator.narrate(scene_lines(lines, voices), engine)
        except TTSRateLimitedError as exc:
            wait = exc.retry_after_seconds or 60.0
            if engine == "kokoro" or wait <= MAX_RATE_LIMIT_WAIT:
                raise DeferJobError(wait, f"TTS rate limited: {exc}") from exc
            reason = f"Orpheus rate limited for {wait:.0f} s"
        except QuotaExhaustedError as exc:  # the per-minute pacing waited too long
            raise DeferJobError(exc.retry_after_seconds, f"TTS pacing: {exc}") from exc
        except TTSUnavailableError as exc:
            if engine == "kokoro" or not context.is_last_attempt:
                raise RetryableJobError(f"TTS unavailable: {exc}") from exc
            reason = f"Orpheus unavailable on the last attempt ({exc})"

        logger.warning("Story %s: %s; narrating this scene with Kokoro.", story_id, reason)
        kokoro_voices = assign_voices("kokoro", character_ids, str(story_id))
        try:
            return await self._narrator.narrate(scene_lines(lines, kokoro_voices), "kokoro")
        except TTSUnavailableError as exc:
            raise RetryableJobError(f"Kokoro fallback failed: {exc}") from exc


async def mark_scene_audio_failed(
    sessions: async_sessionmaker[AsyncSession], story_id: uuid.UUID, idx: int
) -> None:
    async with sessions() as session, session.begin():
        repo = StoryRepository(session)
        scene = await repo.get_scene(story_id, idx)
        if scene is not None and scene.status != SceneStatus.AUDIO_READY:
            await repo.set_scene_audio(scene, audio_key=None, status=SceneStatus.AUDIO_FAILED)
