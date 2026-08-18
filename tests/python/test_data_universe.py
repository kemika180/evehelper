"""NameCache resolves via ESI once, persists, and reloads without refetching."""

import asyncio
from pathlib import Path

import httpx

from evetrader.config import Config
from evetrader.data.universe import NameCache
from evetrader.esi.client import EsiClient


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="contact@example.com",
    )


_NAMES = {34: "Tritanium", 10000002: "The Forge", 587: "Rifter"}


def test_name_cache_fetches_missing_persists_and_reloads(tmp_path: Path) -> None:
    calls = 0
    path = tmp_path / "names.json"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        import json

        requested = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {"id": i, "name": _NAMES[i], "category": "inventory_type"} for i in requested
            ],
        )

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http)

            first = NameCache(path, client)
            resolved = await first.resolve([34, 10000002])
            assert resolved == {34: "Tritanium", 10000002: "The Forge"}
            assert calls == 1

            # Already cached in memory -> no new call.
            await first.resolve([34])
            assert calls == 1

            # A fresh cache loads from disk; only the genuinely new id is fetched.
            second = NameCache(path, client)
            resolved2 = await second.resolve([34, 10000002, 587])
            assert resolved2[587] == "Rifter"
            assert calls == 2

    asyncio.run(go())
    assert path.exists()
