"""add sponsorblock

Revision ID: a7b8c9d0e1f3
Revises: f6a7b8c9d0e2
Create Date: 2026-10-19 13:00:00.000000+00:00

SponsorBlock for the in-page player: a mode ('off' / 'show' / 'skip') that
cascades subscription > category > user like the other preferences, and the
user's choice of segment kinds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f3"
down_revision: str | None = "f6a7b8c9d0e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("sponsorblock", sa.String(length=8), server_default="off", nullable=False))
    op.add_column(
        "users",
        sa.Column(
            "sponsorblock_categories",
            sa.String(length=255),
            server_default="sponsor,selfpromo,interaction",
            nullable=False,
        ),
    )
    op.add_column("categories", sa.Column("sponsorblock", sa.String(length=8), nullable=True))
    op.add_column("subscriptions", sa.Column("sponsorblock", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "sponsorblock")
    op.drop_column("categories", "sponsorblock")
    op.drop_column("users", "sponsorblock_categories")
    op.drop_column("users", "sponsorblock")
