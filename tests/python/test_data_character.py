"""build_character_state assembles the wallet, trade skills, and free order slots."""

import asyncio

import httpx

from evehelper.config import Config
from evehelper.data.character import build_character_state
from evehelper.esi.client import EsiClient
from evehelper.esi.models import CharacterOrder, CharacterSkills

_STATION = 60003760


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
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
    raise AssertionError(f"unexpected path {path}")


_SKILLS = CharacterSkills.model_validate(
    {
        "skills": [
            {"skill_id": 3443, "active_skill_level": 5, "trained_skill_level": 5},  # Trade
        ],
        "total_sp": 10_000_000,
    }
)


def test_build_character_state_computes_free_slots() -> None:
    async def go() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http)
            open_orders = [
                CharacterOrder.model_validate(_character_order(1)),
                CharacterOrder.model_validate(_character_order(2)),
            ]
            state = await build_character_state(
                client, _config(), 42, "tok", _SKILLS, open_orders
            )

            assert state.wallet_balance == 5_000_000.0
            # Trade 5 -> 5 + 4*5 = 25 slots, minus 2 open orders = 23 free
            assert state.free_order_slots == 23
            assert state.trade_skills.trade == 5

    asyncio.run(go())
