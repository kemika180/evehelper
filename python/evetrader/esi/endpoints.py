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
    CharacterAffiliation,
    CharacterAttributes,
    CharacterOrder,
    CharacterShip,
    CharacterSkills,
    EsiName,
    IndustryJob,
    Location,
    MarketPrice,
    SkillQueueEntry,
    Structure,
    WalletTransaction,
)

_WALLET = TypeAdapter(float)
_ASSETS = TypeAdapter(list[Asset])
_ASSET_NAMES = TypeAdapter(list[AssetName])
_BLUEPRINTS = TypeAdapter(list[Blueprint])
_INDUSTRY_JOBS = TypeAdapter(list[IndustryJob])
_CHARACTER_ORDERS = TypeAdapter(list[CharacterOrder])
_MARKET_PRICES = TypeAdapter(list[MarketPrice])
_NAMES = TypeAdapter(list[EsiName])
_AFFILIATIONS = TypeAdapter(list[CharacterAffiliation])
_SKILLQUEUE = TypeAdapter(list[SkillQueueEntry])
_TRANSACTIONS = TypeAdapter(list[WalletTransaction])


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


async def fetch_transactions(
    client: EsiClient, character_id: int, token: str
) -> list[WalletTransaction]:
    """The character's recent wallet transactions (buys and sells). A single unpaged
    response of the most recent trades; older history sits behind ``from_id``, which the
    Overview digest never needs — it only reports trades since the previous session."""
    body = await client.get(f"/characters/{character_id}/wallet/transactions/", token=token)
    return _TRANSACTIONS.validate_json(body)


async def fetch_open_orders(
    client: EsiClient, character_id: int, token: str
) -> list[CharacterOrder]:
    body = await client.get(f"/characters/{character_id}/orders/", token=token)
    return _CHARACTER_ORDERS.validate_json(body)


async def fetch_location(client: EsiClient, character_id: int, token: str) -> Location:
    body = await client.get(f"/characters/{character_id}/location/", token=token)
    return Location.model_validate_json(body)


async def fetch_ship(client: EsiClient, character_id: int, token: str) -> CharacterShip:
    """The ship the character is currently in (needs the read_ship_type scope)."""
    body = await client.get(f"/characters/{character_id}/ship/", token=token)
    return CharacterShip.model_validate_json(body)


async def fetch_affiliation(client: EsiClient, character_id: int) -> CharacterAffiliation | None:
    """The character's corp/alliance via POST /characters/affiliation/ (public, no token).
    Returns None if the id resolves to nothing."""
    body = await client.post_json("/characters/affiliation/", body=[character_id])
    rows = _AFFILIATIONS.validate_json(body)
    return rows[0] if rows else None


async def fetch_market_prices(client: EsiClient) -> list[MarketPrice]:
    """CCP's daily global reference prices for every type — one cheap unpaged call,
    the source for asset valuation and build costing (see ``MarketPrice``)."""
    body = await client.get("/markets/prices/")
    return _MARKET_PRICES.validate_json(body)


async def fetch_skills(client: EsiClient, character_id: int, token: str) -> CharacterSkills:
    body = await client.get(f"/characters/{character_id}/skills/", token=token)
    return CharacterSkills.model_validate_json(body)


async def fetch_attributes(
    client: EsiClient, character_id: int, token: str
) -> CharacterAttributes:
    body = await client.get(f"/characters/{character_id}/attributes/", token=token)
    return CharacterAttributes.model_validate_json(body)


async def fetch_skillqueue(
    client: EsiClient, character_id: int, token: str
) -> list[SkillQueueEntry]:
    body = await client.get(f"/characters/{character_id}/skillqueue/", token=token)
    return _SKILLQUEUE.validate_json(body)


async def fetch_structure(client: EsiClient, structure_id: int, token: str) -> Structure:
    """A player-owned structure's public info; needs docking access (else 403)."""
    body = await client.get(f"/universe/structures/{structure_id}/", token=token)
    return Structure.model_validate_json(body)


async def resolve_names(client: EsiClient, ids: Sequence[int]) -> list[EsiName]:
    """Resolve up to 1000 ids to names via POST /universe/names/ (single call)."""
    body = await client.post_json("/universe/names/", body=list(ids))
    return _NAMES.validate_json(body)
