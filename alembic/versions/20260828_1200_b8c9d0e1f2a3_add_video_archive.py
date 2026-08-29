"""add video archive: downloads, jellyfin accounts, playlists, retention prefs

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-28 12:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Cascading preferences, mirroring the include_shorts/include_live pattern:
# NOT NULL with a default at the user level (the cascade must terminate),
# nullable at category and subscription level (NULL == inherit).
_USER_COLUMNS = (
    ('download_enabled', sa.Boolean(), sa.text('false')),
    ('keep_last_n', sa.Integer(), sa.text('15')),
    ('max_duration_seconds', sa.Integer(), sa.text('0')),
    ('generate_podcast', sa.Boolean(), sa.text('false')),
    ('link_target', sa.String(length=16), sa.text("'youtube'")),
)

_INHERITABLE_COLUMNS = (
    ('download_enabled', sa.Boolean()),
    ('keep_last_n', sa.Integer()),
    ('max_duration_seconds', sa.Integer()),
    ('generate_podcast', sa.Boolean()),
    ('link_target', sa.String(length=16)),
)


def upgrade() -> None:
    for name, type_, default in _USER_COLUMNS:
        op.add_column(
            'users',
            sa.Column(name, type_, server_default=default, nullable=False),
        )

    for table in ('categories', 'subscriptions'):
        for name, type_ in _INHERITABLE_COLUMNS:
            op.add_column(table, sa.Column(name, type_, nullable=True))

    op.create_table(
        'videos',
        sa.Column('video_id', sa.String(length=32), nullable=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=512), server_default='', nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column(
            'first_seen_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['channel_id'], ['channels.channel_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('video_id'),
    )
    op.create_index('ix_videos_channel_id', 'videos', ['channel_id'])
    op.create_index('ix_videos_published_at', 'videos', ['published_at'])

    op.create_table(
        'downloads',
        sa.Column('video_id', sa.String(length=32), nullable=False),
        sa.Column(
            'status', sa.String(length=16), server_default='pending', nullable=False
        ),
        sa.Column('skip_reason', sa.String(length=32), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('file_path', sa.Text(), nullable=True),
        sa.Column('file_size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('audio_path', sa.Text(), nullable=True),
        sa.Column('audio_size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('jellyfin_item_id', sa.String(length=64), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['video_id'], ['videos.video_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('video_id'),
    )
    op.create_index('ix_downloads_status', 'downloads', ['status'])

    op.create_table(
        'jellyfin_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('base_url', sa.String(length=512), nullable=False),
        sa.Column('api_key_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column(
            'jellyfin_user_id', sa.String(length=64), server_default='', nullable=False
        ),
        sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_jellyfin_account_user'),
    )

    op.create_table(
        'category_playlists',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('playlist_id', sa.String(length=64), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['category_id'], ['categories.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('user_id', 'category_id'),
    )


def downgrade() -> None:
    op.drop_table('category_playlists')
    op.drop_table('jellyfin_accounts')
    op.drop_index('ix_downloads_status', table_name='downloads')
    op.drop_table('downloads')
    op.drop_index('ix_videos_published_at', table_name='videos')
    op.drop_index('ix_videos_channel_id', table_name='videos')
    op.drop_table('videos')

    for table in ('subscriptions', 'categories'):
        for name, _ in _INHERITABLE_COLUMNS:
            op.drop_column(table, name)
    for name, _, _ in _USER_COLUMNS:
        op.drop_column('users', name)
