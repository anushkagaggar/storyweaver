"""Data access for stories. Repositories never commit; the caller controls the transaction."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from storyweaver.domain.story import AgeBand, Character, Line, Scene, Story
from storyweaver.storage.models import CharacterRow, SceneRow, StoryRow, StoryStatus


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
    ) -> StoryRow:
        row = StoryRow(
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
