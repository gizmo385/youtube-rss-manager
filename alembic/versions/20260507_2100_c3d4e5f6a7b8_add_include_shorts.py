"""add include_shorts to users, categories, subscriptions

Revision ID: c3d4e5f6a7b8
Revises: b7f2a3c1d890
Create Date: 2026-05-07 21:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b7f2a3c1d890'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('include_shorts', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.add_column('categories', sa.Column('include_shorts', sa.Boolean(), nullable=True))
    op.add_column('subscriptions', sa.Column('include_shorts', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'include_shorts')
    op.drop_column('categories', 'include_shorts')
    op.drop_column('users', 'include_shorts')
