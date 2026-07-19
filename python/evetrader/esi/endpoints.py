"""Typed fetches for the milestone-2 endpoints: bytes in from the client, pydantic
models out. Every payload is parsed with a TypeAdapter so nothing dynamic (``Any``)
leaks past this boundary into the rest of the app.

Character endpoints need a bearer ``token`` (from the Authenticator); the public
market endpoint does not.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import TypeAdapter

from evetrader.esi.client import EsiClient
from evetrader.esi.models import (
    Asset,
    CharacterOrder,
    CharacterSkills,
    EsiName,
    Location,
    MarketHistoryDay,
    MarketOrder,
    Standing,
)

_WALLET = TypeAdapter(float)
_ASSETS = TypeAdapter(list[Asset])
_CHARACTER_ORDERS = TypeAdapter(list[CharacterOrder])
_MARKET_ORDERS = TypeAdapter(list[MarketOrder])
_MARKET_HISTORY = TypeAdapter(list[MarketHistoryDay])
_NAMES = TypeAdapter(list[EsiName])
_STANDINGS = TypeAdapter(list[Standing])


async def fetch_wallet_balance(client: EsiClient, character_id: int, token: str) -> float:
    body = await client.get(f"/characters/{character_id}/wallet/", token=token)
    return _WALLET.validate_json(body)


async def fetch_assets(client: EsiClient, character_id: int, token: str) -> list[Asset]:
    pages = await client.get_all_pages(f"/characters/{character_id}/assets/", token=token)
    return [asset for page in pages for asset in _ASSETS.validate_json(page)]


async def fetch_open_orders(
    client: EsiClient, character_id: int, token: str
) -> list[CharacterOrder]:
    body = await client.get(f"/characters/{character_id}/orders/", token=token)
    return _CHARACTER_ORDERS.validate_json(body)


async def fetch_location(client: EsiClient, character_id: int, token: str) -> Location:
    body = await client.get(f"/characters/{character_id}/location/", token=token)
    return Location.model_validate_json(body)


async def fetch_market_orders(client: EsiClient, region_id: int) -> list[MarketOrder]:
    pages = await client.get_all_pages(f"/markets/{region_id}/orders/")
    return [order for page in pages for order in _MARKET_ORDERS.validate_json(page)]


async def fetch_market_history(
    client: EsiClient, region_id: int, type_id: int
) -> list[MarketHistoryDay]:
    body = await client.get(f"/markets/{region_id}/history/", params={"type_id": type_id})
    return _MARKET_HISTORY.validate_json(body)


async def fetch_skills(client: EsiClient, character_id: int, token: str) -> CharacterSkills:
    body = await client.get(f"/characters/{character_id}/skills/", token=token)
    return CharacterSkills.model_validate_json(body)


async def fetch_standings(client: EsiClient, character_id: int, token: str) -> list[Standing]:
    body = await client.get(f"/characters/{character_id}/standings/", token=token)
    return _STANDINGS.validate_json(body)


async def resolve_names(client: EsiClient, ids: Sequence[int]) -> list[EsiName]:
    """Resolve up to 1000 ids to names via POST /universe/names/ (single call)."""
    body = await client.post_json("/universe/names/", body=list(ids))
    return _NAMES.validate_json(body)
