"""Story memory (V4, plan §6.2 and §9): `tale_chunks` for retrieval and `story_bibles`, with pgvector.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-25

Expand only: it installs the `vector` extension and adds two new tables, so the previous release keeps
running unchanged against the migrated database. The extension is not dropped on downgrade (other data may
use it; a rollback never runs down-migrations anyway, plan §11).

Choices worth knowing:
- `tale_chunks.embedding` is `vector(384)`: bge-small-en-v1.5 writes 384 numbers per text.
- The HNSW index uses cosine distance (`vector_cosine_ops`), matching how bge embeddings are compared.
  `m=16, ef_construction=64` are pgvector's defaults, written out so they are a decision and not an accident.
- `tsv` is a *generated* column: Postgres keeps it in step with `text`, so the loader never writes it and it
  can never go stale. The GIN index on it serves the full-text half of hybrid search.
- `(tale_id, position)` is unique, so reloading a tale is idempotent (an upsert, never a duplicate).
- `source` and `licence` travel with every chunk (licence-aware data lineage, plan §13).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5


def upgrade() -> None:
    # Neon, the local pgvector/pgvector:pg16 image and CI's service container all ship the extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tale_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),  # gutenberg | standard_ebooks | tinystories
        sa.Column("tale_id", sa.String(96), nullable=False),  # "<source>:<id in that source>"
        sa.Column("position", sa.Integer(), nullable=False),  # this chunk's order within its tale
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("theme", sa.String(40)),  # tagged by the cleaning flow; may be missing
        sa.Column("moral", sa.String(200)),
        sa.Column("age_band", sa.String(8), nullable=False),
        sa.Column("grade", sa.Float()),  # Flesch-Kincaid grade of the chunk
        sa.Column("licence", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tale_chunks"),
        sa.UniqueConstraint("tale_id", "position", name="uq_tale_chunks_tale_id_position"),
    )
    op.create_index(
        "ix_tale_chunks_embedding",
        "tale_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_tale_chunks_tsv", "tale_chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_tale_chunks_theme", "tale_chunks", ["theme"])
    op.create_index("ix_tale_chunks_age_band", "tale_chunks", ["age_band"])

    op.create_table(
        "story_bibles",
        sa.Column("story_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bible", postgresql.JSONB(), nullable=False),
        sa.Column("embedding", VECTOR(EMBEDDING_DIM)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("story_id", name="pk_story_bibles"),
        # A bible goes when its story goes (delete on demand and the 30-day expiry both rely on this).
        sa.ForeignKeyConstraint(
            ["story_id"],
            ["stories.id"],
            name="fk_story_bibles_story_id_stories",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    # Used by tests only; production rollbacks never run down-migrations (plan §11).
    op.drop_table("story_bibles")
    op.drop_table("tale_chunks")
