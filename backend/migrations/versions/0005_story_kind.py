"""stories.kind: a whole story, or a single picture asked for in words (V3 speech -> picture).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

Expand only: one NOT NULL column with a server default, so existing rows become "story" and the previous
release keeps inserting rows without knowing the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("kind", sa.String(16), nullable=False, server_default="story"))


def downgrade() -> None:
    # Used by tests only; production rollbacks never run down-migrations (plan §11).
    op.drop_column("stories", "kind")
