"""The SDE downloader streams a bz2 dump and decompresses it to a local file."""

import bz2
from pathlib import Path

import httpx

from evetrader.data.sde_download import download_sde, write_decompressed


def test_write_decompressed_reassembles_across_chunks(tmp_path: Path) -> None:
    payload = b"CREATE TABLE industryActivityProducts;" * 500
    compressed = bz2.compress(payload)
    # Feed it in tiny chunks to exercise the streaming decompressor across boundaries.
    chunks = [compressed[i : i + 7] for i in range(0, len(compressed), 7)]

    dest = tmp_path / "sde.sqlite"
    write_decompressed(chunks, dest)

    assert dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()  # atomic temp cleaned up


def test_download_sde_streams_decompresses_and_sends_user_agent(tmp_path: Path) -> None:
    payload = b"sqlite-bytes"
    compressed = bz2.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "evetrader" in request.headers.get("User-Agent", "")
        return httpx.Response(200, content=compressed)

    dest = tmp_path / "sde.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_sde(dest, url="https://example.test/sde.bz2", contact="c@e.com", client=client)

    assert dest.read_bytes() == payload
