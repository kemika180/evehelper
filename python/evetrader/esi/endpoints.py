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
    AssetName,
    Blueprint,
    CharacterOrder,
    CharacterSkills,
    Corporation,
    EsiName,
    IndustryJob,
    Location,
    MarketHistoryDay,
    MarketOrder,
    SkillQueueEntry,
    Standing,
    Station,
    Structure,
)

_WALLET = TypeAdapter(float)
_ASSETS = TypeAdapter(list[Asset])
_ASSET_NAMES = TypeAdapter(list[AssetName])
_BLUEPRINTS = TypeAdapter(list[Blueprint])
_INDUSTRY_JOBS = TypeAdapter(list[IndustryJob])
_CHARACTER_ORDERS = TypeAdapter(list[CharacterOrder])
_MARKET_ORDERS = TypeAdapter(list[MarketOrder])
_MARKET_HISTORY = TypeAdapter(list[MarketHistoryDay])
_NAMES = TypeAdapter(list[EsiName])
_STANDINGS = TypeAdapter(list[Standing])
_SKILLQUEUE = TypeAdapter(list[SkillQueueEntry])


async def fetch_wallet_balance(client: EsiClient, character_id: int, token: str) -> float:
    body = await client.get(f"/characters/{character_id}/wallet/", token=token)
    return _WALLET.validate_json(body)


async def fetch_assets(client: EsiClient, character_id: int, token: str) -> list[Asset]:
    pages = await client.get_all_pages(f"/characters/{character_id}/assets/", token=token)
    return [asset for page in pages for asset in _ASSETS.validate_json(page)]


async def fetch_asset_names(
    client: EsiClient, character_id: int, token: str, item_ids: Sequence[int]
) -> list[AssetName]:
    """Assigned names for singleton items (ships/containers), up to 1000 ids per call."""
    body = await client.post_json(
        f"/characters/{character_id}/assets/names/", body=list(item_ids), token=token
    )
    return _ASSET_NAMES.validate_json(body)


async def fetch_blueprints(
    client: EsiClient, character_id: int, token: str
) -> list[Blueprint]:
    """The character's blueprints (ME/TE, runs, original vs copy), paged like assets."""
    pages = await client.get_all_pages(f"/characters/{character_id}/blueprints/", token=token)
    return [bp for page in pages for bp in _BLUEPRINTS.validate_json(page)]


async def fetch_industry_jobs(
    client: EsiClient, character_id: int, token: str
) -> list[IndustryJob]:
    """Running/ready industry jobs (not the delivered/cancelled history — the default,
    no ``include_completed``). A single unpaged response."""
    body = await client.get(f"/characters/{character_id}/industry/jobs/", token=token)
    return _INDUSTRY_JOBS.validate_json(body)


async def fetch_open_orders(
    client: EsiClient, character_id: int, token: str
) -> list[CharacterOrder]:
    body = await client.get(f"/characters/{character_id}/orders/", token=token)
    return _CHARACTER_ORDERS.validate_json(body)


async def fetch_location(client: EsiClient, character_id: int, token: str) -> Location:
    body = await client.get(f"/characters/{character_id}/location/", token=token)
    return Location.model_validate_json(body)


async def fetch_market_orders(
    client: EsiClient, region_id: int, type_id: int | None = None
) -> list[MarketOrder]:
    # Filtering by type keeps this to a handful of pages; a whole region is ~400.
    params: dict[str, str | int] = {"order_type": "all"}
    if type_id is not None:
        params["type_id"] = type_id
    pages = await client.get_all_pages(f"/markets/{region_id}/orders/", params=params)
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


async def fetch_skillqueue(
    client: EsiClient, character_id: int, token: str
) -> list[SkillQueueEntry]:
    body = await client.get(f"/characters/{character_id}/skillqueue/", token=token)
    return _SKILLQUEUE.validate_json(body)


async def fetch_station(client: EsiClient, station_id: int) -> Station:
    body = await client.get(f"/universe/stations/{station_id}/")
    return Station.model_validate_json(body)


async def fetch_structure(client: EsiClient, structure_id: int, token: str) -> Structure:
    """A player-owned structure's public info; needs docking access (else 403)."""
    body = await client.get(f"/universe/structures/{structure_id}/", token=token)
    return Structure.model_validate_json(body)


async def fetch_corporation(client: EsiClient, corporation_id: int) -> Corporation:
    body = await client.get(f"/corporations/{corporation_id}/")
    return Corporation.model_validate_json(body)


async def resolve_names(client: EsiClient, ids: Sequence[int]) -> list[EsiName]:
    """Resolve up to 1000 ids to names via POST /universe/names/ (single call)."""
    body = await client.post_json("/universe/names/", body=list(ids))
    return _NAMES.validate_json(body)
