import uuid
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import alembic_config
from story_samples import make_planned_story
from storyweaver.llm.schemas import PlannedStory
from storyweaver.storage.models import Base, StoryStatus
from storyweaver.storage.repositories import StoryRepository, row_to_story

pytestmark = pytest.mark.integration


def _plan(num_scenes: int = 5) -> Any:
    return PlannedStory.model_validate(make_planned_story(num_scenes)).to_story(age_band="4-6", story_key="k")


async def test_models_match_migration(session: AsyncSession) -> None:
    """Contract: SQLAlchemy models and migration 0001 describe the same schema."""
    connection = await session.connection()

    diff = await connection.run_sync(
        lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)
    )

    assert diff == []


async def test_expected_indexes_exist(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
    indexes = {row[0] for row in result}

    assert {
        "ix_stories_status",
        "ix_stories_created_at",
        "uq_stories_idempotency_key",
        "uq_characters_story_id_char_key",
        "uq_scenes_story_id_idx",
        "uq_jobs_payload_hash",
        "ix_jobs_status_run_after",
        "ix_jobs_story_id",
    } <= indexes


async def test_create_and_find_by_idempotency_key(session: AsyncSession) -> None:
    repo = StoryRepository(session)
    created = await repo.create(
        prompt_text="a brave dragon", age_band="4-6", num_scenes=5, idempotency_key="k-1", request_hash="h"
    )
    await session.commit()

    found = await repo.get_by_idempotency_key("k-1")

    assert found is not None and found.id == created.id
    assert found.status == StoryStatus.QUEUED
    assert await repo.get_by_idempotency_key("missing") is None


async def test_idempotency_key_is_unique(session: AsyncSession) -> None:
    repo = StoryRepository(session)
    await repo.create(prompt_text="a", age_band="4-6", num_scenes=5, idempotency_key="dup")
    await session.commit()

    with pytest.raises(IntegrityError):
        await repo.create(prompt_text="b", age_band="4-6", num_scenes=5, idempotency_key="dup")
    await session.rollback()


async def test_save_plan_round_trips_to_domain(session: AsyncSession) -> None:
    repo = StoryRepository(session)
    row = await repo.create(prompt_text="a brave dragon", age_band="4-6", num_scenes=5)
    plan = _plan()

    await repo.save_plan(row, plan)
    await session.commit()
    session.expunge_all()
    loaded = await repo.get(row.id)

    assert loaded is not None
    assert loaded.status == StoryStatus.READY
    assert row_to_story(loaded) == plan


async def test_save_plan_twice_replaces_previous_plan(session: AsyncSession) -> None:
    repo = StoryRepository(session)
    row = await repo.create(prompt_text="x", age_band="4-6", num_scenes=6)
    await repo.save_plan(row, _plan(5))
    await repo.save_plan(row, _plan(6))
    await session.commit()
    session.expunge_all()

    loaded = await repo.get(row.id)

    assert loaded is not None and [s.idx for s in loaded.scenes] == [1, 2, 3, 4, 5, 6]
    assert len(loaded.characters) == 2


async def test_mark_failed_and_cascade_delete(session: AsyncSession) -> None:
    repo = StoryRepository(session)
    row = await repo.create(prompt_text="x", age_band="4-6", num_scenes=5)
    await repo.save_plan(row, _plan())
    await repo.mark_failed(row, error_code="planning_failed", error_detail="too short")
    await session.commit()
    assert row.status == StoryStatus.FAILED and row.error_code == "planning_failed"

    await session.delete(row)
    await session.commit()

    counts = await session.execute(
        text("SELECT (SELECT count(*) FROM scenes), (SELECT count(*) FROM characters)")
    )
    assert counts.one() == (0, 0)
    assert await repo.get(uuid.uuid4()) is None


def test_downgrade_then_upgrade(database_url: str) -> None:
    config = alembic_config(database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
