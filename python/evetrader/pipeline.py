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
from evetrader.config import Config, HomeMarket
from evetrader.data.character import build_character_state
from evetrader.data.market import history_to_frame, orders_frame_from_pages
from evetrader.data.skills import SkillReference, load_skills
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient, EsiError
from evetrader.esi.endpoints import (
    fetch_assets,
    fetch_market_history,
    fetch_skillqueue,
    fetch_skills,
)
from evetrader.esi.models import MarketHistoryDay, Skill, SkillQueueEntry
from evetrader.market.investment import InvestmentSignal, find_opportunities, liquid_types

# Bounded concurrency for the per-type history fetches.
_MAX_CONCURRENT_HISTORY = 8


@dataclass(frozen=True)
class CharacterReport:
    """Fast phase: character state, skill queue, and current holdings."""

    captured_at: datetime
    character: CharacterState
    skill_queue: list[SkillQueueEntry]
    # The character's trained skills (id + trained level), for the full skill view.
    skills: list[Skill]
    holdings: dict[int, int]
    names: dict[int, str]
    station_name: str
    # Static skill facts (name/group/rank/attributes/description) for the skill views.
    skill_reference: dict[int, SkillReference]


@dataclass(frozen=True)
class OpportunityReport:
    """Slow phase: value suggestions split into buys and sells of holdings."""

    buys: list[InvestmentSignal]
    sells: list[InvestmentSignal]
    names: dict[int, str]
    # Daily history for the signalled types, retained so a selected row can be
    # plotted without a fresh (keystroke-triggered) ESI call.
    history: dict[int, list[MarketHistoryDay]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def fetch_character(
    client: EsiClient,
    authenticator: Authenticator,
    config: Config,
    character_id: int,
    home: HomeMarket,
    name_cache: NameCache,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> CharacterReport:
    """Wallet, skills, standings, fees, skill queue, and inventory — the quick fetches."""
    token = await authenticator.access_token(character_id)
    skills = await fetch_skills(client, character_id, token)
    character = await build_character_state(
        client, config, character_id, token, home.station_id, skills
    )
    skill_queue = await fetch_skillqueue(client, character_id, token)

    # Only holdings AT the home market are sellable there — don't suggest selling
    # something sitting 20 jumps away.
    assets = await fetch_assets(client, character_id, token)
    holdings: dict[int, int] = {}
    for asset in assets:
        if asset.location_id == home.station_id:
            holdings[asset.type_id] = holdings.get(asset.type_id, 0) + asset.quantity

    # Structures don't resolve via /universe/names — a config label names them.
    # Names back up the bundled reference for any skill it doesn't cover.
    name_ids = [entry.skill_id for entry in skill_queue]
    name_ids += [skill.skill_id for skill in skills.skills]
    if home.label is None:
        name_ids.append(home.station_id)
    names = await name_cache.resolve(name_ids)
    station_name = home.label or names.get(home.station_id, str(home.station_id))
    return CharacterReport(
        now(),
        character,
        skill_queue,
        skills.skills,
        holdings,
        names,
        station_name,
        load_skills(),
    )


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
    home: HomeMarket,
    name_cache: NameCache,
) -> OpportunityReport:
    """Scan the home market, then find undervalued buys and overvalued holdings."""
    pages = await client.get_all_pages(
        f"/markets/{home.region_id}/orders/", params={"order_type": "all"}
    )
    orders = orders_frame_from_pages(pages).filter(pl.col("location_id") == home.station_id)

    # Only pull history for liquid candidates and holdings that actually trade here —
    # fetching non-market types would 400 and burn the error-limit budget.
    traded = set(orders["type_id"].to_list())
    candidates = liquid_types(orders, home.station_id, config.scan_candidates)
    sellable = [type_id for type_id in character.holdings if type_id in traded]
    history = await _histories(client, home.region_id, list({*candidates, *sellable}))

    signals = find_opportunities(
        orders=orders,
        history=history_to_frame(history),
        station_id=home.station_id,
        holdings=character.holdings,
        fees=character.character.fees,
        window=config.investment.window_days,
        buy_position=config.investment.buy_below_position,
        sell_position=config.investment.sell_above_position,
        trend_days=config.investment.trend_days,
        max_downtrend=config.investment.max_downtrend,
        min_daily_isk_volume=config.risk.min_daily_isk_volume,
        max_capital_per_item=config.risk.max_capital_per_order_isk,
    )
    buys = [signal for signal in signals if signal.action == "BUY"]
    sells = [signal for signal in signals if signal.action == "SELL"]
    names = await name_cache.resolve([signal.type_id for signal in signals])
    signalled = {signal.type_id for signal in signals}
    window = config.investment.window_days
    retained = {
        type_id: sorted(history[type_id], key=lambda day: day.date)[-window:]
        for type_id in signalled
        if type_id in history
    }
    return OpportunityReport(buys=buys, sells=sells, names=names, history=retained)
