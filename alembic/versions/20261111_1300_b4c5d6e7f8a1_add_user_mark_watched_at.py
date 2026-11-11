"""add user mark watched at

Revision ID: b4c5d6e7f8a1
Revises: f2a3b4c5d6e9
Create Date: 2026-11-11 13:00:00.000000+00:00

When playing a video in the app marks it watched: 'open' (as it starts, the
behaviour until now) or 'finish' (once most of it has played).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a1"
down_revision: str | None = "f2a3b4c5d6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mark_watched_at", sa.String(length=8), server_default="open", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "mark_watched_at")
