"""build_character_state assembles fees (via resolved standings) and free slots."""

import asyncio

import httpx
import pytest

from evetrader.config import Config, RiskPreferences
from evetrader.data.character import build_character_state
from evetrader.esi.client import EsiClient

_STATION = 60003760
_OWNER_CORP = 1000035
_FACTION = 500001


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
        home_region_id=10000002,
        home_station_id=_STATION,
        total_capital_isk=1_000_000.0,
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=1000.0, max_capital_per_order_isk=1_000_000.0
        ),
    )


def _character_order(order_id: int) -> dict[str, object]:
    return {
        "order_id": order_id,
        "type_id": 34,
        "region_id": 10000002,
        "location_id": _STATION,
        "price": 5.0,
        "volume_remain": 10,
        "volume_total": 10,
        "duration": 90,
        "issued": "2020-01-01T00:00:00Z",
        "range": "station",
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/wallet/"):
        return httpx.Response(200, json=5_000_000.0)
    if path.endswith("/skills/"):
        return httpx.Response(
            200,
            json={
                "skills": [
                    {"skill_id": 16622, "active_skill_level": 5, "trained_skill_level": 5},  # Accounting
                    {"skill_id": 3446, "active_skill_level": 4, "trained_skill_level": 4},  # Broker Relations
                    {"skill_id": 3443, "active_skill_level": 5, "trained_skill_level": 5},  # Trade
                ],
                "total_sp": 10_000_000,
            },
        )
    if path.endswith("/orders/"):
        return httpx.Response(200, json=[_character_order(1), _character_order(2)])
    if path.endswith("/standings/"):
        return httpx.Response(
            200,
            json=[
                {"from_id": _OWNER_CORP, "from_type": "npc_corp", "standing": 5.0},
                {"from_id": _FACTION, "from_type": "faction", "standing": 8.0},
            ],
        )
    if "/universe/stations/" in path:
        return httpx.Response(
            200,
            json={
                "station_id": _STATION,
                "name": "Jita IV-4 CNAP",
                "system_id": 30000142,
                "type_id": 1529,
                "owner": _OWNER_CORP,
            },
        )
    if "/corporations/" in path:
        return httpx.Response(200, json={"name": "Caldari Navy", "faction_id": _FACTION})
    raise AssertionError(f"unexpected path {path}")


def test_build_character_state_computes_fees_and_free_slots() -> None:
    async def go() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http)
            state = await build_character_state(client, _config(), character_id=42, token="tok")

            assert state.wallet_balance == 5_000_000.0
            assert state.station_id == _STATION
            # Accounting 5: 0.08 * (1 - 0.55) = 0.036
            assert state.fees.sales_tax == pytest.approx(0.036)
            # Broker: 0.03 - 0.003*4 - 0.0003*8 - 0.0002*5 = 0.0146
            assert state.fees.broker_fee == pytest.approx(0.0146)
            # Trade 5 -> 5 + 4*5 = 25 slots, minus 2 open orders = 23 free
            assert state.free_order_slots == 23
            assert state.trade_skills.accounting == 5

    asyncio.run(go())
