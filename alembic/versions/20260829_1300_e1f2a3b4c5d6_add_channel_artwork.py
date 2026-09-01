"""add channel artwork urls

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-29 13:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('channels', sa.Column('thumbnail_url', sa.Text(), nullable=True))
    op.add_column('channels', sa.Column('banner_url', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('channels', 'banner_url')
    op.drop_column('channels', 'thumbnail_url')
