"""add podcast sponsorblock

Revision ID: d0e1f2a3b4c7
Revises: c9d0e1f2a3b6
Create Date: 2026-10-20 12:00:00.000000+00:00

SponsorBlock for podcast feeds: a mode ('off' / 'remove') that cascades
subscription > category > user like the player's, the user's own choice of
segment kinds for it, and the cut copies of episode audio the downloader
makes (audio_cuts).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c7"
down_revision: str | None = "c9d0e1f2a3b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("podcast_sponsorblock", sa.String(length=8), server_default="off", nullable=False))
    op.add_column(
        "users",
        sa.Column(
            "podcast_sponsorblock_categories",
            sa.String(length=255),
            server_default="sponsor,selfpromo,interaction",
            nullable=False,
        ),
    )
    op.add_column("categories", sa.Column("podcast_sponsorblock", sa.String(length=8), nullable=True))
    op.add_column("subscriptions", sa.Column("podcast_sponsorblock", sa.String(length=8), nullable=True))
    op.create_table(
        "audio_cuts",
        sa.Column("video_id", sa.String(length=32), nullable=False),
        sa.Column("categories", sa.String(length=255), nullable=False),
        sa.Column("segments", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "categories"),
    )


def downgrade() -> None:
    op.drop_table("audio_cuts")
    op.drop_column("subscriptions", "podcast_sponsorblock")
    op.drop_column("categories", "podcast_sponsorblock")
    op.drop_column("users", "podcast_sponsorblock_categories")
    op.drop_column("users", "podcast_sponsorblock")
