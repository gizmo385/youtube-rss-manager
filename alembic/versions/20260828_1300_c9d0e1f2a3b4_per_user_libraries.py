"""per-user libraries: download_links, drop downloads.jellyfin_item_id

Each subscriber gets their own Jellyfin library rooted at a per-user subtree, so
one download is hardlinked into every subscriber's directory. The same file in
two libraries is two distinct Jellyfin items, so the item id can no longer live
as a single column on ``downloads`` — it moves to a per-(user, video) row.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-28 13:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'download_links',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('video_id', sa.String(length=32), nullable=False),
        # The .mkv hardlink path in the user's subtree. NULL until the worker
        # has created the link.
        sa.Column('link_path', sa.Text(), nullable=True),
        # This user's Jellyfin item id, resolved after their library is scanned
        # (Phase 3). Distinct from any other user's id for the same file.
        sa.Column('jellyfin_item_id', sa.String(length=64), nullable=True),
        sa.Column('linked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['video_id'], ['downloads.video_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('user_id', 'video_id'),
    )
    op.create_index(
        'ix_download_links_video_id', 'download_links', ['video_id']
    )

    # Item ids are now per-user (see download_links), not one per file.
    op.drop_column('downloads', 'jellyfin_item_id')


def downgrade() -> None:
    op.add_column(
        'downloads',
        sa.Column('jellyfin_item_id', sa.String(length=64), nullable=True),
    )
    op.drop_index('ix_download_links_video_id', table_name='download_links')
    op.drop_table('download_links')
