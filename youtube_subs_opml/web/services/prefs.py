"""Cascading preference resolution: subscription > category > user.

This generalizes the pattern already used by ``resolve_include_shorts`` and
``resolve_include_live``. Those two are left in place so this change doesn't
touch the feed proxy's hot path; folding them into ``resolve`` is a tidy
follow-up, not a prerequisite.

Semantics worth being explicit about, because two different things are being
expressed with similar-looking values:

- ``None`` at the subscription or category level means *inherit*. The user
  level is never NULL, so the cascade always terminates.
- ``0`` for ``keep_last_n`` and ``max_duration_seconds`` means *unlimited*.
  Using NULL for that would collide with "inherit".
"""

from __future__ import annotations

from typing import Literal, TypeVar

T = TypeVar("T")

LinkTarget = Literal["youtube", "when_ready", "hold"]

LINK_TARGETS: tuple[str, ...] = ("youtube", "when_ready", "hold")

#: What each link_target does to a feed entry:
#:
#: youtube     — always link to YouTube. Current behavior; the default.
#: when_ready  — publish immediately linking to YouTube, then rewrite the link
#:               to Jellyfin once the download completes. Readers dedupe on the
#:               entry's ``yt:videoId`` GUID, not the link, so this updates in
#:               place rather than duplicating. Some readers do re-mark an
#:               updated entry unread, which is the tradeoff.
#: hold        — withhold the entry entirely until the file is in Jellyfin,
#:               then publish it once with the Jellyfin link. No rewrite, no
#:               dedupe question, no unread churn — at the cost of latency, and
#:               needing a fallback so permanently-failed downloads eventually
#:               publish with the YouTube link instead of vanishing.


def resolve(sub_pref: T | None, cat_pref: T | None, user_pref: T) -> T:
    """Return the first non-NULL preference walking up the cascade."""
    if sub_pref is not None:
        return sub_pref
    if cat_pref is not None:
        return cat_pref
    return user_pref


# --- form parsing --------------------------------------------------------
#
# The settings UI edits these prefs at three levels. At the user level a value
# is never NULL (the cascade must terminate); at the category and subscription
# levels the widget offers an extra "inherit" choice that maps to NULL. These
# helpers turn the raw form strings into the right typed value for each level.


def parse_tristate_bool(value: str | None) -> bool | None:
    """``'true'``/``'false'`` → bool; anything else (``'inherit'``) → None."""
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def parse_inherit_int(value: str | None) -> int | None:
    """A non-negative int, or None for blank/``'inherit'``.

    Used for ``keep_last_n`` and (minutes) ``max_duration`` at the inheritable
    levels, where blank means inherit and ``0`` means unlimited. Unparseable
    input is treated as inherit rather than raising, so a stray keystroke can't
    500 the pref update.
    """
    if value is None:
        return None
    stripped = value.strip().lower()
    if stripped in ("", "inherit"):
        return None
    try:
        return max(int(stripped), 0)
    except ValueError:
        return None


def parse_required_int(value: str | None, default: int) -> int:
    """A non-negative int with a fallback — for the user level, which is NOT NULL."""
    parsed = parse_inherit_int(value)
    return default if parsed is None else parsed


def parse_link_target(value: str | None, *, allow_inherit: bool) -> str | None:
    """Validate a link_target against the known set.

    Returns None for inherit (category/subscription level) or the user default
    ``'youtube'`` (user level), so an unexpected value can never be stored.
    """
    if value in LINK_TARGETS:
        return value
    return None if allow_inherit else "youtube"


def minutes_to_seconds(value: str | None) -> int | None:
    """Minutes form field → seconds. Blank/inherit → None; ``0`` → 0 (no limit)."""
    minutes = parse_inherit_int(value)
    return None if minutes is None else minutes * 60


def is_within_duration_limit(
    duration_seconds: int | None, max_duration_seconds: int
) -> bool:
    """True if a video is short enough to download.

    Fails *open* on an unknown duration: a probe that couldn't determine length
    shouldn't silently drop the video. The download itself will fail loudly if
    something is actually wrong with it.
    """
    if max_duration_seconds <= 0:
        return True
    if duration_seconds is None:
        return True
    return duration_seconds <= max_duration_seconds


def meets_duration_floor(
    duration_seconds: int | None, min_duration_seconds: int
) -> bool:
    """True if a video is long enough to be worth archiving.

    The floor to ``is_within_duration_limit``'s ceiling, for keeping clips and
    one-minute updates out of an archive meant for longer-form content. 0 — the
    default — means no floor. Fails open on an unknown duration for the same
    reason the ceiling does: a failed probe shouldn't silently drop a video.
    """
    if min_duration_seconds <= 0:
        return True
    if duration_seconds is None:
        return True
    return duration_seconds >= min_duration_seconds
