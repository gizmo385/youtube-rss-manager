"""add subscription sponsorblock categories

Revision ID: c5d6e7f8a9b2
Revises: b4c5d6e7f8a1
Create Date: 2026-11-13 03:00:00.000000+00:00

Per-channel overrides of which SponsorBlock segment kinds count, in the
player and in podcasts. NULL inherits the user's choice.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b2"
down_revision: str | None = "b4c5d6e7f8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("sponsorblock_categories", sa.String(length=255), nullable=True))
    op.add_column("subscriptions", sa.Column("podcast_sponsorblock_categories", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "podcast_sponsorblock_categories")
    op.drop_column("subscriptions", "sponsorblock_categories")
