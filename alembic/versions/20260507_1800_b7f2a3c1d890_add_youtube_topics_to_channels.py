"""add youtube_topics to channels

Revision ID: b7f2a3c1d890
Revises: a5e00424f473
Create Date: 2026-05-07 18:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY


revision: str = 'b7f2a3c1d890'
down_revision: Union[str, None] = 'a5e00424f473'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('channels', sa.Column('youtube_topics', ARRAY(sa.String()), nullable=True))


def downgrade() -> None:
    op.drop_column('channels', 'youtube_topics')
