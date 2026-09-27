"""add channel platform

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-25 12:00:00.000000+00:00

Which video service a channel lives on. Every existing row is a YouTube
channel, which the server default fills in; Nebula channels are added manually
with ids of the form ``nebula:{slug}``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b9"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column("platform", sa.String(length=16), nullable=False, server_default="youtube"),
    )


def downgrade() -> None:
    op.drop_column("channels", "platform")
