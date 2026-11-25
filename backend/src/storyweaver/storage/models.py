"""SQLAlchemy models for plan §6.2 (V1 tables). Schema changes go through Alembic, expand/contract only.

Status/kind columns are plain strings validated by Python enums, with no database CHECK constraint, so new
values (audio and image states in V2/V3) need no migration.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    ARRAY,
    Computed,
    DateTime,
    Float,
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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# bge-small-en-v1.5 writes 384 numbers per text (plan §2.2). Changing the embedder means a new migration.
EMBEDDING_DIM = 384

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class StoryStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    CHECKING = "checking"  # planned, awaiting the output safety check; content is not shown yet
    READY = "ready"
    FAILED = "failed"
    # Being deleted: its files are going and the rows follow. Reads treat it as gone; a retry finishes it.
    DELETING = "deleting"


# Statuses a story can be deleted (by the parent or the expiry sweep) from: nothing is still writing it.
DELETABLE_STATUSES = frozenset({StoryStatus.READY, StoryStatus.FAILED, StoryStatus.DELETING})


class SceneStatus(StrEnum):
    TEXT_READY = "text_ready"  # text shown; narration queued (or none, when stories.tts_engine is null)
    AUDIO_READY = "audio_ready"
    AUDIO_FAILED = "audio_failed"  # narration gave up; the page still reads fine without it


class ImageStatus(StrEnum):
    """scenes.image_status (V3). NULL: the story has no illustrations (made before V3, or images off)."""

    PENDING = "pending"
    READY = "ready"
    PLACEHOLDER = "placeholder"  # no GPU image could be made; a soft wordless picture stands in
    FAILED = "failed"  # not even the placeholder could be stored; the page reads fine without it


class StoryKind(StrEnum):
    STORY = "story"
    PICTURE = "picture"


class JobKind(StrEnum):
    PLAN = "plan"
    TTS = "tts"  # narrate one scene: payload {"story_id", "idx"}
    IMAGE = "image"  # illustrate one scene: payload {"story_id", "idx"}


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
    # Set when the idea was adapted before planning, e.g. "brands": named characters became original ones.
    adaptation: Mapped[str | None] = mapped_column(String(32))
    # V3 (migration 0004): the signed-in user's pseudonymous id, or "operator" for API-key-only callers
    # (auth/user_token.py). NULL for stories made before V3: nobody owns them, so nobody can open them.
    owner_id: Mapped[str | None] = mapped_column(String(64))
    # V3 (migration 0005): "story", or "picture" for one page drawn from a request like "draw a purple
    # dragon" (no planner, no narration; orchestrator/picture_request.py).
    kind: Mapped[str] = mapped_column(String(16), default=StoryKind.STORY, server_default=StoryKind.STORY)

    characters: Mapped[list["CharacterRow"]] = relationship(
        back_populates="story", cascade="all, delete-orphan", order_by="CharacterRow.position"
    )
    scenes: Mapped[list["SceneRow"]] = relationship(
        back_populates="story", cascade="all, delete-orphan", order_by="SceneRow.idx"
    )

    __table_args__ = (
        Index("ix_stories_status", "status"),
        Index("ix_stories_created_at", "created_at"),
        Index("ix_stories_owner_id_created_at", "owner_id", "created_at"),  # "My stories", newest first
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
    status: Mapped[str] = mapped_column(String(16), default=SceneStatus.TEXT_READY)  # the audio state
    image_status: Mapped[str | None] = mapped_column(String(16))  # ImageStatus; V3 (migration 0003)

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


class TaleChunkRow(Base):
    """One passage of a public-domain tale, ready for hybrid retrieval (V4, migration 0006).

    Loaded offline by the data pipeline, read by the retriever. Never tied to a story or a user.
    """

    __tablename__ = "tale_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(24))  # gutenberg | standard_ebooks | tinystories
    tale_id: Mapped[str] = mapped_column(String(96))  # "<source>:<id in that source>"
    position: Mapped[int] = mapped_column(Integer)  # this chunk's order within its tale
    title: Mapped[str] = mapped_column(String(200))
    theme: Mapped[str | None] = mapped_column(String(40))
    moral: Mapped[str | None] = mapped_column(String(200))
    age_band: Mapped[str] = mapped_column(String(8))
    grade: Mapped[float | None] = mapped_column(Float)  # Flesch-Kincaid
    licence: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(EMBEDDING_DIM))
    # Generated by Postgres from `text`: never written by us, so it cannot go stale.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tale_id", "position", name="uq_tale_chunks_tale_id_position"),
        # Nearest-neighbour search by cosine distance (approximate: HNSW trades a little recall for speed).
        Index(
            "ix_tale_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_tale_chunks_tsv", "tsv", postgresql_using="gin"),  # full-text search
        Index("ix_tale_chunks_theme", "theme"),
        Index("ix_tale_chunks_age_band", "age_band"),
    )


class StoryBibleRow(Base):
    """What must stay true across a story (characters, setting, facts), written after planning (V4).

    `continue_story` (V8) reads it so a continuation stays consistent. It goes when its story goes.
    """

    __tablename__ = "story_bibles"

    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"), primary_key=True
    )
    bible: Mapped[dict[str, Any]] = mapped_column(JSONB)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
