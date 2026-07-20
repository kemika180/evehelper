"""End-to-end pipeline over mocked ESI: character + holdings, then buy/sell signals."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evetrader.config import Config, InvestmentParams, RiskPreferences
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient
from evetrader.pipeline import fetch_character, fetch_opportunities

_STATION = 60003760
_REGION = 10000002
_OWNER = 1000035
_FACTION = 500001
_FUTURE = "Wed, 21 Oct 2099 07:28:00 GMT"
_UNDERVALUED = 34  # a cheap buy candidate
_HELD_DEAR = 35  # held, and currently dear


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
        home_region_id=_REGION,
        home_station_id=_STATION,
        total_capital_isk=1_000_000_000.0,
        scan_candidates=50,
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=0.0, max_capital_per_order_isk=100_000_000.0
        ),
        investment=InvestmentParams(window_days=4, buy_below_position=0.3, sell_above_position=0.7),
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


def _order(order_id: int, type_id: int, *, is_buy: bool, price: float) -> dict[str, object]:
    return {
        "order_id": order_id,
        "type_id": type_id,
        "location_id": _STATION,
        "system_id": 30000142,
        "is_buy_order": is_buy,
        "price": price,
        "volume_remain": 1000,
        "volume_total": 1000,
        "min_volume": 1,
        "range": "region",
        "duration": 90,
        "issued": "2020-01-01T00:00:00Z",
    }


def _channel_history() -> list[dict[str, object]]:
    # 4 days with a median ~1000 and a 900-1100 low/high channel.
    return [
        {
            "date": f"2020-01-0{day}",
            "average": average,
            "highest": 1100.0,
            "lowest": 900.0,
            "order_count": 10,
            "volume": 1000,
        }
        for day, average in enumerate([950.0, 1050.0, 950.0, 1050.0], start=1)
    ]


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "login.eveonline.com":
        return httpx.Response(
            200,
            json={"access_token": "atk", "token_type": "Bearer", "expires_in": 1200, "refresh_token": "r1"},
        )
    exp = {"Expires": _FUTURE}
    if "/markets/" in path and path.endswith("/history/"):
        return httpx.Response(200, json=_channel_history(), headers=exp)
    if "/markets/" in path and path.endswith("/orders/"):
        orders = [
            _order(1, _UNDERVALUED, is_buy=False, price=700.0),  # cheap ask
            _order(2, _UNDERVALUED, is_buy=True, price=600.0),
            _order(3, _HELD_DEAR, is_buy=True, price=1300.0),  # dear bid
            _order(4, _HELD_DEAR, is_buy=False, price=1400.0),
        ]
        return httpx.Response(200, json=orders, headers={**exp, "X-Pages": "1"})
    if path.endswith("/assets/"):
        return httpx.Response(
            200,
            json=[
                {
                    "item_id": 1,
                    "type_id": _HELD_DEAR,
                    "quantity": 10,
                    "location_id": _STATION,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "is_singleton": False,
                }
            ],
            headers=exp,
        )
    if path.endswith("/wallet/"):
        return httpx.Response(200, json=5_000_000.0, headers=exp)
    if path.endswith("/skillqueue/"):
        return httpx.Response(200, json=[], headers=exp)
    if path.endswith("/skills/"):
        return httpx.Response(
            200,
            json={
                "skills": [{"skill_id": 3443, "active_skill_level": 5, "trained_skill_level": 5}],
                "total_sp": 10_000_000,
            },
            headers=exp,
        )
    if "/characters/" in path and path.endswith("/orders/"):
        return httpx.Response(200, json=[], headers=exp)
    if path.endswith("/standings/"):
        return httpx.Response(200, json=[], headers=exp)
    if "/universe/stations/" in path:
        return httpx.Response(
            200,
            json={"station_id": _STATION, "name": "Jita", "system_id": 30000142, "type_id": 1529, "owner": _OWNER},
            headers=exp,
        )
    if "/corporations/" in path:
        return httpx.Response(200, json={"name": "Caldari Navy", "faction_id": _FACTION}, headers=exp)
    if "/universe/names/" in path:
        catalogue = {_UNDERVALUED: "Tritanium", _HELD_DEAR: "Pyerite", _STATION: "Jita IV-4"}
        return httpx.Response(
            200, json=[{"id": i, "name": catalogue[i], "category": "x"} for i in json.loads(request.content)]
        )
    raise AssertionError(f"unexpected {path}")


def test_pipeline_produces_buys_and_sells(tmp_path: Path) -> None:
    async def go() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http)
            authenticator = Authenticator(_config(), http, _FakeStore())
            name_cache = NameCache(tmp_path / "names.json", client)
            now = lambda: datetime(2020, 1, 1, tzinfo=UTC)  # noqa: E731

            character = await fetch_character(client, authenticator, _config(), 42, name_cache, now=now)
            assert character.holdings == {_HELD_DEAR: 10}
            assert character.station_name == "Jita IV-4"

            report = await fetch_opportunities(client, _config(), character, name_cache)
            assert [s.type_id for s in report.buys] == [_UNDERVALUED]
            assert [(s.type_id, s.quantity) for s in report.sells] == [(_HELD_DEAR, 10)]
            assert report.names[_UNDERVALUED] == "Tritanium"

    asyncio.run(go())
