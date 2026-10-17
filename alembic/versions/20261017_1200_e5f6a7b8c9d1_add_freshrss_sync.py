"""add freshrss sync

Revision ID: e5f6a7b8c9d1
Revises: d4e5f6a7b8c0
Create Date: 2026-10-17 12:00:00.000000+00:00

Per-user FreshRSS credentials, and the item map the watched/read sync keeps:
which FreshRSS item is which video, and the read state both sides last agreed
on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d1"
down_revision: str | None = "d4e5f6a7b8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "freshrss_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("api_password_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_freshrss_account_user"),
    )
    op.create_table(
        "freshrss_items",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("item_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("video_key", sa.String(length=255), nullable=True),
        sa.Column("synced_read", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("freshrss_items")
    op.drop_table("freshrss_accounts")
