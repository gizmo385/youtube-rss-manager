"""add shorts backfill

Revision ID: c9d0e1f2a3b6
Revises: b8c9d0e1f2a4
Create Date: 2026-10-19 15:00:00.000000+00:00

After backfilling a channel's uploads, the history backfill pages through its
Shorts playlist too, so the older videos it found can be filtered like any
other by the Shorts preferences. Tracks how far that's got per channel.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b6"
down_revision: str | None = "b8c9d0e1f2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("shorts_page_token", sa.String(length=128), nullable=True))
    op.add_column("channels", sa.Column("shorts_backfilled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("channels", "shorts_backfilled_at")
    op.drop_column("channels", "shorts_page_token")
