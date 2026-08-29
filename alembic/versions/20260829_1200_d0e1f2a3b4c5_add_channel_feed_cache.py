"""add channel_feed_cache

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-29 12:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'channel_feed_cache',
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('xml', sa.LargeBinary(), nullable=False),
        sa.Column(
            'fetched_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['channel_id'], ['channels.channel_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('channel_id'),
    )


def downgrade() -> None:
    op.drop_table('channel_feed_cache')
