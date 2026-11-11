"""add user app name

Revision ID: a3b4c5d6e7f0
Revises: e1f2a3b4c5d8
Create Date: 2026-11-11 11:00:00.000000+00:00

Each user may rename the app: the name shows in the header, page titles and
the installed web app. Empty means the default name.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f0"
down_revision: str | None = "e1f2a3b4c5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("app_name", sa.String(length=40), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "app_name")
