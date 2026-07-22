"""Download the EVE SDE SQLite into the local data dir. Impure (network + file I/O).

The SDE is static game data (blueprints, volumes, PI schematics) ESI doesn't serve.
It's large (~250 MB) and versioned by CCP's patches, so it's downloaded once and
refreshed on demand — never committed, never fetched per run. Fuzzwork publishes a
ready-to-query SQLite conversion; we stream its bz2 and decompress it in one pass.

This is a one-off static-data fetch to a non-ESI host, so it deliberately does not go
through ``esi/client.py`` (no error-limit budget or ESI cache applies); it still sends
a descriptive User-Agent.
"""

from __future__ import annotations

import bz2
from collections.abc import Iterable
from pathlib import Path

import httpx

FUZZWORK_SDE_URL = "https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2"
_CHUNK = 1 << 20  # 1 MiB


def write_decompressed(chunks: Iterable[bytes], dest: Path) -> None:
    """Bz2-decompress a stream of chunks into ``dest``, writing atomically (via a
    ``.part`` file renamed on success) so an interrupted download leaves no half file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    decompressor = bz2.BZ2Decompressor()
    with partial.open("wb") as out:
        for chunk in chunks:
            out.write(decompressor.decompress(chunk))
    partial.replace(dest)


def download_sde(
    dest: Path,
    *,
    url: str = FUZZWORK_SDE_URL,
    contact: str = "",
    client: httpx.Client | None = None,
) -> None:
    """Stream the compressed SDE from ``url``, decompress, and write to ``dest``."""
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=None)
    try:
        headers = {"User-Agent": f"evetrader ({contact})"} if contact else {}
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            write_decompressed(response.iter_bytes(_CHUNK), dest)
    finally:
        if owns_client:
            client.close()
