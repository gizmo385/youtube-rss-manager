"""add include_live preference and video_live_status cache

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-19 12:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'include_live',
            sa.Boolean(),
            server_default=sa.text('true'),
            nullable=False,
        ),
    )
    op.add_column(
        'categories',
        sa.Column('include_live', sa.Boolean(), nullable=True),
    )
    op.add_column(
        'subscriptions',
        sa.Column('include_live', sa.Boolean(), nullable=True),
    )
    op.create_table(
        'video_live_status',
        sa.Column('video_id', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column(
            'checked_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('video_id'),
    )


def downgrade() -> None:
    op.drop_table('video_live_status')
    op.drop_column('subscriptions', 'include_live')
    op.drop_column('categories', 'include_live')
    op.drop_column('users', 'include_live')
