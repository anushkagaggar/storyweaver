"""stories.adaptation: how the parent's idea was changed before planning (V1.1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16

Expand only: one nullable column, so the previous release keeps running against the migrated database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("adaptation", sa.String(32)))


def downgrade() -> None:
    # Used by tests only; production rollbacks never run down-migrations (plan §11).
    op.drop_column("stories", "adaptation")
