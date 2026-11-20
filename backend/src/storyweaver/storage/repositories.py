"""Data access for stories. Repositories never commit; the caller controls the transaction."""

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from storyweaver.domain.story import AgeBand, Character, Line, Scene, Story
from storyweaver.storage.models import (
    DELETABLE_STATUSES,
    CharacterRow,
    ImageStatus,
    SceneRow,
    SceneStatus,
    StoryKind,
    StoryRow,
    StoryStatus,
)


class StoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        prompt_text: str,
        age_band: AgeBand,
        num_scenes: int,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        adaptation: str | None = None,
        owner_id: str | None = None,
        kind: str = StoryKind.STORY,
    ) -> StoryRow:
        row = StoryRow(
            owner_id=owner_id,
            kind=kind,
            prompt_text=prompt_text,
            age_band=age_band,
            num_scenes=num_scenes,
            status=StoryStatus.QUEUED,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            adaptation=adaptation,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, story_id: uuid.UUID) -> StoryRow | None:
        result = await self._session.execute(
            select(StoryRow)
            .where(StoryRow.id == story_id)
            .options(selectinload(StoryRow.characters), selectinload(StoryRow.scenes))
        )
        return result.scalar_one_or_none()

    async def list_for_owner(self, owner_id: str, *, limit: int) -> list[StoryRow]:
        """The owner's stories, newest first, without pages (a list, not a reader)."""
        result = await self._session.execute(
            select(StoryRow)
            .where(StoryRow.owner_id == owner_id, StoryRow.status != StoryStatus.DELETING)
            .order_by(StoryRow.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def lock(self, story_id: uuid.UUID) -> StoryRow | None:
        """The story row, locked until the transaction ends (serialises deletion with narration saves)."""
        result = await self._session.execute(
            select(StoryRow).where(StoryRow.id == story_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def delete_row(self, story_id: uuid.UUID) -> None:
        """Delete the story; characters, scenes and jobs go with it (ON DELETE CASCADE)."""
        await self._session.execute(delete(StoryRow).where(StoryRow.id == story_id))

    async def expired_ids(
        self, *, created_before: datetime, stuck_before: datetime, limit: int
    ) -> list[uuid.UUID]:
        """Deletable stories created before `created_before`; deletions interrupted before `stuck_before`."""
        result = await self._session.execute(
            select(StoryRow.id)
            .where(
                or_(
                    and_(StoryRow.created_at < created_before, StoryRow.status.in_(DELETABLE_STATUSES)),
                    and_(StoryRow.status == StoryStatus.DELETING, StoryRow.updated_at < stuck_before),
                )
            )
            .order_by(StoryRow.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def get_by_idempotency_key(self, key: str) -> StoryRow | None:
        result = await self._session.execute(select(StoryRow).where(StoryRow.idempotency_key == key))
        return result.scalar_one_or_none()

    async def set_status(self, story: StoryRow, status: StoryStatus) -> None:
        story.status = status
        await self._session.flush()

    async def mark_failed(self, story: StoryRow, *, error_code: str, error_detail: str) -> None:
        story.status = StoryStatus.FAILED
        story.error_code = error_code
        story.error_detail = error_detail
        await self._session.flush()

    async def save_plan(
        self, story_row: StoryRow, plan: Story, *, status: StoryStatus = StoryStatus.READY
    ) -> None:
        """Store a validated plan with `status`. Replaces any earlier plan (safe to retry)."""
        await self._session.execute(delete(SceneRow).where(SceneRow.story_id == story_row.id))
        await self._session.execute(delete(CharacterRow).where(CharacterRow.story_id == story_row.id))
        story_row.title = plan.title
        story_row.moral = plan.moral
        story_row.setting = plan.setting
        story_row.status = status
        story_row.error_code = None
        story_row.error_detail = None
        self._session.add_all(
            [
                CharacterRow(
                    story_id=story_row.id,
                    char_key=c.id,
                    position=position,
                    name=c.name,
                    species=c.species,
                    visual_desc=c.visual,
                    voice_id=c.voice,
                    seed=c.seed,
                )
                for position, c in enumerate(plan.characters)
            ]
            + [
                SceneRow(
                    story_id=story_row.id,
                    idx=s.idx,
                    text=s.text,
                    image_prompt=s.image_prompt,
                    character_keys=list(s.characters),
                    lines=[line.model_dump() for line in s.lines],
                )
                for s in plan.scenes
            ]
        )
        await self._session.flush()

    async def set_narration(self, story_row: StoryRow, engine: str, voices: Mapping[str, str]) -> None:
        """Record the story's TTS engine and each character's voice (chosen once per story)."""
        story_row.tts_engine = engine
        for character in story_row.characters:
            character.voice_id = voices.get(character.char_key)
        await self._session.flush()

    async def get_scene(self, story_id: uuid.UUID, idx: int) -> SceneRow | None:
        result = await self._session.execute(
            select(SceneRow).where(SceneRow.story_id == story_id, SceneRow.idx == idx)
        )
        return result.scalar_one_or_none()

    async def set_scene_audio(self, scene: SceneRow, *, audio_key: str | None, status: SceneStatus) -> None:
        scene.audio_key = audio_key
        scene.status = status
        await self._session.flush()

    async def set_scene_image(self, scene: SceneRow, *, image_key: str | None, status: ImageStatus) -> None:
        scene.image_key = image_key
        scene.image_status = status
        await self._session.flush()

    async def mark_images_pending(self, story_row: StoryRow) -> None:
        for scene in story_row.scenes:
            scene.image_status = ImageStatus.PENDING
        await self._session.flush()


def row_to_story(row: StoryRow) -> Story:
    """Rebuild the domain Story from a planned row (CHECKING or READY) loaded with characters and scenes."""
    planned = (StoryStatus.CHECKING, StoryStatus.READY)
    if row.status not in planned or row.title is None or row.moral is None or row.setting is None:
        raise ValueError(f"Story {row.id} has no plan (status={row.status}).")
    return Story(
        title=row.title,
        age_band=row.age_band,  # type: ignore[arg-type]  # validated on write
        moral=row.moral,
        setting=row.setting,
        characters=[
            Character(
                id=c.char_key,
                name=c.name,
                species=c.species,
                visual=c.visual_desc,
                seed=c.seed,
                voice=c.voice_id,
            )
            for c in row.characters
        ],
        scenes=[
            Scene(
                idx=s.idx,
                text=s.text,
                image_prompt=s.image_prompt,
                characters=list(s.character_keys),
                lines=[Line.model_validate(line) for line in s.lines],
            )
            for s in row.scenes
        ],
    )
