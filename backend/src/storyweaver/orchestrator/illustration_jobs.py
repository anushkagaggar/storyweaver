"""Scene illustration as worker jobs (plan §6.4, V3).

When a story is published, one `image` job per scene is queued next to its `tts` job, scene 1 first. Image
jobs run in their own worker lane, so narration pacing never delays a picture (jobs/worker.py).

Retries are idempotent. An image is stored under a content key (`image_key(story, cache_key(params))`), and
every attempt first asks the store whether that key exists: a worker that crashed after the image was made
but before the database was updated finishes on its next attempt without generating anything again.

Failures:
- no GPU budget left (ours or ZeroGPU's): the placeholder now, no retries;
- a transient GPU or endpoint failure: retried with backoff, the placeholder on the last attempt;
- object storage failing: retried; on the final failure the page is marked image_failed and reads fine.

The placeholder is stored under its own per-scene key, never under a content key, so a scene that fell back
once is not stuck with it: the same settings asked for again later still generate the real image.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.imaging.generator import (
    PNG,
    ImageGenerator,
    ImageQuotaExhaustedError,
    ImageUnavailableError,
)
from storyweaver.imaging.params import GenerationParams, cache_key
from storyweaver.imaging.placeholder import PlaceholderImage
from storyweaver.jobs.worker import JobContext, PermanentJobError, RetryableJobError
from storyweaver.storage.blob import BlobError, BlobStore, image_key, placeholder_image_key
from storyweaver.storage.models import CharacterRow, ImageStatus, SceneRow, StoryRow, StoryStatus
from storyweaver.storage.repositories import StoryRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageSettings:
    """The generation settings that don't come from the story (config.py IMAGE_*)."""

    model: str
    model_revision: str
    width: int
    height: int
    sampler: str
    steps: int
    guidance: float


class Illustrator(Protocol):
    """Makes the image for `params` and stores it at `key`.

    In-process (`LocalIllustrator`) it generates, then uploads. The ZeroGPU illustrator (V3 step 4) calls
    the Space's Gradio endpoint, which uploads the image itself before returning, so an image the GPU has
    finished is never lost to a worker crash.
    """

    @property
    def name(self) -> str: ...

    async def illustrate(self, story_id: uuid.UUID, params: GenerationParams, key: str) -> None: ...


class LocalIllustrator:
    def __init__(self, generator: ImageGenerator, blobs: BlobStore) -> None:
        self._generator = generator
        self._blobs = blobs

    @property
    def name(self) -> str:
        return self._generator.name

    async def illustrate(self, story_id: uuid.UUID, params: GenerationParams, key: str) -> None:
        png = await self._generator.generate(params)
        await self._blobs.put(key, png, PNG)


def main_character(scene: SceneRow, characters: list[CharacterRow]) -> CharacterRow | None:
    """The scene's first declared character, else the story's first: its seed keeps it recognisable."""
    by_key = {c.char_key: c for c in characters}
    for ref in scene.character_keys:
        if ref in by_key:
            return by_key[ref]
    return characters[0] if characters else None


def scene_params(
    scene: SceneRow, characters: list[CharacterRow], settings: ImageSettings
) -> GenerationParams:
    character = main_character(scene, characters)
    return GenerationParams(
        prompt=scene.image_prompt,
        seed=character.seed if character is not None else scene.idx,
        model=settings.model,
        model_revision=settings.model_revision,
        width=settings.width,
        height=settings.height,
        sampler=settings.sampler,
        steps=settings.steps,
        guidance=settings.guidance,
    )


