"""Composition root: fetch, build the pure inputs, run the advisor. Impure.

Split into two phases so the TUI renders the character (and holdings) immediately,
then fills in market suggestions after the slower scan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

from evetrader.advisor.state import CharacterState
from evetrader.config import Config
from evetrader.data.character import build_character_state
from evetrader.data.market import history_to_frame, orders_frame_from_pages
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient, EsiError
from evetrader.esi.endpoints import fetch_assets, fetch_market_history, fetch_skillqueue
from evetrader.esi.models import MarketHistoryDay, SkillQueueEntry
from evetrader.market.investment import InvestmentSignal, find_opportunities, liquid_types

# Bounded concurrency for the per-type history fetches.
_MAX_CONCURRENT_HISTORY = 8


@dataclass(frozen=True)
class CharacterReport:
    """Fast phase: character state, skill queue, and current holdings."""

    captured_at: datetime
    character: CharacterState
    skill_queue: list[SkillQueueEntry]
    holdings: dict[int, int]
    names: dict[int, str]
    station_name: str


@dataclass(frozen=True)
class OpportunityReport:
    """Slow phase: value suggestions split into buys and sells of holdings."""

    buys: list[InvestmentSignal]
    sells: list[InvestmentSignal]
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
    """Wallet, skills, standings, fees, skill queue, and inventory — the quick fetches."""
    token = await authenticator.access_token(character_id)
    character = await build_character_state(client, config, character_id, token)
    skill_queue = await fetch_skillqueue(client, character_id, token)
    assets = await fetch_assets(client, character_id, token)
    holdings: dict[int, int] = {}
    for asset in assets:
        holdings[asset.type_id] = holdings.get(asset.type_id, 0) + asset.quantity

    name_ids = [entry.skill_id for entry in skill_queue]
    name_ids.append(config.home_station_id)
    names = await name_cache.resolve(name_ids)
    station_name = names.get(config.home_station_id, str(config.home_station_id))
    return CharacterReport(now(), character, skill_queue, holdings, names, station_name)


async def _histories(
    client: EsiClient, region_id: int, type_ids: list[int]
) -> dict[int, list[MarketHistoryDay]]:
    """Fetch history for each type with bounded concurrency, skipping non-market types."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_HISTORY)

    async def one(type_id: int) -> tuple[int, list[MarketHistoryDay]]:
        async with semaphore:
            try:
                return type_id, await fetch_market_history(client, region_id, type_id)
            except EsiError:
                return type_id, []  # type has no market history

    results = await asyncio.gather(*(one(type_id) for type_id in type_ids))
    return {type_id: days for type_id, days in results if days}


async def fetch_opportunities(
    client: EsiClient,
    config: Config,
    character: CharacterReport,
    name_cache: NameCache,
) -> OpportunityReport:
    """Scan the station book, then find undervalued buys and overvalued holdings."""
    pages = await client.get_all_pages(
        f"/markets/{config.home_region_id}/orders/", params={"order_type": "all"}
    )
    orders = orders_frame_from_pages(pages).filter(
        pl.col("location_id") == config.home_station_id
    )

    # Only pull history for liquid candidates and holdings that actually trade here —
    # fetching non-market types would 400 and burn the error-limit budget.
    traded = set(orders["type_id"].to_list())
    candidates = liquid_types(orders, config.home_station_id, config.scan_candidates)
    sellable = [type_id for type_id in character.holdings if type_id in traded]
    history = await _histories(
        client, config.home_region_id, list({*candidates, *sellable})
    )

    signals = find_opportunities(
        orders=orders,
        history=history_to_frame(history),
        station_id=config.home_station_id,
        holdings=character.holdings,
        fees=character.character.fees,
        window=config.investment.window_days,
        buy_position=config.investment.buy_below_position,
        sell_position=config.investment.sell_above_position,
        min_daily_isk_volume=config.risk.min_daily_isk_volume,
        max_capital_per_item=config.risk.max_capital_per_order_isk,
    )
    buys = [signal for signal in signals if signal.action == "BUY"]
    sells = [signal for signal in signals if signal.action == "SELL"]
    names = await name_cache.resolve([signal.type_id for signal in signals])
    return OpportunityReport(buys=buys, sells=sells, names=names)
