"""stories.owner_id: who made each story (V3 sign-in).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16

Expand only: one nullable column and an index, so the previous release keeps running against the migrated
database (it never reads the column). Stories made before sign-in keep NULL: nobody owns them, so they can't
be opened any more, and the 30-day expiry removes them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("owner_id", sa.String(64)))
    op.create_index("ix_stories_owner_id_created_at", "stories", ["owner_id", "created_at"])


def downgrade() -> None:
    # Used by tests only; production rollbacks never run down-migrations (plan §11).
    op.drop_index("ix_stories_owner_id_created_at", table_name="stories")
    op.drop_column("stories", "owner_id")