class IllustrateSceneJob:
    """Worker handler for `image` jobs. Holds no DB transaction while an image is made.

    `illustrator` None means IMAGE_ENGINE=placeholder: every page gets the placeholder.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        settings: ImageSettings,
        illustrator: Illustrator | None = None,
    ) -> None:
        self._sessions = sessions
        self._blobs = blobs
        self._settings = settings
        self._illustrator = illustrator
        self._placeholder = PlaceholderImage()

    async def __call__(self, context: JobContext) -> None:
        story_id, idx = context.story_id, context.payload.get("idx")
        if story_id is None or not isinstance(idx, int):
            raise PermanentJobError("invalid_job", "Image job has no story or scene.")

        async with self._sessions() as session:
            row = await StoryRepository(session).get(story_id)
            scene = next((s for s in row.scenes if s.idx == idx), None) if row is not None else None
            if row is None or scene is None or row.status != StoryStatus.READY:
                raise PermanentJobError("not_found", f"Scene {idx} of story {story_id} is not available.")
            if scene.image_status in (ImageStatus.READY, ImageStatus.PLACEHOLDER):
                return  # duplicate delivery
            params = scene_params(scene, list(row.characters), self._settings)

        key, status = await self._make(context, story_id, idx, params)
        if not await self._save(story_id, idx, key, status):
            await self._discard(key)
            logger.info("Story %s was deleted while illustrating scene %d; image discarded.", story_id, idx)
            return
        logger.info("Story %s scene %d illustrated (%s).", story_id, idx, status)

    async def _make(
        self, context: JobContext, story_id: uuid.UUID, idx: int, params: GenerationParams
    ) -> tuple[str, ImageStatus]:
        if self._illustrator is None:
            return await self._make_placeholder(story_id, idx, params), ImageStatus.PLACEHOLDER

        key = image_key(story_id, cache_key(params))
        try:
            if await self._blobs.exists(key):
                logger.info("Story %s scene %d: image already stored; reusing it.", story_id, idx)
                return key, ImageStatus.READY
            await self._illustrator.illustrate(story_id, params, key)
            return key, ImageStatus.READY
        except BlobError as exc:
            raise RetryableJobError(str(exc)) from exc
        except ImageQuotaExhaustedError as exc:
            logger.warning(
                "Story %s scene %d: no GPU budget (%s); using the placeholder.", story_id, idx, exc
            )
        except ImageUnavailableError as exc:
            if not context.is_last_attempt:
                raise RetryableJobError(f"Image generation unavailable: {exc}") from exc
            logger.warning(
                "Story %s scene %d: %s on the last attempt; using the placeholder.", story_id, idx, exc
            )
        return await self._make_placeholder(story_id, idx, params), ImageStatus.PLACEHOLDER

    async def _make_placeholder(self, story_id: uuid.UUID, idx: int, params: GenerationParams) -> str:
        key = placeholder_image_key(story_id, idx)
        try:
            if not await self._blobs.exists(key):
                await self._blobs.put(key, await self._placeholder.generate(params), PNG)
        except BlobError as exc:
            raise RetryableJobError(str(exc)) from exc
        return key

    async def _save(self, story_id: uuid.UUID, idx: int, key: str, status: ImageStatus) -> bool:
        """Record the image under the story's row lock, only while it is READY (see story_deletion.py)."""
        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            story: StoryRow | None = await repo.lock(story_id)
            scene = await repo.get_scene(story_id, idx) if story is not None else None
            if story is None or scene is None or story.status != StoryStatus.READY:
                return False
            await repo.set_scene_image(scene, image_key=key, status=status)
            return True

    async def _discard(self, key: str) -> None:
        try:
            await self._blobs.delete(key)
        except BlobError as exc:
            logger.error("Could not remove orphaned image %s: %s", key, exc)


async def mark_scene_image_failed(
    sessions: async_sessionmaker[AsyncSession], story_id: uuid.UUID, idx: int
) -> None:
    async with sessions() as session, session.begin():
        repo = StoryRepository(session)
        scene = await repo.get_scene(story_id, idx)
        if scene is not None and scene.image_status not in (ImageStatus.READY, ImageStatus.PLACEHOLDER):
            await repo.set_scene_image(scene, image_key=None, status=ImageStatus.FAILED)
