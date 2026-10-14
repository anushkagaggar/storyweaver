"""Initial V1 tables: stories, characters, scenes, jobs (plan §6.2).

Revision ID: 0001
Revises:
Create Date: 2026-10-14

Expand/contract only: this migration must be safe while the previous release is still running.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "stories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("age_band", sa.String(8), nullable=False),
        sa.Column("num_scenes", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(80)),
        sa.Column("moral", sa.String(200)),
        sa.Column("setting", sa.String(300)),
        sa.Column("tts_engine", sa.String(16)),
        sa.Column("error_code", sa.String(32)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_hash", sa.String(64)),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_stories"),
        sa.UniqueConstraint("idempotency_key", name="uq_stories_idempotency_key"),
    )
    op.create_index("ix_stories_status", "stories", ["status"])
    op.create_index("ix_stories_created_at", "stories", ["created_at"])

    op.create_table(
        "characters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("story_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("char_key", sa.String(32), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("species", sa.String(40), nullable=False),
        sa.Column("visual_desc", sa.String(300), nullable=False),
        sa.Column("voice_id", sa.String(64)),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("ref_image_key", sa.String(256)),
        sa.PrimaryKeyConstraint("id", name="pk_characters"),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name="fk_characters_story_id_stories", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("story_id", "char_key", name="uq_characters_story_id_char_key"),
    )

    op.create_table(
        "scenes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("story_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idx", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("image_prompt", sa.Text(), nullable=False),
        sa.Column("character_keys", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("lines", postgresql.JSONB(), nullable=False),
        sa.Column("image_key", sa.String(256)),
        sa.Column("audio_key", sa.String(256)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scenes"),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name="fk_scenes_story_id_stories", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("story_id", "idx", name="uq_scenes_story_id_idx"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("story_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name="fk_jobs_story_id_stories", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("payload_hash", name="uq_jobs_payload_hash"),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])
    op.create_index("ix_jobs_story_id", "jobs", ["story_id"])


def downgrade() -> None:
    # Used by tests only; production rollbacks never run down-migrations (plan §11).
    op.drop_table("jobs")
    op.drop_table("scenes")
    op.drop_table("characters")
    op.drop_table("stories")
