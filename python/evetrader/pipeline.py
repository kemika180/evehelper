"""Composition root: fetch, build the pure inputs, run the advisor. Impure.

Split into two phases so the TUI can render the character and skill queue
immediately, then fill in opportunities after the (slower) market scan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

from evetrader.advisor.engine import rank
from evetrader.advisor.source import Opportunity, StationTradingSource
from evetrader.advisor.state import CharacterState
from evetrader.config import Config
from evetrader.data.character import build_character_state
from evetrader.data.market import build_market_snapshot, orders_frame_from_pages, orders_to_frame
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient
from evetrader.esi.endpoints import fetch_market_history, fetch_market_orders, fetch_skillqueue
from evetrader.esi.models import MarketHistoryDay, MarketOrder, SkillQueueEntry
from evetrader.market.fees import EffectiveFees
from evetrader.market.station_trading import candidate_types


@dataclass(frozen=True)
class CharacterReport:
    """Fast phase: character state and skill queue, rendered immediately."""

    captured_at: datetime
    character: CharacterState
    skill_queue: list[SkillQueueEntry]
    names: dict[int, str]


@dataclass(frozen=True)
class OpportunityReport:
    """Slow phase: ranked opportunities from the market scan."""

    opportunities: list[Opportunity]
    names: dict[int, str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def fetch_character(
    client: EsiClient,
    authenticator: Authenticator,
    config: Config,
    character_id: int,
    name_cache: NameCache,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> CharacterReport:
    """Wallet, skills, standings, fees, and the skill queue — the quick fetches."""
    token = await authenticator.access_token(character_id)
    character = await build_character_state(client, config, character_id, token)
    skill_queue = await fetch_skillqueue(client, character_id, token)
    names = await name_cache.resolve([entry.skill_id for entry in skill_queue])
    return CharacterReport(now(), character, skill_queue, names)


async def _orders_and_types(
    client: EsiClient, config: Config, fees: EffectiveFees
) -> tuple[pl.DataFrame, list[int]]:
    if config.scan_candidates > 0:
        pages = await client.get_all_pages(
            f"/markets/{config.home_region_id}/orders/", params={"order_type": "all"}
        )
        orders = orders_frame_from_pages(pages).filter(
            pl.col("location_id") == config.home_station_id
        )
        candidates = candidate_types(
            orders,
            station_id=config.home_station_id,
            fees=fees,
            min_margin=config.risk.min_margin,
            limit=config.scan_candidates,
        )
        return orders, list(dict.fromkeys([*candidates, *config.watchlist_type_ids]))

    collected: list[MarketOrder] = []
    for type_id in config.watchlist_type_ids:
        collected.extend(await fetch_market_orders(client, config.home_region_id, type_id))
    return orders_to_frame(collected), list(config.watchlist_type_ids)


async def fetch_opportunities(
    client: EsiClient,
    config: Config,
    character: CharacterState,
    name_cache: NameCache,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> OpportunityReport:
    """Scan the market (discovery or watchlist), rank trades, resolve item names."""
    orders, type_ids = await _orders_and_types(client, config, character.fees)
    history: dict[int, list[MarketHistoryDay]] = {
        type_id: await fetch_market_history(client, config.home_region_id, type_id)
        for type_id in type_ids
    }
    snapshot = build_market_snapshot(
        region_id=config.home_region_id,
        captured_at=now(),
        orders=orders,
        history_by_type=history,
    )
    opportunities = rank([StationTradingSource()], snapshot, character, config)
    names = await name_cache.resolve([opportunity.type_id for opportunity in opportunities])
    return OpportunityReport(opportunities, names)
