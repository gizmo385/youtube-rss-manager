"""FreshRSS client over its Google Reader–compatible API.

Auth is the username plus an API password set in FreshRSS (Profile → API
management), exchanged at ``accounts/ClientLogin`` for an ``Auth`` token sent as
``Authorization: GoogleLogin auth=<token>``. Writes also need an action token
(``T``) from ``reader/api/0/token``.

Endpoint shapes checked against FreshRSS's ``p/api/greader.php`` (October 2026):

- Items come back in "compat" mode, which has **no** feed guid — only the
  item's link (``alternate[0].href``) and its feed (``origin.streamId``). Callers
  map items to videos by link.
- ``stream/items/ids`` returns bare decimal ids; ``stream/items/contents`` and
  ``edit-tag`` accept those decimal ids as ``i`` (repeated), while contents
  responses give ids as ``tag:google.com,2005:reader/item/<hex>``.
- ``ot`` filters on crawl time *or* content-modified time — not on when the read
  state last changed. There's no "read state changed since" query, so the sync
  re-lists a whole window's read ids each run.
- Repeated form fields are parsed from the raw body (capped at 1 MiB), so ids go
  out in batches.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

READING_LIST = "user/-/state/com.google/reading-list"
READ = "user/-/state/com.google/read"

# Ids per stream/items/ids page.
_PAGE = 1000
# Ids per contents/edit-tag request: ~20 bytes each, far under the 1 MiB cap.
_ID_BATCH = 250


def _batched(ids: list[int]) -> Iterator[list[int]]:
    for i in range(0, len(ids), _ID_BATCH):
        yield ids[i : i + _ID_BATCH]


def api_base(base_url: str) -> str:
    """The API root for a FreshRSS URL, whether it was given as the site
    (``https://rss.example.com``) or the API endpoint itself."""
    base = base_url.strip().rstrip("/")
    return base if base.endswith("/api/greader.php") else f"{base}/api/greader.php"


class FreshRssError(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    id: int
    stream_id: str  # "feed/<n>"
    link: str


@dataclass
class FreshRssClient:
    base_url: str
    username: str
    api_password: str
    _auth: str | None = field(default=None, repr=False)
    _token: str | None = field(default=None, repr=False)

    def _url(self, path: str) -> str:
        return f"{api_base(self.base_url)}{path}"

    def _login(self) -> str:
        if self._auth is None:
            try:
                resp = httpx.post(
                    self._url("/accounts/ClientLogin"),
                    data={"Email": self.username, "Passwd": self.api_password},
                    timeout=_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise FreshRssError(f"login failed: {exc}") from exc
            if resp.status_code != 200:
                raise FreshRssError(f"login failed: HTTP {resp.status_code}")
            for line in resp.text.splitlines():
                if line.startswith("Auth="):
                    self._auth = line.removeprefix("Auth=").strip()
                    break
            else:
                raise FreshRssError("login response had no Auth token")
        return self._auth

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"GoogleLogin auth={self._login()}"}
        try:
            resp = httpx.request(method, self._url(path), headers=headers, timeout=_TIMEOUT, **kwargs)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FreshRssError(f"{method} {path} failed: {exc}") from exc
        return resp

    def verify(self) -> bool:
        """Cheap credential check for the settings page."""
        try:
            self._request("GET", "/reader/api/0/user-info", params={"output": "json"})
            return True
        except FreshRssError:
            return False

    def subscriptions(self) -> dict[str, str]:
        """Map ``stream id ("feed/<n>") -> feed URL`` for every subscription."""
        data = self._request("GET", "/reader/api/0/subscription/list", params={"output": "json"}).json()
        return {s["id"]: s.get("url", "") for s in data.get("subscriptions", [])}

    def item_ids(self, stream: str, since: datetime) -> list[int]:
        """Ids of every item in ``stream`` crawled (or modified) since ``since``."""
        ids: list[int] = []
        continuation = ""
        while True:
            params = {"s": stream, "ot": str(int(since.timestamp())), "n": str(_PAGE), "output": "json"}
            if continuation:
                params["c"] = continuation
            data = self._request("GET", "/reader/api/0/stream/items/ids", params=params).json()
            ids.extend(int(ref["id"]) for ref in data.get("itemRefs") or [])
            continuation = data.get("continuation") or ""
            if not continuation:
                return ids

    def items(self, ids: list[int]) -> list[Item]:
        """Feed and link for each of ``ids`` (items deleted meanwhile are omitted)."""
        out: list[Item] = []
        for batch in _batched(ids):
            data = self._request(
                "POST", "/reader/api/0/stream/items/contents", data={"i": [str(i) for i in batch]}
            ).json()
            for item in data.get("items", []):
                alternate = item.get("alternate") or item.get("canonical") or [{}]
                out.append(
                    Item(
                        id=int(item["id"].rsplit("/", 1)[-1], 16),
                        stream_id=(item.get("origin") or {}).get("streamId", ""),
                        link=alternate[0].get("href", ""),
                    )
                )
        return out

    def set_read(self, ids: list[int], read: bool) -> None:
        """Mark ``ids`` read (or unread)."""
        if not ids:
            return
        if self._token is None:
            self._token = self._request("GET", "/reader/api/0/token").text.strip()
        for batch in _batched(ids):
            body = {"T": self._token, "a" if read else "r": READ, "i": [str(i) for i in batch]}
            resp = self._request("POST", "/reader/api/0/edit-tag", data=body)
            if resp.text.strip() != "OK":
                raise FreshRssError(f"edit-tag returned {resp.text[:100]!r}")
