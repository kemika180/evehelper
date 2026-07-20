"""Composition root: fetch, build the pure inputs, run the advisor. Impure.

Ties the I/O layer to the pure core in one place. The TUI calls `refresh` on an
interval; the client's Expires-cache means only genuinely stale resources hit the
network, so re-running this often is cheap and ToS-safe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from evetrader.advisor.engine import rank
from evetrader.advisor.source import Opportunity, StationTradingSource
from evetrader.advisor.state import CharacterState
from evetrader.config import Config
from evetrader.data.character import build_character_state
from evetrader.data.market import build_market_snapshot, orders_to_frame
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient
from evetrader.esi.endpoints import (
    fetch_market_history,
    fetch_market_orders,
    fetch_skillqueue,
)
from evetrader.esi.models import MarketHistoryDay, MarketOrder, SkillQueueEntry
from evetrader.market.fees import EffectiveFees
from evetrader.market.station_trading import candidate_types


@dataclass(frozen=True)
class AdvisorReport:
    """Everything the TUI renders for one refresh."""

    captured_at: datetime
    character: CharacterState
    opportunities: list[Opportunity]
    skill_queue: list[SkillQueueEntry]
    names: dict[int, str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _orders_and_candidates(
    client: EsiClient, config: Config, fees: EffectiveFees
) -> tuple[list[MarketOrder], list[int]]:
    """The station order book plus the type ids to analyse.

    Discovery mode pulls the whole region book (slow first run, then cached) and
    picks the best-spread items; watchlist mode fetches only the configured types.
    """
    if config.scan_candidates > 0:
        region_orders = await fetch_market_orders(client, config.home_region_id)
        station_orders = [
            order for order in region_orders if order.location_id == config.home_station_id
        ]
        candidates = candidate_types(
            orders_to_frame(station_orders),
            station_id=config.home_station_id,
            fees=fees,
            min_margin=config.risk.min_margin,
            limit=config.scan_candidates,
        )
        return station_orders, list(dict.fromkeys([*candidates, *config.watchlist_type_ids]))

    orders: list[MarketOrder] = []
    for type_id in config.watchlist_type_ids:
        orders.extend(await fetch_market_orders(client, config.home_region_id, type_id))
    return orders, list(config.watchlist_type_ids)


async def refresh(
    client: EsiClient,
    authenticator: Authenticator,
    config: Config,
    character_id: int,
    name_cache: NameCache,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> AdvisorReport:
    """Fetch live state, run the advisor, and resolve display names."""
    token = await authenticator.access_token(character_id)
    character = await build_character_state(client, config, character_id, token)

    orders, type_ids = await _orders_and_candidates(client, config, character.fees)

    # History (for ISK/hr and liquidity) only for the candidate + watchlist types.
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

    skill_queue = await fetch_skillqueue(client, character_id, token)

    opportunities = rank([StationTradingSource()], snapshot, character, config)

    name_ids = [opportunity.type_id for opportunity in opportunities]
    name_ids.extend(entry.skill_id for entry in skill_queue)
    names = await name_cache.resolve(name_ids)

    return AdvisorReport(
        captured_at=now(),
        character=character,
        opportunities=opportunities,
        skill_queue=skill_queue,
        names=names,
    )
