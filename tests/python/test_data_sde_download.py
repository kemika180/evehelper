"""The SDE downloader streams a gzip dump and decompresses it to a local file."""

import gzip
import os
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx

from evehelper.data.sde_download import (
    SdeState,
    check_sde_freshness,
    download_sde,
    write_decompressed,
)


def _head_client(last_modified: datetime | None, *, fail: bool = False) -> httpx.Client:
    """A client whose HEAD returns the given Last-Modified (or none / a connection error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        if fail:
            raise httpx.ConnectError("offline")
        headers = {}
        if last_modified is not None:
            headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)
        return httpx.Response(200, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_sde_freshness_reports_missing_when_absent(tmp_path: Path) -> None:
    dest = tmp_path / "sde.sqlite"  # never created
    with _head_client(datetime(2026, 1, 1, tzinfo=UTC)) as client:
        assert check_sde_freshness(dest, client=client) is SdeState.MISSING


def test_check_sde_freshness_reports_stale_when_remote_newer(tmp_path: Path) -> None:
    dest = tmp_path / "sde.sqlite"
    dest.write_bytes(b"local")
    old = (datetime.now(UTC) - timedelta(days=10)).timestamp()
    os.utime(dest, (old, old))
    with _head_client(datetime.now(UTC)) as client:
        assert check_sde_freshness(dest, client=client) is SdeState.STALE


def test_check_sde_freshness_reports_current_when_remote_older(tmp_path: Path) -> None:
    dest = tmp_path / "sde.sqlite"
    dest.write_bytes(b"local")  # mtime is now
    with _head_client(datetime.now(UTC) - timedelta(days=10)) as client:
        assert check_sde_freshness(dest, client=client) is SdeState.CURRENT


def test_check_sde_freshness_unknown_when_remote_unreadable(tmp_path: Path) -> None:
    dest = tmp_path / "sde.sqlite"
    dest.write_bytes(b"local")
    with _head_client(None, fail=True) as client:  # offline -> can't tell
        assert check_sde_freshness(dest, client=client) is SdeState.UNKNOWN


def test_write_decompressed_reassembles_across_chunks(tmp_path: Path) -> None:
    payload = b"CREATE TABLE industryActivityProducts;" * 500
    compressed = gzip.compress(payload)
    # Feed it in tiny chunks to exercise the streaming decompressor across boundaries.
    chunks = [compressed[i : i + 7] for i in range(0, len(compressed), 7)]

    dest = tmp_path / "sde.sqlite"
    write_decompressed(chunks, dest)

    assert dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()  # atomic temp cleaned up


def test_download_sde_streams_decompresses_and_sends_user_agent(tmp_path: Path) -> None:
    payload = b"sqlite-bytes"
    compressed = gzip.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "evehelper" in request.headers.get("User-Agent", "")
        return httpx.Response(200, content=compressed)

    dest = tmp_path / "sde.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_sde(dest, url="https://example.test/sde.bz2", contact="c@e.com", client=client)

    assert dest.read_bytes() == payload


def test_download_sde_reports_progress_against_content_length(tmp_path: Path) -> None:
    payload = b"x" * 4096
    compressed = gzip.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        # Explicit Content-Length so progress can report a total to divide against.
        return httpx.Response(
            200, content=compressed, headers={"Content-Length": str(len(compressed))}
        )

    ticks: list[tuple[int, int | None]] = []

    def record(downloaded: int, total: int | None) -> None:
        ticks.append((downloaded, total))
    dest = tmp_path / "sde.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_sde(
            dest, url="https://example.test/sde.gz", client=client, on_progress=record
        )

    assert ticks  # at least one progress tick fired
    assert all(total == len(compressed) for _, total in ticks)  # total is the compressed size
    assert ticks[-1][0] == len(compressed)  # final tick reports the full transfer
    assert [downloaded for downloaded, _ in ticks] == sorted(d for d, _ in ticks)  # monotonic


def test_download_sde_progress_total_none_without_content_length(tmp_path: Path) -> None:
    compressed = gzip.compress(b"y" * 100)

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx.Response computes Content-Length from content; strip it to simulate a
        # chunked response so the total is unknown.
        response = httpx.Response(200, content=compressed)
        del response.headers["Content-Length"]
        return response

    ticks: list[tuple[int, int | None]] = []

    def record(downloaded: int, total: int | None) -> None:
        ticks.append((downloaded, total))
    dest = tmp_path / "sde.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_sde(dest, url="https://example.test/sde.gz", client=client, on_progress=record)

    assert ticks and all(total is None for _, total in ticks)  # no length -> total is None
