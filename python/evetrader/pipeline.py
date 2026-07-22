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
from evetrader.data.assets import AssetLocation, build_asset_tree, nameable_item_ids
from evetrader.data.character import build_character_state
from evetrader.data.market import best_ask_prices, history_to_frame, orders_frame_from_pages
from evetrader.data.sde import SdeDatabase
from evetrader.data.skills import SkillReference, load_skills
from evetrader.data.structures import StructureCache
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient, EsiError
from evetrader.esi.endpoints import (
    fetch_asset_names,
    fetch_assets,
    fetch_blueprints,
    fetch_industry_jobs,
    fetch_market_history,
    fetch_skillqueue,
    fetch_skills,
)
from evetrader.esi.models import (
    Blueprint,
    IndustryJob,
    MarketHistoryDay,
    Skill,
    SkillQueueEntry,
)
from evetrader.market.investment import InvestmentSignal, find_opportunities, liquid_types
from evetrader.market.production import BuildAnalysis, analyze_build

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
    # All assets as a nested tree (places -> items -> container/ship contents).
    assets: list[AssetLocation]
    # Player-assigned names for containers/ships, keyed by item_id.
    asset_names: dict[int, str]
    # Blueprint research (ME/TE, runs, original vs copy), keyed by asset item_id.
    blueprints: dict[int, Blueprint]
    # Running/ready industry jobs (manufacturing, research, copying, invention, …).
    industry_jobs: list[IndustryJob]


@dataclass(frozen=True)
class BuildOpportunity:
    """A build-vs-buy result for one owned blueprint, priced against the reference
    market (Jita). Per single run, so two copies of a type with different ME rank
    separately."""

    blueprint_item_id: int
    blueprint_type_id: int
    product_type_id: int
    material_efficiency: int
    analysis: BuildAnalysis


@dataclass(frozen=True)
class OpportunityReport:
    """Slow phase: value suggestions split into buys and sells of holdings."""

    buys: list[InvestmentSignal]
    sells: list[InvestmentSignal]
    names: dict[int, str]
    # Daily history for the signalled types, retained so a selected row can be
    # plotted without a fresh (keystroke-triggered) ESI call.
    history: dict[int, list[MarketHistoryDay]]
    # Build-vs-buy for owned blueprints, ranked by margin (empty if the SDE isn't
    # installed or no owned blueprint is manufacturable).
    builds: list[BuildOpportunity]
    # Whether the local SDE was available this run — lets the UI explain an empty
    # Manufacturing tab (needs `evetrader sde`) instead of leaving it blank.
    sde_available: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _resolvable_location(location_id: int) -> bool:
    """Whether ``/universe/names`` can name this place. Player structures (Keepstars
    etc.) cannot be, and one unresolvable id 404s the whole batch — so exclude them;
    they fall back to a config label or their id at the display layer."""
    return (
        10_000_000 <= location_id < 11_000_000  # regions
        or 30_000_000 <= location_id < 32_000_000  # solar systems
        or 60_000_000 <= location_id < 64_000_000  # NPC stations
    )


