"""EsiClient honours ESI's rules: Expires caching, ETag revalidation, paging,
error-limit backoff, descriptive User-Agent, and bearer auth."""

import asyncio
import json
import pickle
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from evetrader.config import Config
from evetrader.esi.client import EsiClient, EsiError

Handler = Callable[[httpx.Request], httpx.Response]


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="contact@example.com",
    )


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _http_date(dt: datetime) -> str:
    return format_datetime(dt, usegmt=True)


def _run(
    handler: Handler,
    body: Callable[[EsiClient], Awaitable[None]],
    *,
    now: _Clock | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    clock = now or _Clock(datetime(2020, 1, 1, tzinfo=UTC))

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            kwargs = {"now": clock} if sleep is None else {"now": clock, "sleep": sleep}
            client = EsiClient(_config(), http, **kwargs)  # type: ignore[arg-type]
            await body(client)

    asyncio.run(go())


def test_expires_prevents_refetch_until_it_passes() -> None:
    calls = 0
    clock = _Clock(datetime(2020, 1, 1, tzinfo=UTC))
    future = _http_date(clock.now + timedelta(minutes=5))

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Expires": future, "ETag": "v1"}, json=[1, 2, 3])

    async def body(client: EsiClient) -> None:
        first = await client.get("/x/")
        second = await client.get("/x/")
        assert first == second

    _run(handler, body, now=clock)
    assert calls == 1  # second call served from cache, no network hit


def test_revalidates_with_if_none_match_after_expiry() -> None:
    seen_inm: list[str | None] = []
    clock = _Clock(datetime(2020, 1, 1, tzinfo=UTC))
    soon = _http_date(clock.now + timedelta(seconds=5))
    later = _http_date(clock.now + timedelta(minutes=10))

    def handler(request: httpx.Request) -> httpx.Response:
        inm = request.headers.get("If-None-Match")
        seen_inm.append(inm)
        if inm == "v1":
            return httpx.Response(304, headers={"Expires": later, "ETag": "v1"})
        return httpx.Response(200, headers={"Expires": soon, "ETag": "v1"}, json=[42])

    async def body(client: EsiClient) -> None:
        first = await client.get("/x/")
        clock.advance(30)  # past the first Expires
        second = await client.get("/x/")
        assert first == second  # 304 keeps the cached body

    _run(handler, body, now=clock)
    assert seen_inm == [None, "v1"]


def test_get_all_pages_follows_x_pages() -> None:
    pages_seen: list[str | None] = []
    future = _http_date(datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=5))

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        pages_seen.append(page)
        return httpx.Response(200, headers={"X-Pages": "3", "Expires": future}, json=[page])

    async def body(client: EsiClient) -> None:
        bodies = await client.get_all_pages("/markets/10000002/orders/")
        # Pages are fetched concurrently but returned in page order.
        assert [json.loads(body) for body in bodies] == [["1"], ["2"], ["3"]]

    _run(handler, body)
    assert sorted(filter(None, pages_seen)) == ["1", "2", "3"]


def test_backs_off_when_error_budget_is_low() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Esi-Error-Limit-Remain": "1", "X-Esi-Error-Limit-Reset": "30"},
            json=[],
        )

    async def body(client: EsiClient) -> None:
        await client.get("/a/")  # observes remain=1, reset=30
        await client.get("/a/")  # low budget -> back off before firing

    _run(handler, body, sleep=fake_sleep)
    assert sleeps == [30.0]


def test_sends_descriptive_user_agent_with_contact() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json=[])

    async def body(client: EsiClient) -> None:
        await client.get("/a/")

    _run(handler, body)
    ua = seen["ua"]
    assert ua is not None and ua.startswith("evetrader/") and "contact@example.com" in ua


def test_bearer_token_is_attached_when_provided() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    async def body(client: EsiClient) -> None:
        await client.get("/characters/1/wallet/", token="tok123")

    _run(handler, body)
    assert seen["auth"] == "Bearer tok123"


def test_non_success_raises_esi_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    async def body(client: EsiClient) -> None:
        with pytest.raises(EsiError) as excinfo:
            await client.get("/boom/")
        assert excinfo.value.status_code == 500

    _run(handler, body)


def test_cache_persists_between_sessions(tmp_path: Path) -> None:
    calls = 0
    clock = _Clock(datetime(2020, 1, 1, tzinfo=UTC))
    cache_file = tmp_path / "esi_cache.pickle"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.headers.get("If-None-Match") == "e1":
            calls += 1
            return httpx.Response(304, headers={"Expires": _http_date(clock.now + timedelta(minutes=5))})
        calls += 1
        expires = _http_date(clock.now + timedelta(minutes=5))
        return httpx.Response(200, json={"n": calls}, headers={"Expires": expires, "ETag": "e1"})

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            # Session 1: one real fetch, then persist the cache.
            first = EsiClient(_config(), http, now=clock, cache_path=cache_file)
            body1 = await first.get("/x/")
            first.save_cache()
            assert calls == 1
            assert cache_file.exists()

            # Session 2 (new client), still within Expires -> served from disk, no network.
            second = EsiClient(_config(), http, now=clock, cache_path=cache_file)
            assert await second.get("/x/") == body1
            assert calls == 1

            # Session 3 after Expires passes -> revalidates with the persisted ETag; a 304
            # reuses the persisted body without re-downloading it.
            clock.advance(600)
            third = EsiClient(_config(), http, now=clock, cache_path=cache_file)
            assert await third.get("/x/") == body1
            assert calls == 2  # one conditional request, no full re-fetch

    asyncio.run(go())


def test_region_order_book_is_not_persisted(tmp_path: Path) -> None:
    clock = _Clock(datetime(2020, 1, 1, tzinfo=UTC))
    cache_file = tmp_path / "esi_cache.pickle"

    def handler(_: httpx.Request) -> httpx.Response:
        expires = _http_date(clock.now + timedelta(minutes=5))
        return httpx.Response(200, json=[1], headers={"Expires": expires, "ETag": "e"})

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http, now=clock, cache_path=cache_file)
            # The huge, short-lived region order book is served from the in-memory cache...
            await client.get("/markets/10000002/orders/", params={"order_type": "all"})
            assert await client.get("/markets/10000002/orders/", params={"order_type": "all"}) == (
                b"[1]"  # second read within Expires -> no network, so it is cached in memory
            )
            await client.get("/universe/stations/60003760/")
            client.save_cache()

        with cache_file.open("rb") as handle:
            _version, entries = pickle.load(handle)
        paths = {urlsplit(url).path for url in entries}
        # ...but it is left out of the on-disk cache; the long-lived station stays.
        assert "/latest/markets/10000002/orders/" not in paths
        assert "/latest/universe/stations/60003760/" in paths

    asyncio.run(go())


def test_missing_or_corrupt_cache_starts_cold(tmp_path: Path) -> None:
    corrupt = tmp_path / "esi_cache.pickle"
    corrupt.write_bytes(b"not a pickle")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http, cache_path=corrupt)  # corrupt -> empty, no crash
            assert await client.get("/x/") == b'{"ok":true}'
            client.save_cache()  # overwrites the corrupt file cleanly

    asyncio.run(go())
