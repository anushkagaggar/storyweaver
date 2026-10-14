"""SQLAlchemy models for plan §6.2 (V1 tables). Schema changes go through Alembic, expand/contract only.

Status/kind columns are plain strings validated by Python enums, with no database CHECK constraint, so new
values (audio and image states in V2/V3) need no migration.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class StoryStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    READY = "ready"
    FAILED = "failed"


class SceneStatus(StrEnum):
    TEXT_READY = "text_ready"


class JobKind(StrEnum):
    PLAN = "plan"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StoryRow(_Timestamps, Base):
    __tablename__ = "stories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_text: Mapped[str] = mapped_column(Text)
    age_band: Mapped[str] = mapped_column(String(8))
    num_scenes: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(16), default=StoryStatus.QUEUED)
    title: Mapped[str | None] = mapped_column(String(80))
    moral: Mapped[str | None] = mapped_column(String(200))
    setting: Mapped[str | None] = mapped_column(String(300))
    tts_engine: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)
    # Idempotency for POST /v1/stories: the same key with the same request returns the same story.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))

    characters: Mapped[list["CharacterRow"]] = relationship(
        back_populates="story", cascade="all, delete-orphan", order_by="CharacterRow.position"
    )
    scenes: Mapped[list["SceneRow"]] = relationship(
        back_populates="story", cascade="all, delete-orphan", order_by="SceneRow.idx"
    )

    __table_args__ = (
        Index("ix_stories_status", "status"),
        Index("ix_stories_created_at", "created_at"),
    )


class CharacterRow(Base):
    __tablename__ = "characters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"))
    char_key: Mapped[str] = mapped_column(String(32))
    # Order as planned; the first character is usually the main one.
    position: Mapped[int] = mapped_column(SmallInteger)
    name: Mapped[str] = mapped_column(String(40))
    species: Mapped[str] = mapped_column(String(40))
    visual_desc: Mapped[str] = mapped_column(String(300))
    voice_id: Mapped[str | None] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(Integer)
    ref_image_key: Mapped[str | None] = mapped_column(String(256))

    story: Mapped[StoryRow] = relationship(back_populates="characters")

    # The unique (story_id, char_key) index also serves lookups by story_id.
    __table_args__ = (UniqueConstraint("story_id", "char_key"),)


class SceneRow(Base):
    __tablename__ = "scenes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str] = mapped_column(Text)
    image_prompt: Mapped[str] = mapped_column(Text)
    character_keys: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    image_key: Mapped[str | None] = mapped_column(String(256))
    audio_key: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default=SceneStatus.TEXT_READY)

    story: Mapped[StoryRow] = relationship(back_populates="scenes")

    __table_args__ = (UniqueConstraint("story_id", "idx"),)


class JobRow(_Timestamps, Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    story_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Same work enqueued twice (e.g. a retried request) collapses into one job.
    payload_hash: Mapped[str] = mapped_column(String(64), unique=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Worker poll: WHERE status = 'queued' AND run_after <= now() ORDER BY run_after.
        Index("ix_jobs_status_run_after", "status", "run_after"),
        Index("ix_jobs_story_id", "story_id"),
    )
