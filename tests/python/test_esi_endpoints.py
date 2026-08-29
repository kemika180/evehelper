"""Endpoint fetches parse into models, page correctly, and attach auth only where
required (character endpoints authed; public market endpoint not)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
from evehelper.config import Config
from evehelper.esi.client import EsiClient
from evehelper.esi.endpoints import (
    fetch_affiliation,
    fetch_assets,
    fetch_attributes,
    fetch_blueprints,
    fetch_industry_jobs,
    fetch_location,
    fetch_market_prices,
    fetch_open_orders,
    fetch_ship,
    fetch_skillqueue,
    fetch_skills,
    fetch_wallet_balance,
    resolve_names,
)

_FUTURE = format_datetime(datetime(2099, 1, 1, tzinfo=UTC), usegmt=True)


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="contact@example.com",
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


def test_fetch_market_prices_is_public_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets/prices/")
        assert request.headers.get("Authorization") is None  # public, no token
        return httpx.Response(
            200,
            json=[
                {"type_id": 34, "average_price": 5.02, "adjusted_price": 5.44},
                {"type_id": 44992, "adjusted_price": 4_500_000.0},  # no average for some types
            ],
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        prices = await fetch_market_prices(client)
        assert prices[0].average_price == 5.02
        assert prices[1].average_price == 0.0  # absent -> default, so callers can fall back
        assert prices[1].adjusted_price == 4_500_000.0

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


def test_fetch_attributes_authenticates_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/attributes/")
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "charisma": 19,
                "intelligence": 27,
                "memory": 21,
                "perception": 20,
                "willpower": 24,
            },
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        attributes = await fetch_attributes(client, 42, token="tok")
        assert attributes.intelligence == 27
        assert attributes.memory == 21

    _run(body, httpx.MockTransport(handler))


def test_fetch_ship_authenticates_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(
            200,
            json={"ship_type_id": 587, "ship_item_id": 1000000016991, "ship_name": "My Rifter"},
            headers={"Expires": _FUTURE},
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        ship = await fetch_ship(client, 42, token="tok")
        assert ship.ship_type_id == 587
        assert ship.ship_name == "My Rifter"

    _run(body, httpx.MockTransport(handler))


def test_fetch_affiliation_is_public_and_returns_corp_alliance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") is None  # public POST, no token
        return httpx.Response(
            200,
            json=[{"character_id": 42, "corporation_id": 98000001, "alliance_id": 99000001}],
        )

    async def body(client: EsiClient, _: dict[str, str | None]) -> None:
        affiliation = await fetch_affiliation(client, 42)
        assert affiliation is not None
        assert affiliation.corporation_id == 98000001
        assert affiliation.alliance_id == 99000001

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
