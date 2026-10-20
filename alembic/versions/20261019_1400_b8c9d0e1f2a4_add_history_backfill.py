"""add history backfill

Revision ID: b8c9d0e1f2a4
Revises: a7b8c9d0e1f3
Create Date: 2026-10-19 14:00:00.000000+00:00

Channels' older uploads, beyond the ~15 their RSS feed carries, fetched from
the Data API so search covers whole channels. Backfilled videos are flagged so
the archive leaves them alone; channels track how far the backfill has got.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a4"
down_revision: str | None = "a7b8c9d0e1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("backfilled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("channels", sa.Column("backfill_page_token", sa.String(length=128), nullable=True))
    op.add_column("channels", sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("channels", "backfilled_at")
    op.drop_column("channels", "backfill_page_token")
    op.drop_column("videos", "backfilled")
