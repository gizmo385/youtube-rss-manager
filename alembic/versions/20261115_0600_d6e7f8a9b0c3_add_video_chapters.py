"""add video chapters

Revision ID: d6e7f8a9b0c3
Revises: c5d6e7f8a9b2
Create Date: 2026-11-15 06:00:00.000000+00:00

The chapters yt-dlp reports for a video when the downloader probes it, for
the in-page player. NULL means not probed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c3"
down_revision: str | None = "c5d6e7f8a9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("chapters", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "chapters")