async def fetch_character(
    client: EsiClient,
    authenticator: Authenticator,
    config: Config,
    character_id: int,
    home: HomeMarket,
    name_cache: NameCache,
    structure_cache: StructureCache,
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
    asset_tree = build_asset_tree(assets)

    # Blueprint research is per-item (ME/TE/runs differ between two copies of one
    # type), so key it by item_id to match a selected asset row in the browser.
    blueprints = {bp.item_id: bp for bp in await fetch_blueprints(client, character_id, token)}

    industry_jobs = await fetch_industry_jobs(client, character_id, token)

    # Player-assigned names for containers/ships (POST, 1000 ids/call), to make them
    # findable in the browser. Only singleton items that hold things can be named.
    nameable = nameable_item_ids(asset_tree)
    asset_names: dict[int, str] = {}
    for start in range(0, len(nameable), 1000):
        chunk = nameable[start : start + 1000]
        for named in await fetch_asset_names(client, character_id, token, chunk):
            if named.name and named.name != "None":
                asset_names[named.item_id] = named.name

    # Places to name: asset locations plus each industry job's facility. Player
    # structures don't resolve via /universe/names; look them up individually (needs
    # docking access) — except the home, which a config label already names.
    place_ids = {loc.location_id for loc in asset_tree} | {job.facility_id for job in industry_jobs}
    structure_ids = [
        place_id
        for place_id in place_ids
        if not _resolvable_location(place_id) and place_id != home.station_id
    ]
    structures = await structure_cache.resolve(token, structure_ids)

    # Names back up the bundled reference for any skill it doesn't cover, and label
    # asset/job types and their (resolvable) places plus each structure's solar system.
    name_ids = [entry.skill_id for entry in skill_queue]
    name_ids += [skill.skill_id for skill in skills.skills]
    name_ids += [asset.type_id for asset in assets]
    name_ids += [job.blueprint_type_id for job in industry_jobs]
    name_ids += [job.product_type_id for job in industry_jobs if job.product_type_id is not None]
    name_ids += [place_id for place_id in place_ids if _resolvable_location(place_id)]
    name_ids += [structure.solar_system_id for structure in structures.values()]
    if home.label is None:
        name_ids.append(home.station_id)
    names = await name_cache.resolve(name_ids)
    for structure_id, structure in structures.items():
        system = names.get(structure.solar_system_id)
        names[structure_id] = f"{structure.name} · {system}" if system else structure.name
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
        asset_tree,
        asset_names,
        blueprints,
        industry_jobs,
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


async def _reference_ask_prices(
    client: EsiClient, reference: HomeMarket, type_ids: set[int]
) -> dict[int, float]:
    """Lowest sell price at the reference market (Jita) for each wanted type. Fetches
    only the sell side of the reference region's book (asks) — half the pages."""
    if not type_ids:
        return {}
    pages = await client.get_all_pages(
        f"/markets/{reference.region_id}/orders/", params={"order_type": "sell"}
    )
    asks = best_ask_prices(orders_frame_from_pages(pages), reference.station_id)
    return {type_id: asks[type_id] for type_id in type_ids if type_id in asks}


async def _build_opportunities(
    client: EsiClient, config: Config, character: CharacterReport, sde: SdeDatabase
) -> list[BuildOpportunity]:
    """Build-vs-buy for each owned, manufacturable blueprint, priced at the reference
    market. Per single run; ranked by margin (best first)."""
    recipes = [
        (item_id, blueprint, recipe)
        for item_id, blueprint in character.blueprints.items()
        if (recipe := sde.manufacturing_recipe(blueprint.type_id)) is not None
    ]
    if not recipes:
        return []

    wanted = {recipe.product_type_id for _, _, recipe in recipes}
    wanted |= {material.type_id for _, _, recipe in recipes for material in recipe.materials}
    ask = await _reference_ask_prices(client, config.reference_market, wanted)

    fees = character.character.fees  # home-station fees; sales tax is skill-based, broker approx
    builds: list[BuildOpportunity] = []
    for item_id, blueprint, recipe in recipes:
        # A product with no Jita sell order (a thin market — e.g. a Black Ops hull) is
        # kept as an unvalued row, not dropped, so every manufacturable blueprint shows.
        product_price = ask.get(recipe.product_type_id)
        material_prices = {m.type_id: ask[m.type_id] for m in recipe.materials if m.type_id in ask}
        analysis = analyze_build(
            recipe,
            material_efficiency=blueprint.material_efficiency,
            material_prices=material_prices,
            product_price=product_price,
            sales_tax=fees.sales_tax,
            broker_fee=fees.broker_fee,
        )
        builds.append(
            BuildOpportunity(
                blueprint_item_id=item_id,
                blueprint_type_id=blueprint.type_id,
                product_type_id=recipe.product_type_id,
                material_efficiency=blueprint.material_efficiency,
                analysis=analysis,
            )
        )
    builds.sort(key=lambda build: build.analysis.margin, reverse=True)
    return builds


async def fetch_opportunities(
    client: EsiClient,
    config: Config,
    character: CharacterReport,
    home: HomeMarket,
    name_cache: NameCache,
    sde: SdeDatabase | None = None,
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
    builds = await _build_opportunities(client, config, character, sde) if sde is not None else []

    buys = [signal for signal in signals if signal.action == "BUY"]
    sells = [signal for signal in signals if signal.action == "SELL"]
    name_ids = [signal.type_id for signal in signals]
    name_ids += [build.product_type_id for build in builds]
    # Material type ids too, so a build's bill-of-materials view can name each input.
    name_ids += [line.type_id for build in builds for line in build.analysis.materials]
    names = await name_cache.resolve(name_ids)
    signalled = {signal.type_id for signal in signals}
    window = config.investment.window_days
    retained = {
        type_id: sorted(history[type_id], key=lambda day: day.date)[-window:]
        for type_id in signalled
        if type_id in history
    }
    return OpportunityReport(
        buys=buys,
        sells=sells,
        names=names,
        history=retained,
        builds=builds,
        sde_available=sde is not None,
    )
