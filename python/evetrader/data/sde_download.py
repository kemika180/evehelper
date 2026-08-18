"""Download the EVE SDE SQLite into the local data dir. Impure (network + file I/O).

The SDE is static game data (blueprints, volumes, PI schematics) ESI doesn't serve.
It's large (~140 MB compressed) and versioned by CCP's patches, so it's downloaded
once and refreshed on demand — never committed, never fetched per run. Fuzzwork
publishes a ready-to-query SQLite conversion; we stream its gzip and decompress it in
one pass.

This is a one-off static-data fetch to a non-ESI host, so it deliberately does not go
through ``esi/client.py`` (no error-limit budget or ESI cache applies); it still sends
a descriptive User-Agent.
"""

from __future__ import annotations

import enum
import zlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

FUZZWORK_SDE_URL = "https://www.fuzzwork.co.uk/dump/latest-sqlite.db.gz"
_CHUNK = 1 << 20  # 1 MiB
_GZIP_WBITS = 16 + zlib.MAX_WBITS  # decode a gzip (not raw zlib) stream

# Called with (compressed bytes downloaded so far, total compressed bytes or None) after
# each chunk, so a caller can drive a progress bar. Progress tracks the *compressed*
# transfer — the slow part; decompression is effectively instant.
ProgressFn = Callable[[int, int | None], None]


class SdeState(enum.Enum):
    """Freshness of the local SDE relative to the published dump."""

    CURRENT = "current"  # present and no newer dump is published
    MISSING = "missing"  # not downloaded yet
    STALE = "stale"  # a newer dump exists than the local copy
    UNKNOWN = "unknown"  # present, but the remote couldn't be checked (offline)


def remote_last_modified(
    *, url: str = FUZZWORK_SDE_URL, contact: str = "", client: httpx.Client | None = None
) -> datetime | None:
    """The dump's ``Last-Modified`` time via a cheap HEAD, or None if it can't be read
    (offline, no header, HEAD unsupported). Never raises — freshness is best-effort."""
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=10.0)
    try:
        headers = {"User-Agent": f"evetrader ({contact})"} if contact else {}
        response = client.head(url, headers=headers)
        response.raise_for_status()
        stamp = response.headers.get("Last-Modified")
        return parsedate_to_datetime(stamp) if stamp else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    finally:
        if owns_client:
            client.close()


def check_sde_freshness(
    path: Path,
    *,
    url: str = FUZZWORK_SDE_URL,
    contact: str = "",
    client: httpx.Client | None = None,
) -> SdeState:
    """Whether the local SDE at ``path`` is missing, stale, current, or uncheckable —
    by comparing its mtime to the published dump's ``Last-Modified``."""
    exists = path.exists()
    remote = remote_last_modified(url=url, contact=contact, client=client)
    if not exists:
        return SdeState.MISSING
    if remote is None:
        return SdeState.UNKNOWN
    local = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return SdeState.STALE if remote > local else SdeState.CURRENT


def write_decompressed(chunks: Iterable[bytes], dest: Path) -> None:
    """Gzip-decompress a stream of chunks into ``dest``, writing atomically (via a
    ``.part`` file renamed on success) so an interrupted download leaves no half file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    with partial.open("wb") as out:
        for chunk in chunks:
            out.write(decompressor.decompress(chunk))
        out.write(decompressor.flush())
    partial.replace(dest)


def _content_length(response: httpx.Response) -> int | None:
    """The response's compressed size from ``Content-Length``, or None if absent/unparsable."""
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _reporting(
    chunks: Iterable[bytes], total: int | None, on_progress: ProgressFn | None
) -> Iterable[bytes]:
    """Yield chunks unchanged, reporting the running byte count to ``on_progress``."""
    downloaded = 0
    for chunk in chunks:
        downloaded += len(chunk)
        if on_progress is not None:
            on_progress(downloaded, total)
        yield chunk


def download_sde(
    dest: Path,
    *,
    url: str = FUZZWORK_SDE_URL,
    contact: str = "",
    client: httpx.Client | None = None,
    on_progress: ProgressFn | None = None,
) -> None:
    """Stream the compressed SDE from ``url``, decompress, and write to ``dest``.

    ``on_progress`` (if given) is called after each chunk with the compressed bytes
    downloaded and the total from ``Content-Length`` (or None when the server omits it)."""
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=None)
    try:
        headers = {"User-Agent": f"evetrader ({contact})"} if contact else {}
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            total = _content_length(response)
            write_decompressed(_reporting(response.iter_bytes(_CHUNK), total, on_progress), dest)
    finally:
        if owns_client:
            client.close()
