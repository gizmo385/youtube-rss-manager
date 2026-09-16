"""add minimum duration

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-08 13:00:00.000000+00:00

The floor to max_duration_seconds' ceiling. 0 at the user level means no floor,
which is the default, so every existing install keeps archiving everything it
archives today.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with a 0 default at the user level (the cascade must terminate);
    # nullable below it, where NULL means inherit.
    op.add_column(
        'users',
        sa.Column('min_duration_seconds', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
    )
    for table in ('categories', 'subscriptions'):
        op.add_column(
            table, sa.Column('min_duration_seconds', sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    for table in ('users', 'categories', 'subscriptions'):
        op.drop_column(table, 'min_duration_seconds')
