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
from evetrader.data.sde import OreYield, SdeDatabase
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
    fetch_open_orders,
    fetch_skillqueue,
    fetch_skills,
)
from evetrader.esi.models import (
    Blueprint,
    CharacterOrder,
    IndustryJob,
    MarketHistoryDay,
    Skill,
    SkillQueueEntry,
)
from evetrader.market.investment import TrackedStatus, summarize_tracked
from evetrader.market.listings import ListingStatus, OwnOrder, classify_listings
from evetrader.market.production import BuildAnalysis, Recipe, analyze_build

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
    # The character's open market orders (all regions), for the active-listings overlay.
    open_orders: list[CharacterOrder]


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
    """Slow phase: the tracked-item watchlist plus the own-order overlays."""

    # One verdict + trend per tracked item (config.trading_type_ids), in config order.
    tracked: list[TrackedStatus]
    # The character's own open orders, classified best/beaten, split by side.
    listing_buys: list[ListingStatus]
    listing_sells: list[ListingStatus]
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
    open_orders = await fetch_open_orders(client, character_id, token)
    character = await build_character_state(
        client, config, character_id, token, home.station_id, skills, open_orders
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

    # Places to name: asset locations, each industry job's facility, and every station
    # the character has an open order at (the overlay shows where). Player structures
    # don't resolve via /universe/names; look them up individually (needs docking
    # access) — except the home, which a config label already names.
    place_ids = (
        {loc.location_id for loc in asset_tree}
        | {job.facility_id for job in industry_jobs}
        | {order.location_id for order in open_orders}
    )
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
        open_orders,
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


def _expand_recipes(
    sde: SdeDatabase, seed: list[Recipe], owned_blueprint_types: set[int]
) -> dict[int, Recipe]:
    """Product-id → recipe for the seed recipes and every sub-material the character can
    *actually* build — i.e. one it owns the blueprint for — as a transitive closure, so
    a build's inputs can be self-costed.

    A sub-component whose blueprint the character doesn't own is left out: self-building
    it would mean acquiring the blueprint too (a cost this doesn't model), so it's costed
    by buying instead. Materials with no manufacturing recipe are likewise absent.
    Cycle-safe via a visited set; the engine additionally depth-caps the cost recursion."""
    recipes: dict[int, Recipe] = {recipe.product_type_id: recipe for recipe in seed}
    visited: set[int] = set(recipes)
    frontier = [material.type_id for recipe in seed for material in recipe.materials]
    while frontier:
        type_id = frontier.pop()
        if type_id in visited:
            continue
        visited.add(type_id)
        sub = sde.recipe_for_product(type_id)
        if sub is None or sub.blueprint_type_id not in owned_blueprint_types:
            continue
        recipes[type_id] = sub
        frontier.extend(material.type_id for material in sub.materials)
    return recipes


def _refine_prices(
    ore_sources: dict[int, list[OreYield]], ask: dict[int, float], efficiency: float
) -> dict[int, float]:
    """Unit cost of obtaining each mineral by mining-and-refining, cheapest ore wins.

    Naive model (a documented simplification): a mineral's refine cost is the ore's ask
    divided by how much of that mineral one unit of ore yields after efficiency —
    ignoring the *other* minerals the same ore also produces. So an ore is only cheap for
    the mineral it's densest in, which keeps refining honest without needing byproduct
    prices."""
    prices: dict[int, float] = {}
    for mineral_id, sources in ore_sources.items():
        costs = [
            ask[source.ore_type_id] / (source.units_per_ore * efficiency)
            for source in sources
            if source.ore_type_id in ask and source.units_per_ore > 0
        ]
        if costs:
            prices[mineral_id] = min(costs)
    return prices


async def _build_opportunities(
    client: EsiClient, config: Config, character: CharacterReport, sde: SdeDatabase
) -> list[BuildOpportunity]:
    """Craft cost for each owned, manufacturable blueprint, with each input priced at the
    cheapest of buying, self-building, or refining ore into it. Per single run; best
    self-source savings first."""
    recipes = [
        (item_id, blueprint, recipe)
        for item_id, blueprint in character.blueprints.items()
        if (recipe := sde.manufacturing_recipe(blueprint.type_id)) is not None
    ]
    if not recipes:
        return []

    # Expand into every sub-component the character owns a blueprint for. Sub-components
    # without an owned blueprint stay buy-only (self-building them would mean acquiring a
    # blueprint too).
    owned_blueprint_types = {bp.type_id for bp in character.blueprints.values()}
    recipe_map = _expand_recipes(
        sde, [recipe for _, _, recipe in recipes], owned_blueprint_types
    )
    material_types = {product_id for product_id in recipe_map}
    material_types |= {mat.type_id for recipe in recipe_map.values() for mat in recipe.materials}

    # Each material that some asteroid ore reprocesses into can be refined instead of
    # bought; price the whole tree plus those ores from the reference market in one pass.
    ore_sources = sde.ore_sources(material_types)
    ore_ids = {source.ore_type_id for sources in ore_sources.values() for source in sources}
    ask = await _reference_ask_prices(client, config.reference_market, material_types | ore_ids)
    refine = _refine_prices(ore_sources, ask, config.refining.efficiency)

    fees = character.character.fees  # home-station fees; sales tax is skill-based, broker approx
    builds: list[BuildOpportunity] = []
    for item_id, blueprint, recipe in recipes:
        product_price = ask.get(recipe.product_type_id)
        analysis = analyze_build(
            recipe,
            material_efficiency=blueprint.material_efficiency,
            material_prices=ask,
            product_price=product_price,
            sales_tax=fees.sales_tax,
            broker_fee=fees.broker_fee,
            recipes=recipe_map,
            refine_prices=refine,
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
    builds.sort(key=lambda build: build.analysis.savings, reverse=True)
    return builds


async def _classify_own_listings(
    client: EsiClient,
    home: HomeMarket,
    region_orders: pl.DataFrame,
    open_orders: list[CharacterOrder],
) -> list[ListingStatus]:
    """Decide, for each open order, whether it still leads its market. Reuses the
    already-fetched home-region book; for orders in other regions, fetches just that
    order's type there (a few pages), not the whole foreign book."""
    if not open_orders:
        return []
    own = [
        OwnOrder(
            order_id=order.order_id,
            type_id=order.type_id,
            location_id=order.location_id,
            is_buy=order.is_buy_order,
            price=order.price,
            volume_remain=order.volume_remain,
        )
        for order in open_orders
    ]
    home_types = {order.type_id for order in open_orders if order.region_id == home.region_id}
    book = region_orders.filter(pl.col("type_id").is_in(list(home_types)))
    foreign_pairs = sorted(
        {
            (order.region_id, order.type_id)
            for order in open_orders
            if order.region_id != home.region_id
        }
    )
    for region_id, type_id in foreign_pairs:
        pages = await client.get_all_pages(
            f"/markets/{region_id}/orders/", params={"order_type": "all", "type_id": type_id}
        )
        book = pl.concat([book, orders_frame_from_pages(pages)])
    return classify_listings(book, own)


async def _special_market_book(
    client: EsiClient,
    config: Config,
    home: HomeMarket,
    history: dict[int, list[MarketHistoryDay]],
) -> pl.DataFrame:
    """Orders for tracked types that trade on a special region-wide market (PLEX on EVE's
    global market), priced from their own region and relabelled to the home station so the
    station-scoped engine treats them like any other item. Their history is fetched from
    the same region and merged into ``history`` in place — the home region's book holds no
    such orders and, for PLEX, only stale legacy history.

    Returns an empty frame when nothing is configured, so the caller can skip the concat.
    """
    by_region: dict[int, list[int]] = {}
    for type_id in config.trading_type_ids:
        source = config.special_markets.get(type_id)
        if source is not None:
            by_region.setdefault(source.region_id, []).append(type_id)

    frames: list[pl.DataFrame] = []
    for region_id, type_ids in sorted(by_region.items()):
        history.update(await _histories(client, region_id, sorted(type_ids)))
        for type_id in type_ids:
            pages = await client.get_all_pages(
                f"/markets/{region_id}/orders/",
                params={"order_type": "all", "type_id": type_id},
            )
            frame = orders_frame_from_pages(pages)
            if frame.height:
                # The global market has no single station; relabel to the home station so
                # summarize_tracked's station filter picks these orders up.
                frames.append(
                    frame.with_columns(pl.lit(home.station_id).cast(pl.Int64).alias("location_id"))
                )
    return pl.concat(frames) if frames else pl.DataFrame()


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
    region_orders = orders_frame_from_pages(pages)
    orders = region_orders.filter(pl.col("location_id") == home.station_id)
    listings = await _classify_own_listings(client, home, region_orders, character.open_orders)

    # The scan covers a fixed set of long-horizon items rather than a broad market sweep
    # — one watchlist verdict each. Most trade on the home station book; a few (PLEX) sit
    # on a special region-wide market, fetched separately and folded in with `location_id`
    # relabelled to the home station so the engine prices every item uniformly.
    normal_types = [t for t in config.trading_type_ids if t not in config.special_markets]
    history = await _histories(client, home.region_id, sorted(normal_types))
    special_book = await _special_market_book(client, config, home, history)
    tracked_orders = pl.concat([orders, special_book]) if special_book.height else orders

    tracked = summarize_tracked(
        type_ids=list(config.trading_type_ids),
        orders=tracked_orders,
        history=history_to_frame(history),
        station_id=home.station_id,
        holdings=character.holdings,
        window=config.investment.window_days,
        buy_position=config.investment.buy_below_position,
        sell_position=config.investment.sell_above_position,
        trend_days=config.investment.trend_days,
        max_downtrend=config.investment.max_downtrend,
    )
    builds = await _build_opportunities(client, config, character, sde) if sde is not None else []

    listing_buys = [status for status in listings if status.is_buy]
    listing_sells = [status for status in listings if not status.is_buy]
    name_ids = [status.type_id for status in tracked]
    name_ids += [status.type_id for status in listings]
    name_ids += [build.product_type_id for build in builds]
    # Material type ids too, so a build's bill-of-materials view can name each input.
    name_ids += [line.type_id for build in builds for line in build.analysis.materials]
    names = await name_cache.resolve(name_ids)
    tracked_ids = {status.type_id for status in tracked}
    window = config.investment.window_days
    retained = {
        type_id: sorted(history[type_id], key=lambda day: day.date)[-window:]
        for type_id in tracked_ids
        if type_id in history
    }
    return OpportunityReport(
        tracked=tracked,
        listing_buys=listing_buys,
        listing_sells=listing_sells,
        names=names,
        history=retained,
        builds=builds,
        sde_available=sde is not None,
    )
