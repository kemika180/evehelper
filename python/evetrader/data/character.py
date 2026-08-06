"""Assemble CharacterState from live ESI data. Impure.

Fetches wallet/skills/standings/open-orders, resolves the home station's owning
corp and faction to look up broker-fee standings, computes effective fees, and
derives free order slots. Bridges live ESI into the pure core's CharacterState.

Simplifications (documented, not silent): standings are used raw — the
Connections/Diplomacy skill adjustments to effective standing are not applied yet.
"""

from __future__ import annotations

from evetrader.advisor.state import CharacterState, TradeSkills, total_order_slots
from evetrader.config import Config
from evetrader.esi.client import EsiClient
from evetrader.esi.endpoints import (
    fetch_corporation,
    fetch_standings,
    fetch_station,
    fetch_wallet_balance,
)
from evetrader.esi.models import CharacterOrder, CharacterSkills
from evetrader.market.fees import compute_fees

# EVE skill type ids (stable game constants; sanity-check against live data).
_ACCOUNTING = 16622
_BROKER_RELATIONS = 3446
_TRADE = 3443
_RETAIL = 3444
_WHOLESALE = 16596
_TYCOON = 18580

# NPC station id range; structures (Keepstars etc.) fall outside it and have no NPC
# owner to hold standings against.
_NPC_STATION_RANGE = range(60_000_000, 64_000_000)


def _trade_skills(skills: CharacterSkills) -> TradeSkills:
    levels = {skill.skill_id: skill.active_skill_level for skill in skills.skills}
    return TradeSkills(
        accounting=levels.get(_ACCOUNTING, 0),
        broker_relations=levels.get(_BROKER_RELATIONS, 0),
        trade=levels.get(_TRADE, 0),
        retail=levels.get(_RETAIL, 0),
        wholesale=levels.get(_WHOLESALE, 0),
        tycoon=levels.get(_TYCOON, 0),
    )


async def _resolve_broker_standings(
    client: EsiClient, character_id: int, token: str, station_id: int
) -> tuple[float, float]:
    """Return (faction_standing, corp_standing) toward the station's owner."""
    station = await fetch_station(client, station_id)
    if station.owner is None:
        return 0.0, 0.0

    standings = await fetch_standings(client, character_id, token)
    by_key = {(standing.from_type, standing.from_id): standing.standing for standing in standings}
    corp_standing = by_key.get(("npc_corp", station.owner), 0.0)

    corp = await fetch_corporation(client, station.owner)
    faction_standing = 0.0
    if corp.faction_id is not None:
        faction_standing = by_key.get(("faction", corp.faction_id), 0.0)
    return faction_standing, corp_standing


async def build_character_state(
    client: EsiClient,
    config: Config,
    character_id: int,
    token: str,
    station_id: int,
    skills: CharacterSkills,
    open_orders: list[CharacterOrder],
) -> CharacterState:
    """Fetch character data and build the pure CharacterState for the advisor.

    `skills` and `open_orders` are passed in (already fetched by the caller, which
    also surfaces them to the TUI) so they aren't fetched twice.
    """
    wallet = await fetch_wallet_balance(client, character_id, token)

    if station_id in _NPC_STATION_RANGE:
        faction_standing, corp_standing = await _resolve_broker_standings(
            client, character_id, token, station_id
        )
    else:
        faction_standing, corp_standing = 0.0, 0.0  # structures: no NPC owner standings

    trade_skills = _trade_skills(skills)
    fees = compute_fees(
        accounting_level=trade_skills.accounting,
        broker_relations_level=trade_skills.broker_relations,
        faction_standing=faction_standing,
        corp_standing=corp_standing,
        rates=config.fees,
    )
    free_order_slots = max(0, total_order_slots(trade_skills) - len(open_orders))
    return CharacterState(
        station_id=station_id,
        wallet_balance=wallet,
        fees=fees,
        trade_skills=trade_skills,
        free_order_slots=free_order_slots,
    )
