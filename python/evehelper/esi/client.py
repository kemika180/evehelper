"""The one cached async ESI transport. All network I/O to ESI goes through here.

Enforces ESI's rules in a single place: honours ``Expires`` (never re-fetches
before it) and ``ETag`` (conditional ``If-None-Match`` revalidation), backs off on
the error-limit budget, sends a descriptive ``User-Agent``, and follows paging.

The clock and sleep are injected so cache expiry and backoff are deterministic
under test. This module is impure by design (network, wall-clock) — it is one of
the two places (with ``data/``) allowed to touch the outside world.
"""

from __future__ import annotations

import asyncio
import contextlib
import pickle
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from evehelper import __version__
from evehelper.config import Config

_BASE_URL = "https://esi.evetech.net/latest"
# Back off once the remaining error budget for the window drops to this.
_ERROR_LIMIT_FLOOR = 5
# Bounded concurrency for paged fetches — fast without hammering ESI. Successful
# requests don't count against the error-limit budget; the cap keeps bursts modest.
_MAX_CONCURRENT_PAGES = 8
# Bump when `_CacheEntry`'s shape changes so an old on-disk cache is discarded, not misread.
_CACHE_VERSION = 1
# The region-wide market order book: hundreds of pages (100s of MB) with a ~5-minute
# Expires. It's re-fetched almost every launch anyway, so it's cached in-memory for the
# session but never written to disk — persisting it would bloat the file for no speed-up.
_UNPERSISTED_PATH = re.compile(r"/markets/\d+/orders/$")

Params = Mapping[str, str | int]


class EsiError(Exception):
    """An ESI request returned a non-success, non-304 status."""

    def __init__(self, status_code: int, path: str) -> None:
        super().__init__(f"ESI {status_code} for {path}")
        self.status_code = status_code
        self.path = path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_expires(response: httpx.Response) -> datetime | None:
    raw = response.headers.get("Expires")
    if raw is None:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass
class _CacheEntry:
    etag: str | None
    expires: datetime | None
    body: bytes
    pages: int


def _load_cache(path: Path) -> dict[str, _CacheEntry]:
    """The persisted response cache from a previous session, or empty on any problem —
    a corrupt/old/absent cache must degrade to a cold start, never crash the launch."""
    try:
        with path.open("rb") as handle:
            version, entries = pickle.load(handle)
    except (
        OSError,
        pickle.UnpicklingError,
        EOFError,
        ValueError,
        TypeError,
        AttributeError,
        # A cache written before the evetrader→evehelper rename pickled classes under the
        # old module path; unpickling now fails to import it. Treat as a cold start.
        ImportError,
    ):
        return {}
    if version != _CACHE_VERSION or not isinstance(entries, dict):
        return {}
    return entries


def _persistable(url: str) -> bool:
    """Whether a cached response is worth keeping between sessions. Excludes the region-wide
    order book (see ``_UNPERSISTED_PATH``): huge and short-lived, so it dominates the file
    yet is re-fetched every launch. Everything else has a long enough TTL to speed a relaunch."""
    return _UNPERSISTED_PATH.search(urlsplit(url).path) is None


def _save_cache(path: Path, cache: Mapping[str, _CacheEntry]) -> None:
    """Persist the worth-keeping slice of the response cache atomically (temp file + rename).
    Best-effort — a failed write just means the next session starts colder, so it's
    suppressed, not fatal."""
    persistable = {url: entry for url, entry in cache.items() if _persistable(url)}
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as handle:
            pickle.dump((_CACHE_VERSION, persistable), handle, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)


