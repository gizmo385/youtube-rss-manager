"""add video chapters_checked_at

Revision ID: e7f8a9b0c1d4
Revises: d6e7f8a9b0c3
Create Date: 2026-11-24 06:00:00.000000+00:00

When the downloader last asked yt-dlp for a video's chapters, so one probed
too soon after upload to have any can be asked again a few hours later (see
downloader.worker.recheck_chapters). NULL means not known: either never
probed, or probed before this column existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d4"
down_revision: str | None = "d6e7f8a9b0c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("chapters_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "chapters_checked_at")
