"""add playlist videos

Revision ID: e1f2a3b4c5d8
Revises: d0e1f2a3b4c7
Create Date: 2026-10-28 12:00:00.000000+00:00

Followed YouTube playlists live in ``channels`` (platform 'youtube_playlist'),
but their videos belong to the channels that uploaded them, so which playlists
a video is in needs a table of its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d8"
down_revision: str | None = "d0e1f2a3b4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playlist_videos",
        sa.Column(
            "playlist_id",
            sa.String(length=64),
            sa.ForeignKey("channels.channel_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "video_id",
            sa.String(length=32),
            sa.ForeignKey("videos.video_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_playlist_videos_video_id", "playlist_videos", ["video_id"])


def downgrade() -> None:
    op.drop_table("playlist_videos")
