"""add separate podcast audio retention

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-09-08 12:00:00.000000+00:00

Nullable everywhere, including at the user level: NULL means "same window as
the video", so every existing install keeps exactly the retention it has today
until someone sets a number.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('users', 'categories', 'subscriptions')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table, sa.Column('keep_last_n_audio', sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, 'keep_last_n_audio')
