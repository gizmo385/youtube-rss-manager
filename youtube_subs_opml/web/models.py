from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    oidc_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255), server_default="")
    include_shorts: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), default=True
    )
    include_live: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), default=True
    )
    # --- Archive preferences (root of the cascade; never NULL) ---------------
    download_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    # 0 means "keep everything". Non-zero prunes to the N most recent per
    # channel. NULL is reserved for "inherit" at the category/subscription
    # levels, so the user level uses 0 rather than NULL for unlimited.
    keep_last_n: Mapped[int] = mapped_column(
        Integer, server_default=text("15"), default=15
    )
    # 0 means "no limit". Videos longer than this are marked skipped rather
    # than downloaded, so six-hour streams don't eat the disk.
    max_duration_seconds: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    generate_podcast: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    # 'youtube' | 'when_ready' | 'hold' — see services/prefs.LinkTarget.
    link_target: Mapped[str] = mapped_column(
        String(16), server_default="youtube", default="youtube"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    youtube_accounts: Mapped[list["YoutubeAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    categories: Mapped[list["Category"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class YoutubeAccount(Base):
    __tablename__ = "youtube_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "channel_id", name="uq_user_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_title: Mapped[str] = mapped_column(String(255), server_default="")
    refresh_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="youtube_accounts")


class Channel(Base):
    __tablename__ = "channels"

    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), server_default="")
    description: Mapped[str] = mapped_column(Text, server_default="")
    # Native array on Postgres; JSON on SQLite (for local/dev use).
    youtube_topics: Mapped[list[str] | None] = mapped_column(
        ARRAY(String).with_variant(JSON, "sqlite"), nullable=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("channels.channel_id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("youtube_accounts.id", ondelete="CASCADE"), nullable=True
    )
    ignored: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    include_shorts: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    include_live: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # NULL == inherit from category, then user.
    download_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    keep_last_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generate_podcast: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    link_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_user_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    include_shorts: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    include_live: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # NULL == inherit from user.
    download_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    keep_last_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generate_podcast: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    link_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="categories")


class ChannelCategory(Base):
    __tablename__ = "channel_categories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "channel_id"],
            ["subscriptions.user_id", "subscriptions.channel_id"],
            ondelete="CASCADE",
            name="fk_channel_categories_subscription",
        ),
    )

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class VideoShort(Base):
    """Permanent cache of whether a video id is a YouTube Short.

    A video's Short-ness never changes, so rows are written once and never
    expire. Populated lazily by the feed proxy when filtering Shorts.
    """

    __tablename__ = "video_shorts"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    is_short: Mapped[bool] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class VideoLiveStatus(Base):
    """Cache of a video's live/premiere status: 'none' | 'upcoming' | 'live'.

    Only partly immutable: 'none' (a regular upload or an aired stream/premiere)
    is terminal and cached permanently, while 'upcoming'/'live' are transient
    and re-probed after a short TTL (see services.live).
    """

    __tablename__ = "video_live_status"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Video(Base):
    """A video seen on a channel's RSS feed.

    Populated by the scheduled poller (services.poller), *not* by the feed
    proxy — the proxy only runs when an RSS reader polls it, which would make
    the archive depend on the reader's schedule and on which OPML feeds happen
    to be subscribed.

    Note that YouTube's channel feed only returns the ~15 most recent entries,
    so the poll interval bounds what can be captured for prolific channels.
    """

    __tablename__ = "videos"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("channels.channel_id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), server_default="")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Filled in by the downloader's metadata probe, not by the poller: the RSS
    # feed doesn't carry duration. NULL means "not probed yet".
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Download(Base):
    """Download state for a video. One row per video, globally.

    Deliberately *not* per-user: the file on disk is shared. Per-user intent is
    resolved at enqueue/prune time by unioning across every user who subscribes
    to the channel (see services/retention.py), so the most permissive
    retention wins and nobody's videos vanish because a housemate set
    keep_last_n lower.

    Status flow:
        pending -> probing -> downloading -> complete
                     |            |
                     v            v
                  skipped       failed -> (retry) pending
    """

    __tablename__ = "downloads"

    video_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("videos.video_id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), server_default="pending", default="pending", index=True
    )
    # Why a row is terminal-but-not-complete: 'too_long' | 'unavailable' |
    # 'members_only' | 'no_subscribers' — kept so we don't re-probe forever.
    skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    # Retry backoff: the worker ignores pending rows until this passes.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Separate audio extraction for podcast feeds. NULL if not requested.
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # NB: the Jellyfin item id is *not* here — the same file is hardlinked into
    # each subscriber's own library, so it has a distinct item id per user. That
    # mapping lives on DownloadLink.

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DownloadLink(Base):
    """A per-user hardlink of a completed download into that user's library.

    Files are downloaded once to a canonical path (``Download.file_path``) that
    no Jellyfin library scans, then hardlinked into a subtree per subscribing
    user. Each user points their own Jellyfin library at their subtree, giving
    true per-user visibility and independent watched state without duplicating a
    byte on disk — the inode is shared, and its data is freed only when the last
    link is removed.

    A row here means "this user should have this video visible in their
    library". The worker reconciles the actual hardlinks against these rows.
    """

    __tablename__ = "download_links"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("downloads.video_id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    # The .mkv hardlink path in the user's subtree. NULL until the worker has
    # actually created the link.
    link_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # This user's Jellyfin item id, set after their library is scanned (Phase 3).
    # Distinct from any other user's id for the same underlying file.
    jellyfin_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JellyfinAccount(Base):
    """Per-user Jellyfin credentials.

    Jellyfin has no OAuth authorization-code flow, and this deployment
    authenticates users through Keycloak via the SSO plugin, so most users have
    no local password to exchange for a token. That leaves an API key generated
    in the Jellyfin dashboard and pasted here. The key is encrypted with the
    same Fernet key used for YouTube refresh tokens.

    ``jellyfin_user_id`` is the GUID of the Jellyfin user that playlists should
    be created for — an admin API key can act on behalf of a user, but it has
    to be told which one.
    """

    __tablename__ = "jellyfin_accounts"
    __table_args__ = (UniqueConstraint("user_id", name="uq_jellyfin_account_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    base_url: Mapped[str] = mapped_column(String(512))
    api_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    jellyfin_user_id: Mapped[str] = mapped_column(String(64), server_default="")
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CategoryPlaylist(Base):
    """Maps a category to the Jellyfin playlist mirroring it.

    Files live once on disk under a single path; categories are expressed as
    playlists rather than directories. That sidesteps the many-to-many problem
    (a channel in both Aviation and Engineering needs no hardlinks) and keeps
    per-user views separate.
    """

    __tablename__ = "category_playlists"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    playlist_id: Mapped[str] = mapped_column(String(64))
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChannelFeedCache(Base):
    """Last good raw Atom XML for a channel's YouTube RSS feed.

    The feed proxy serves readers from this cache rather than fetching YouTube on
    every request: an RSS reader polls every feed URL at once, and proxying each
    to YouTube synchronously turned that into a burst the IP got throttled for.
    The scheduled poller — which already fetches these feeds on a spread-out,
    backed-off cadence — writes the raw XML here, and the proxy reads it. One row
    per channel; the upstream feed is per-channel and user-independent (filtering
    happens after), so it's shared across all subscribers.
    """

    __tablename__ = "channel_feed_cache"

    channel_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("channels.channel_id", ondelete="CASCADE"),
        primary_key=True,
    )
    xml: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OpmlToken(Base):
    __tablename__ = "opml_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
