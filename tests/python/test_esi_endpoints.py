"""Endpoint fetches parse into models, page correctly, and attach auth only where
required (character endpoints authed; public market endpoint not)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from evetrader.config import Config, HomeMarket, RiskPreferences
from evetrader.esi.client import EsiClient
from evetrader.esi.endpoints import (
    fetch_assets,
    fetch_blueprints,
    fetch_corporation,
    fetch_industry_jobs,
    fetch_location,
    fetch_market_history,
    fetch_market_orders,
    fetch_open_orders,
    fetch_skillqueue,
    fetch_skills,
    fetch_standings,
    fetch_station,
    fetch_wallet_balance,
    resolve_names,
)

_FUTURE = format_datetime(datetime(2099, 1, 1, tzinfo=UTC), usegmt=True)


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="contact@example.com",
        default_home=HomeMarket(region_id=10000002, station_id=60003760),
        total_capital_isk=1.0,
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=0.0, max_capital_per_order_isk=1.0
        ),
    )


def _order_json(order_id: int, *, is_buy: bool) -> dict[str, object]:
    return {
        "order_id": order_id,
        "type_id": 34,
        "location_id": 60003760,
        "system_id": 30000142,  # used by MarketOrder
        "region_id": 10000002,  # used by CharacterOrder
        "is_buy_order": is_buy,
        "price": 5.0,
        "volume_remain": 10,
        "volume_total": 10,
        "min_volume": 1,
        "range": "region",
        "duration": 90,
        "issued": "2016-09-03T05:12:25Z",
    }


def _run(body: Callable[[EsiClient, dict[str, str | None]], Awaitable[None]], handler: httpx.MockTransport) -> None:
    async def go() -> None:
        async with httpx.AsyncClient(transport=handler) as http:
            client = EsiClient(_config(), http, now=lambda: datetime(2020, 1, 1, tzinfo=UTC))
            await body(client, seen)

    seen: dict[str, str | None] = {}
    asyncio.run(go())


def test_fetch_wallet_balance_returns_float() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=123456.78, headers={"Expires": _FUTURE})

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        balance = await fetch_wallet_balance(client, 42, token="tok")
        assert balance == 123456.78

    _run(body, httpx.MockTransport(handler))


def test_fetch_open_orders_authenticates_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(
            200, json=[_order_json(1, is_buy=True)], headers={"Expires": _FUTURE}
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        orders = await fetch_open_orders(client, 42, token="tok")
        assert len(orders) == 1
        assert orders[0].is_buy_order is True

    _run(body, httpx.MockTransport(handler))


def test_fetch_assets_concatenates_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        asset = {
            "item_id": 1000 + page,
            "type_id": 34,
            "quantity": page,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "location_type": "station",
            "is_singleton": False,
        }
        return httpx.Response(200, json=[asset], headers={"X-Pages": "3", "Expires": _FUTURE})

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        assets = await fetch_assets(client, 42, token="tok")
        assert [a.quantity for a in assets] == [1, 2, 3]

    _run(body, httpx.MockTransport(handler))


def test_fetch_blueprints_authenticates_and_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        page = int(request.url.params.get("page", "1"))
        blueprint = {
            "item_id": 1000 + page,
            "type_id": 938,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "quantity": -2,
            "material_efficiency": 10,
            "time_efficiency": 20,
            "runs": page,
        }
        return httpx.Response(200, json=[blueprint], headers={"X-Pages": "2", "Expires": _FUTURE})

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        blueprints = await fetch_blueprints(client, 42, token="tok")
        assert [bp.runs for bp in blueprints] == [1, 2]
        assert blueprints[0].material_efficiency == 10
        assert blueprints[0].time_efficiency == 20

    _run(body, httpx.MockTransport(handler))


def test_fetch_industry_jobs_authenticates_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        job = {
            "job_id": 1,
            "activity_id": 1,
            "blueprint_type_id": 938,
            "product_type_id": 587,
            "facility_id": 60003760,
            "runs": 10,
            "status": "active",
            "cost": 1000.0,
            "start_date": "2016-09-03T05:12:25Z",
            "end_date": "2016-09-04T05:12:25Z",
        }
        return httpx.Response(200, json=[job], headers={"Expires": _FUTURE})

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        jobs = await fetch_industry_jobs(client, 42, token="tok")
        assert len(jobs) == 1
        assert jobs[0].activity_id == 1
        assert jobs[0].product_type_id == 587

    _run(body, httpx.MockTransport(handler))


def test_fetch_location_parses_station() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"solar_system_id": 30000142, "station_id": 60003760},
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        location = await fetch_location(client, 42, token="tok")
        assert location.station_id == 60003760

    _run(body, httpx.MockTransport(handler))


def test_fetch_market_orders_is_public_and_paged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") is None  # public endpoint
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200, json=[_order_json(page, is_buy=False)], headers={"X-Pages": "2", "Expires": _FUTURE}
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        orders = await fetch_market_orders(client, 10000002)
        assert [o.order_id for o in orders] == [1, 2]

    _run(body, httpx.MockTransport(handler))


def test_fetch_market_history_passes_type_id_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("type_id") == "34"
        return httpx.Response(
            200,
            json=[
                {
                    "date": "2020-01-01",
                    "average": 5.1,
                    "highest": 5.5,
                    "lowest": 4.9,
                    "order_count": 120,
                    "volume": 1_000_000,
                }
            ],
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        history = await fetch_market_history(client, 10000002, 34)
        assert history[0].volume == 1_000_000

    _run(body, httpx.MockTransport(handler))


def test_fetch_skills_authenticates_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "skills": [
                    {"skill_id": 16622, "active_skill_level": 5, "trained_skill_level": 5}
                ],
                "total_sp": 5_000_000,
            },
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        skills = await fetch_skills(client, 42, token="tok")
        assert skills.skills[0].skill_id == 16622

    _run(body, httpx.MockTransport(handler))


def test_fetch_standings_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"from_id": 1000035, "from_type": "npc_corp", "standing": 3.5}],
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        standings = await fetch_standings(client, 42, token="tok")
        assert standings[0].standing == 3.5

    _run(body, httpx.MockTransport(handler))


def test_fetch_skillqueue_authenticates_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(
            200,
            json=[
                {
                    "skill_id": 16622,
                    "finished_level": 5,
                    "queue_position": 0,
                    "finish_date": "2026-08-01T00:00:00Z",
                }
            ],
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        queue = await fetch_skillqueue(client, 42, token="tok")
        assert queue[0].skill_id == 16622
        assert queue[0].queue_position == 0

    _run(body, httpx.MockTransport(handler))


def test_fetch_station_is_public_and_parses_owner() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") is None
        return httpx.Response(
            200,
            json={
                "station_id": 60003760,
                "name": "Jita IV - Moon 4 - Caldari Navy Assembly Plant",
                "system_id": 30000142,
                "type_id": 1529,
                "owner": 1000035,
            },
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        station = await fetch_station(client, 60003760)
        assert station.owner == 1000035

    _run(body, httpx.MockTransport(handler))


def test_fetch_corporation_parses_faction() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": "Caldari Navy", "faction_id": 500001},
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        corp = await fetch_corporation(client, 1000035)
        assert corp.faction_id == 500001

    _run(body, httpx.MockTransport(handler))


def test_resolve_names_posts_id_list() -> None:
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == [34, 10000002]
        return httpx.Response(
            200,
            json=[
                {"id": 34, "name": "Tritanium", "category": "inventory_type"},
                {"id": 10000002, "name": "The Forge", "category": "region"},
            ],
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        names = await resolve_names(client, [34, 10000002])
        assert {n.id: n.name for n in names} == {34: "Tritanium", 10000002: "The Forge"}

    _run(body, httpx.MockTransport(handler))
