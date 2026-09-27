"""add channel platform

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-25 12:00:00.000000+00:00

Which video service a channel lives on. Every existing row is a YouTube
channel, which the server default fills in; Nebula channels are added manually
with ids of the form ``nebula:{slug}``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column("platform", sa.String(length=16), nullable=False, server_default="youtube"),
    )


def downgrade() -> None:
    op.drop_column("channels", "platform")
