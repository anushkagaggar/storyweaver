"""scenes.image_status: each page's illustration state (V3).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15

Expand only: one nullable column, so V2 keeps running against the migrated database (it never reads it).
NULL means the story has no illustrations, which is what every pre-V3 story correctly reports.
`scenes.image_key` already exists (0001).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scenes", sa.Column("image_status", sa.String(16)))


def downgrade() -> None:
    # Used by tests only; production rollbacks never run down-migrations (plan §11).
    op.drop_column("scenes", "image_status")