class EsiClient:
    """Cached, rate-limit-aware async GET transport for ESI."""

    def __init__(
        self,
        config: Config,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cache_path: Path | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self._now = now
        self._sleep = sleep
        # Persist the response cache between sessions: a relaunch reuses entries still fresh
        # per their Expires (no fetch) and revalidates the rest with If-None-Match (cheap 304).
        self._cache_path = cache_path
        self._cache: dict[str, _CacheEntry] = (
            _load_cache(cache_path) if cache_path is not None else {}
        )
        self._error_remain: int | None = None
        self._error_reset_at: datetime | None = None

    def save_cache(self) -> None:
        """Write the response cache to disk (if a ``cache_path`` was given) so the next launch
        reuses still-fresh data instead of re-pulling everything. Call at shutdown."""
        if self._cache_path is not None:
            _save_cache(self._cache_path, self._cache)

    @property
    def _user_agent(self) -> str:
        return f"evehelper/{__version__} ({self._config.contact})"

    async def get(
        self, path: str, *, params: Params | None = None, token: str | None = None
    ) -> bytes:
        """Fetch one resource body, from cache when still fresh."""
        body, _pages = await self._request(path, params=params, token=token)
        return body

    async def get_all_pages(
        self, path: str, *, params: Params | None = None, token: str | None = None
    ) -> list[bytes]:
        """Fetch every page of a paged resource, following the ``X-Pages`` header.

        Page 1 is fetched first to learn the page count; the rest are fetched with
        bounded concurrency and returned in page order.
        """
        base = dict(params or {})
        first, pages = await self._request(path, params={**base, "page": 1}, token=token)
        if pages <= 1:
            return [first]

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

        async def fetch(page: int) -> bytes:
            async with semaphore:
                body, _ = await self._request(path, params={**base, "page": page}, token=token)
                return body

        rest = await asyncio.gather(*(fetch(page) for page in range(2, pages + 1)))
        return [first, *rest]

    async def post_json(self, path: str, *, body: object, token: str | None = None) -> bytes:
        """POST a JSON body (e.g. an id list) and return the response body.

        Not cached — POST responses aren't Expires-cacheable — but still subject to
        the error-limit budget and the descriptive User-Agent.
        """
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        await self._respect_error_limit()
        response = await self._http.post(_BASE_URL + path, json=body, headers=headers)
        self._observe_error_limit(response)
        if response.status_code != httpx.codes.OK:
            raise EsiError(response.status_code, path)
        return response.content

    async def _request(
        self, path: str, *, params: Params | None, token: str | None
    ) -> tuple[bytes, int]:
        url = _BASE_URL + path
        key = str(self._http.build_request("GET", url, params=params).url)
        entry = self._cache.get(key)
        if entry is not None and entry.expires is not None and self._now() < entry.expires:
            return entry.body, entry.pages

        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if entry is not None and entry.etag is not None:
            headers["If-None-Match"] = entry.etag

        await self._respect_error_limit()
        response = await self._http.get(url, params=params, headers=headers)
        self._observe_error_limit(response)

        if response.status_code == 304 and entry is not None:
            entry.expires = _parse_expires(response)
            return entry.body, entry.pages
        if response.status_code != httpx.codes.OK:
            raise EsiError(response.status_code, path)

        fresh = _CacheEntry(
            etag=response.headers.get("ETag"),
            expires=_parse_expires(response),
            body=response.content,
            pages=int(response.headers.get("X-Pages", "1")),
        )
        self._cache[key] = fresh
        return fresh.body, fresh.pages

    async def _respect_error_limit(self) -> None:
        if self._error_remain is None or self._error_remain > _ERROR_LIMIT_FLOOR:
            return
        if self._error_reset_at is None:
            return
        delay = (self._error_reset_at - self._now()).total_seconds()
        if delay > 0:
            await self._sleep(delay)

    def _observe_error_limit(self, response: httpx.Response) -> None:
        remain = response.headers.get("X-Esi-Error-Limit-Remain")
        reset = response.headers.get("X-Esi-Error-Limit-Reset")
        if remain is not None:
            self._error_remain = int(remain)
        if reset is not None:
            self._error_reset_at = self._now() + timedelta(seconds=int(reset))
