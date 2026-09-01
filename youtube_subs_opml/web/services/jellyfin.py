"""Jellyfin API client: library refresh, item lookup, playlist sync.

Auth is an API key generated in the Jellyfin dashboard and pasted into settings,
encrypted at rest with the same Fernet key as YouTube refresh tokens. Jellyfin
has no OAuth authorization-code flow, and this deployment authenticates through
Keycloak, so most users have no local password to exchange for a token — an API
key is the pragmatic choice.

Endpoint shapes verified against the Jellyfin 10.11 API (August 2026):

- Auth is the standard ``Authorization: MediaBrowser Token="<key>"`` header. The
  older ``X-Emby-Token`` / ``X-Emby-Authorization`` headers are deprecated and
  slated for removal in 10.12, so they're not sent.
- There is **no** ProviderId query filter (``AnyProviderIdEquals`` is an Emby
  feature that returns everything on Jellyfin), so items are resolved by matching
  the ``Path`` field — cheap here because each user's library is a small,
  per-user subtree.
- Playlist item removal keys off each entry's ``PlaylistItemId`` (not the media
  item id), passed as ``entryIds``. API-key support for the DELETE landed in
  jellyfin/jellyfin#14154; on older 10.11.x builds it can 400, so removal is
  treated as best-effort by the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


class JellyfinError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaylistEntry:
    """One membership row in a playlist.

    ``item_id`` is the underlying media item; ``entry_id`` (Jellyfin's
    ``PlaylistItemId``) identifies this specific placement and is what removal
    requires.
    """

    item_id: str
    entry_id: str


@dataclass(frozen=True)
class JellyfinClient:
    base_url: str
    api_key: str

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f'MediaBrowser Token="{self.api_key}"'}

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def verify(self) -> bool:
        """Cheap credential check for the settings page."""
        try:
            resp = httpx.get(
                self._url("/System/Info"), headers=self._headers(), timeout=_TIMEOUT
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def refresh_library(self) -> None:
        """Trigger a full library scan so newly written files become items.

        Downloads land on disk before Jellyfin indexes them, so nothing has an
        item id until this runs and completes. The sync therefore tolerates
        misses and retries on a later pass rather than failing.
        """
        try:
            resp = httpx.post(
                self._url("/Library/Refresh"), headers=self._headers(), timeout=_TIMEOUT
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"library refresh failed: {exc}") from exc

    def episode_paths(self, user_id: str) -> dict[str, str]:
        """Map ``file path -> item id`` for every episode the user can see.

        Jellyfin has no path or ProviderId filter, so we list the user's
        episodes (scoped by ``userId`` to their own library) with the ``Path``
        field and build the lookup ourselves. Paths are normalised so a trailing
        slash or separator style can't cause a miss.
        """
        try:
            resp = httpx.get(
                self._url("/Items"),
                headers=self._headers(),
                params={
                    "userId": user_id,
                    "includeItemTypes": "Episode",
                    "recursive": "true",
                    "fields": "Path",
                    "enableImages": "false",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"item listing failed: {exc}") from exc

        out: dict[str, str] = {}
        for item in resp.json().get("Items", []):
            path = item.get("Path")
            item_id = item.get("Id")
            if path and item_id:
                out[normalise_path(path)] = item_id
        return out

    def create_playlist(self, name: str, user_id: str) -> str:
        try:
            resp = httpx.post(
                self._url("/Playlists"),
                headers=self._headers(),
                json={
                    "Name": name,
                    "UserId": user_id,
                    "MediaType": "Video",
                    "IsPublic": False,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"playlist creation failed: {exc}") from exc
        return resp.json()["Id"]

    def add_to_playlist(
        self, playlist_id: str, item_ids: list[str], user_id: str
    ) -> None:
        if not item_ids:
            return
        try:
            resp = httpx.post(
                self._url(f"/Playlists/{playlist_id}/Items"),
                headers=self._headers(),
                params={"ids": ",".join(item_ids), "userId": user_id},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"playlist add failed: {exc}") from exc

    def playlist_entries(self, playlist_id: str, user_id: str) -> list[PlaylistEntry]:
        try:
            resp = httpx.get(
                self._url(f"/Playlists/{playlist_id}/Items"),
                headers=self._headers(),
                params={"userId": user_id},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"playlist read failed: {exc}") from exc
        entries = []
        for i in resp.json().get("Items", []):
            item_id = i.get("Id")
            # PlaylistItemId is the removable entry handle; fall back to Id if a
            # build omits it (older behaviour), which at least keeps adds working.
            entry_id = i.get("PlaylistItemId") or item_id
            if item_id and entry_id:
                entries.append(PlaylistEntry(item_id=item_id, entry_id=entry_id))
        return entries

    def remove_from_playlist(
        self, playlist_id: str, entry_ids: list[str], user_id: str
    ) -> None:
        if not entry_ids:
            return
        try:
            resp = httpx.delete(
                self._url(f"/Playlists/{playlist_id}/Items"),
                headers=self._headers(),
                params={"entryIds": ",".join(entry_ids), "userId": user_id},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JellyfinError(f"playlist remove failed: {exc}") from exc


def normalise_path(path: str) -> str:
    """Normalise a Jellyfin/OS path for comparison (posix separators, no trailing slash)."""
    return str(PurePosixPath(path.replace("\\", "/")))
