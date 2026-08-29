"""StructureCache resolves accessible structures and never re-asks about 403s."""

import asyncio

import httpx

from evehelper.config import Config
from evehelper.data.structures import StructureCache
from evehelper.esi.client import EsiClient

_OK = 1_035_660_376_235  # a structure the character can dock at
_FORBIDDEN = 1_040_000_000_001  # one it cannot


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
    )


def test_resolves_accessible_and_negative_caches_forbidden() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith(f"/{_OK}/"):
            return httpx.Response(200, json={"name": "V-3YG7 Fortizar", "solar_system_id": 30004759})
        return httpx.Response(403, json={"error": "forbidden"})

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            cache = StructureCache(EsiClient(_config(), http))

            first = await cache.resolve("tok", [_OK, _FORBIDDEN])
            assert set(first) == {_OK}
            assert first[_OK].name == "V-3YG7 Fortizar"
            assert first[_OK].solar_system_id == 30004759
            assert len(calls) == 2  # both were tried

            second = await cache.resolve("tok", [_OK, _FORBIDDEN])
            assert set(second) == {_OK}
            # The forbidden one is remembered and not retried; only the accessible
            # structure is asked about again.
            assert len(calls) == 3

    asyncio.run(go())
