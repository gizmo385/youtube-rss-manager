"""add watched videos

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b9
Create Date: 2026-10-15 12:00:00.000000+00:00

Per-user watched state for the Watch tab. Keyed by a free-form video key
(a YouTube video id, or ``nebula:{slug}``) since Nebula videos have no row in
``videos`` to point a foreign key at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c0"
down_revision: str | None = "c3d4e5f6a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watched_videos",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("video_key", sa.String(length=255), primary_key=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("watched_videos")
