"""End-to-end pipeline over a fully mocked ESI: refresh yields ranked opportunities,
character state, skill queue, and resolved names."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evetrader.config import Config, RiskPreferences
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient
from evetrader.pipeline import refresh

_STATION = 60003760
_REGION = 10000002
_OWNER_CORP = 1000035
_FACTION = 500001
_FUTURE = "Wed, 21 Oct 2099 07:28:00 GMT"


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
        home_region_id=_REGION,
        home_station_id=_STATION,
        total_capital_isk=1_000_000.0,
        watchlist_type_ids=(34,),
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=1000.0, max_capital_per_order_isk=1_000_000.0
        ),
    )


class _FakeStore:
    def __init__(self) -> None:
        self.tokens = {42: "r0"}

    def load(self, character_id: int) -> str | None:
        return self.tokens.get(character_id)

    def save(self, character_id: int, refresh_token: str) -> None:
        self.tokens[character_id] = refresh_token

    def delete(self, character_id: int) -> None:
        self.tokens.pop(character_id, None)


def _market_order(order_id: int, *, is_buy: bool, price: float) -> dict[str, object]:
    return {
        "order_id": order_id,
        "type_id": 34,
        "location_id": _STATION,
        "system_id": 30000142,
        "is_buy_order": is_buy,
        "price": price,
        "volume_remain": 100,
        "volume_total": 100,
        "min_volume": 1,
        "range": "region",
        "duration": 90,
        "issued": "2020-01-01T00:00:00Z",
    }


def _handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "login.eveonline.com":
        return httpx.Response(
            200,
            json={
                "access_token": "atk",
                "token_type": "Bearer",
                "expires_in": 1200,
                "refresh_token": "r1",
            },
        )
    exp = {"Expires": _FUTURE}
    if "/markets/" in path and path.endswith("/history/"):
        return httpx.Response(
            200,
            json=[
                {
                    "date": "2020-01-01",
                    "average": 120.0,
                    "highest": 121.0,
                    "lowest": 119.0,
                    "order_count": 10,
                    "volume": 1000,
                }
            ],
            headers=exp,
        )
    if "/markets/" in path and path.endswith("/orders/"):
        return httpx.Response(
            200,
            json=[_market_order(1, is_buy=True, price=100.0), _market_order(2, is_buy=False, price=150.0)],
            headers={**exp, "X-Pages": "1"},
        )
    if path.endswith("/wallet/"):
        return httpx.Response(200, json=5_000_000.0, headers=exp)
    if path.endswith("/skillqueue/"):
        return httpx.Response(
            200,
            json=[{"skill_id": 16622, "finished_level": 5, "queue_position": 0}],
            headers=exp,
        )
    if path.endswith("/skills/"):
        return httpx.Response(
            200,
            json={
                "skills": [
                    {"skill_id": 16622, "active_skill_level": 5, "trained_skill_level": 5},
                    {"skill_id": 3446, "active_skill_level": 5, "trained_skill_level": 5},
                    {"skill_id": 3443, "active_skill_level": 5, "trained_skill_level": 5},
                ],
                "total_sp": 10_000_000,
            },
            headers=exp,
        )
    if "/characters/" in path and path.endswith("/orders/"):
        return httpx.Response(200, json=[], headers=exp)
    if path.endswith("/standings/"):
        return httpx.Response(
            200,
            json=[{"from_id": _FACTION, "from_type": "faction", "standing": 8.0}],
            headers=exp,
        )
    if "/universe/stations/" in path:
        return httpx.Response(
            200,
            json={"station_id": _STATION, "name": "Jita", "system_id": 30000142, "type_id": 1529, "owner": _OWNER_CORP},
            headers=exp,
        )
    if "/corporations/" in path:
        return httpx.Response(200, json={"name": "Caldari Navy", "faction_id": _FACTION}, headers=exp)
    if "/universe/names/" in path:
        import json

        ids = json.loads(request.content)
        catalogue = {34: "Tritanium", 16622: "Accounting"}
        return httpx.Response(200, json=[{"id": i, "name": catalogue[i], "category": "x"} for i in ids])
    raise AssertionError(f"unexpected {path}")


def test_refresh_produces_full_report(tmp_path: Path) -> None:
    async def go() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http)
            authenticator = Authenticator(_config(), http, _FakeStore())
            name_cache = NameCache(tmp_path / "names.json", client)

            report = await refresh(
                client,
                authenticator,
                _config(),
                character_id=42,
                name_cache=name_cache,
                now=lambda: datetime(2020, 1, 1, tzinfo=UTC),
            )

            assert len(report.opportunities) == 1
            assert report.opportunities[0].type_id == 34
            assert report.names[34] == "Tritanium"
            assert report.names[16622] == "Accounting"
            assert report.skill_queue[0].skill_id == 16622
            assert report.character.free_order_slots == 25  # Trade 5 -> 25, no open orders
            assert report.character.fees.sales_tax > 0.0

    asyncio.run(go())
