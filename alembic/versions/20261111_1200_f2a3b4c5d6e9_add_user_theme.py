"""add user theme

Revision ID: f2a3b4c5d6e9
Revises: a3b4c5d6e7f0
Create Date: 2026-11-11 12:00:00.000000+00:00

The colour theme picked in Settings. Existing users keep the original dark
theme, now called Ember.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e9"
down_revision: str | None = "a3b4c5d6e7f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("theme", sa.String(length=16), server_default="ember", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "theme")
