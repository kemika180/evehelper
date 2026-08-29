"""Composition root: fetch, build the pure inputs, run the advisor. Impure.

Split into two phases so the TUI renders the character (and holdings) immediately,
then fills in market suggestions after the slower scan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import polars as pl

from evehelper.advisor.state import CharacterState
from evehelper.config import Config
from evehelper.data.assets import (
    AssetLocation,
    build_asset_tree,
    location_values,
    nameable_item_ids,
)
from evehelper.data.character import build_character_state
from evehelper.data.market import orders_frame_from_pages
from evehelper.data.sde import OreYield, SdeDatabase
from evehelper.data.skills import SkillReference, load_skills
from evehelper.data.structures import StructureCache
from evehelper.data.universe import NameCache
from evehelper.esi.auth import Authenticator
from evehelper.esi.client import EsiClient, EsiError
from evehelper.esi.endpoints import (
    fetch_affiliation,
    fetch_asset_names,
    fetch_assets,
    fetch_attributes,
    fetch_blueprints,
    fetch_industry_jobs,
    fetch_location,
    fetch_market_prices,
    fetch_open_orders,
    fetch_ship,
    fetch_skillqueue,
    fetch_skills,
    fetch_transactions,
)
from evehelper.esi.models import (
    Blueprint,
    CharacterAttributes,
    CharacterOrder,
    IndustryJob,
    MarketPrice,
    Skill,
    SkillQueueEntry,
    WalletTransaction,
)
from evehelper.market.listings import ListingStatus, OwnOrder, classify_listings
from evehelper.market.production import (
    BlueprintNeeded,
    BuildInput,
    BuildStep,
    BuyItem,
    MineableOre,
    OreSource,
    Recipe,
    RequiredMaterial,
    RequiredSkill,
    SelfSourcePlan,
    SourceNode,
    adjusted_material_quantity,
    build_source_tree,
    collect_needs,
    plan_ore_mining,
)
from evehelper.market.refining import (
    REPROCESSING_EFFICIENCY_SKILL_ID,
    REPROCESSING_SKILL_ID,
    mineral_commonness,
    ore_source_info,
    reprocessing_yield,
    security_target_rank,
)
from evehelper.market.training import (
    TrainingCandidate,
    TrainingTip,
    max_level_within,
    skill_points_for_level,
    training_tips,
)


@dataclass(frozen=True)
class CharacterReport:
    """Fast phase: character state, skill queue, and where the character is."""

    captured_at: datetime
    character: CharacterState
    skill_queue: list[SkillQueueEntry]
    # The character's trained skills (id + trained level), for the full skill view.
    skills: list[Skill]
    # Learning attributes — the SP/min training rate for the Crafting quick-train tips.
    attributes: CharacterAttributes
    names: dict[int, str]
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
    # Completed trades since the previous time this character was opened (newest first),
    # for the Overview "since your last visit" feed. Empty on a first-ever access.
    completed_transactions: list[WalletTransaction] = field(default_factory=list)
    # The solar system the character is currently in, so the self-source ore suggestions
    # bias toward what's mineable where they actually are. None if the location fetch failed.
    current_system_id: int | None = None
    # Total skill points, for the Overview tile.
    total_sp: int = 0
    # Header identity: corp always, alliance when in one, and the current ship. Any may be
    # None (unresolved name, no alliance, or the ship needs a scope the token lacks).
    corporation_name: str | None = None
    alliance_name: str | None = None
    ship_name: str | None = None
    # Asset type ids that are ammunition/charges (SDE category 8), so the asset view can
    # tell loaded ammo apart from the module holding it. Empty when the SDE isn't installed.
    charge_type_ids: frozenset[int] = frozenset()
    # Where the character is actually docked/flying right now (NPC station, player structure,
    # or the system when undocked), named — the header's location. None if the fetch failed.
    current_location_name: str | None = None


@dataclass(frozen=True)
class BuildOpportunity:
    """A build-vs-buy result for one owned blueprint, priced against the reference
    market (Jita). Per single run, so two copies of a type with different ME rank
    separately."""

    blueprint_item_id: int
    blueprint_type_id: int
    product_type_id: int
    material_efficiency: int
    # The self-source recipe: the full production tree and its flattened shopping/mining
    # plan. None when the SDE couldn't produce a tree (e.g. an unpriceable product).
    tree: SourceNode | None = None
    plan: SelfSourcePlan | None = None
    # Quick-train skill tips that would lower this build's self-source cost, best first.
    training_tips: tuple[TrainingTip, ...] = ()


@dataclass(frozen=True)
class OpportunityReport:
    """Slow phase: the market-priced overlays — own-order status, asset valuation,
    and build-vs-buy for owned blueprints."""

    # The character's own open orders, classified best/beaten, split by side.
    listing_buys: list[ListingStatus]
    listing_sells: list[ListingStatus]
    names: dict[int, str]
    # Build-vs-buy for owned blueprints, ranked by margin (empty if the SDE isn't
    # installed or no owned blueprint is manufacturable).
    builds: list[BuildOpportunity]
    # Whether the local SDE was available this run — lets the UI explain an empty
    # Manufacturing tab (needs `evehelper sde`) instead of leaving it blank.
    sde_available: bool
    # Total ISK value of assets at each place (global average reference prices), keyed by
    # location_id — the Overview ASSETS tile sums these.
    location_values: dict[int, float] = field(default_factory=dict)


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
    name_cache: NameCache,
    structure_cache: StructureCache,
    sde: SdeDatabase | None = None,
    *,
    since: datetime | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> CharacterReport:
    """Wallet, skills, skill queue, and inventory — the quick fetches.

    ``since`` is the previous session's access time: completed trades after it feed the
    Overview "since your last visit" list (empty on a first-ever access)."""
    token = await authenticator.access_token(character_id)
    skills = await fetch_skills(client, character_id, token)
    attributes = await fetch_attributes(client, character_id, token)
    open_orders = await fetch_open_orders(client, character_id, token)
    # Current location: the self-source ore suggestions bias toward this system's security.
    location = await fetch_location(client, character_id, token)
    # Header identity + the SP tile. Affiliation is public; the ship needs a scope the token
    # may not have yet (added 2026-08-17) — degrade to no ship rather than fail the fetch.
    affiliation = await fetch_affiliation(client, character_id)
    try:
        ship = await fetch_ship(client, character_id, token)
    except EsiError:
        ship = None
    character = await build_character_state(
        client, config, character_id, token, skills, open_orders
    )
    skill_queue = await fetch_skillqueue(client, character_id, token)

    assets = await fetch_assets(client, character_id, token)
    asset_tree = build_asset_tree(assets)
    # Loaded ammo/crystals share their weapon's slot flag; flag which asset types are
    # charges so the browser can nest them under the module instead of mislabelling them.
    charge_types = (
        sde.charge_type_ids({asset.type_id for asset in assets})
        if sde is not None
        else frozenset()
    )

    # Blueprint research is per-item (ME/TE/runs differ between two copies of one
    # type), so key it by item_id to match a selected asset row in the browser.
    blueprints = {bp.item_id: bp for bp in await fetch_blueprints(client, character_id, token)}

    industry_jobs = await fetch_industry_jobs(client, character_id, token)

    # Trades completed since the previous session — the Overview "since your last visit"
    # feed. Newest first; on a first-ever access (no cutoff) there's nothing to show yet.
    transactions = await fetch_transactions(client, character_id, token)
    completed = (
        sorted(
            (txn for txn in transactions if txn.date > since),
            key=lambda txn: txn.date,
            reverse=True,
        )
        if since is not None
        else []
    )

    # Player-assigned names for containers/ships (POST, 1000 ids/call), to make them
    # findable in the browser. Only singleton items that hold things can be named.
    nameable = nameable_item_ids(asset_tree)
    asset_names: dict[int, str] = {}
    for start in range(0, len(nameable), 1000):
        chunk = nameable[start : start + 1000]
        for named in await fetch_asset_names(client, character_id, token, chunk):
            if named.name and named.name != "None":
                asset_names[named.item_id] = named.name

    # Places to name: asset locations, each industry job's facility, every station the
    # character has an open order at (the overlay shows where), and where they are now.
    # Player structures don't resolve via /universe/names; look them up individually
    # (needs docking access).
    # Where the character actually is right now, for the header: the docked NPC station or
    # player structure, else the system they're flying in. Resolved alongside the other places.
    current_location_id = location.station_id or location.structure_id or location.solar_system_id
    place_ids = (
        {loc.location_id for loc in asset_tree}
        | {job.facility_id for job in industry_jobs}
        | {order.location_id for order in open_orders}
        | {current_location_id}
    )
    structure_ids = [
        place_id for place_id in place_ids if not _resolvable_location(place_id)
    ]
    structures = await structure_cache.resolve(token, structure_ids)

    # Names back up the bundled reference for any skill it doesn't cover, and label
    # asset/job types and their (resolvable) places plus each structure's solar system.
    name_ids = [entry.skill_id for entry in skill_queue]
    name_ids += [skill.skill_id for skill in skills.skills]
    name_ids += [asset.type_id for asset in assets]
    name_ids += [job.blueprint_type_id for job in industry_jobs]
    name_ids += [job.product_type_id for job in industry_jobs if job.product_type_id is not None]
    name_ids += [txn.type_id for txn in completed]
    name_ids += [place_id for place_id in place_ids if _resolvable_location(place_id)]
    name_ids += [structure.solar_system_id for structure in structures.values()]
    # Header identity: corp, alliance, and the current ship type all resolve via /universe/names.
    if affiliation is not None:
        name_ids.append(affiliation.corporation_id)
        if affiliation.alliance_id is not None:
            name_ids.append(affiliation.alliance_id)
    if ship is not None:
        name_ids.append(ship.ship_type_id)
    names = await name_cache.resolve(name_ids)
    for structure_id, structure in structures.items():
        system = names.get(structure.solar_system_id)
        names[structure_id] = f"{structure.name} · {system}" if system else structure.name
    current_location_name = names.get(current_location_id, str(current_location_id))
    corporation_name = names.get(affiliation.corporation_id) if affiliation is not None else None
    alliance_name = (
        names.get(affiliation.alliance_id)
        if affiliation is not None and affiliation.alliance_id is not None
        else None
    )
    ship_name = names.get(ship.ship_type_id) if ship is not None else None
    return CharacterReport(
        now(),
        character,
        skill_queue,
        skills.skills,
        attributes,
        names,
        load_skills(),
        asset_tree,
        asset_names,
        blueprints,
        industry_jobs,
        open_orders,
        completed,
        location.solar_system_id,
        skills.total_sp,
        corporation_name,
        alliance_name,
        ship_name,
        charge_types,
        current_location_name=current_location_name,
    )


def _reference_prices(prices: list[MarketPrice]) -> dict[int, float]:
    """Type id → CCP's global average reference price, the single map shared by asset
    valuation and build costing. EVE's mine/build/haul cycles take hours, so a smoothed
    average is a better estimate of what you'll actually transact against than a live ask
    that's stale before the goods reach a market. Falls back to the adjusted price for the
    few types ESI gives no average for; types with neither are omitted."""
    return {
        price.type_id: price.average_price or price.adjusted_price
        for price in prices
        if price.average_price or price.adjusted_price
    }


def _expand_recipes(
    sde: SdeDatabase,
    seed: list[Recipe],
    owned_blueprint_types: set[int],
    buildable: Callable[[int], bool] | None = None,
) -> dict[int, Recipe]:
    """Product-id → recipe for the seed recipes and every sub-material the character can
    *actually* build — i.e. one it owns the blueprint for — as a transitive closure, so
    a build's inputs can be self-costed.

    A sub-component whose blueprint the character doesn't own is left out: self-building
    it would mean acquiring the blueprint too (a cost this doesn't model), so it's costed
    by buying instead. When ``buildable`` is given, a sub-blueprint is also skipped unless
    the character meets its manufacturing skill requirements — so an owned-but-skill-locked
    component stays buy-only until the skill is trained (what the quick-train tips key off).
    Materials with no manufacturing recipe are likewise absent. Cycle-safe via a visited
    set; the engine additionally depth-caps the cost recursion."""
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
        if buildable is not None and not buildable(sub.blueprint_type_id):
            continue
        recipes[type_id] = sub
        frontier.extend(material.type_id for material in sub.materials)
    return recipes


def _expand_all_recipes(sde: SdeDatabase, seed: list[Recipe]) -> dict[int, Recipe]:
    """Product-id → recipe for the seed and *every* manufacturable sub-component, whether or
    not the character owns its blueprint — so the recipe can show the whole material tree at
    once (which blueprints to acquire and what to gather to build everything). Cycle-safe."""
    recipes: dict[int, Recipe] = {recipe.product_type_id: recipe for recipe in seed}
    visited: set[int] = set(recipes)
    frontier = [material.type_id for recipe in seed for material in recipe.materials]
    while frontier:
        type_id = frontier.pop()
        if type_id in visited:
            continue
        visited.add(type_id)
        sub = sde.recipe_for_product(type_id)
        if sub is None:
            continue
        recipes[type_id] = sub
        frontier.extend(material.type_id for material in sub.materials)
    return recipes


def _buildable_predicate(
    sde: SdeDatabase, skill_levels: Mapping[int, int]
) -> Callable[[int], bool]:
    """True for a blueprint the character meets every manufacturing skill requirement of."""

    def buildable(blueprint_type_id: int) -> bool:
        return all(
            skill_levels.get(skill_id, 0) >= level
            for skill_id, level in sde.manufacturing_skills(blueprint_type_id)
        )

    return buildable


def _ore_yields(
    sde: SdeDatabase, base_rate: float, skill_levels: Mapping[int, int], ore_ids: set[int]
) -> dict[int, float]:
    """Effective reprocessing yield per ore, from the base station rate and the character's
    Reprocessing / Reprocessing Efficiency / ore-specific processing levels."""
    ore_skills = sde.ore_reprocessing_skills(ore_ids)
    reprocessing = skill_levels.get(REPROCESSING_SKILL_ID, 0)
    efficiency = skill_levels.get(REPROCESSING_EFFICIENCY_SKILL_ID, 0)
    return {
        ore_id: reprocessing_yield(
            base_rate, reprocessing, efficiency, skill_levels.get(ore_skills.get(ore_id, 0), 0)
        )
        for ore_id in ore_ids
    }


# How many ore options to offer per mineral (the first is the plan default; the rest are
# shown as alternatives, since the best ore depends on where the character can mine).
_MAX_REFINE_OPTIONS = 3


def _ore_meta(sde: SdeDatabase, ore_ids: set[int]) -> dict[int, tuple[int, str]]:
    """Each ore's (accessibility rank, rough location label) from its reprocessing skill."""
    return {
        ore_id: ore_source_info(skill_id)
        for ore_id, skill_id in sde.ore_reprocessing_skills(ore_ids).items()
    }


def _base_ore_options(
    ores: list[OreYield],
    yields: Mapping[int, float],
    ore_meta: Mapping[int, tuple[int, str]],
    target_rank: int,
) -> list[OreSource]:
    """A mineral's base ores as `OreSource`s, most-accessible first.

    Ordered by how close each ore's accessibility rank is to ``target_rank`` (the current
    system's security band), then by how little ore it takes to mine (denser ore first) — so
    the ore they can realistically mine nearby leads. Same-family quality variants are collapsed
    to the base rock by name: an ore whose name ends in another candidate's full name (e.g.
    "Brimful Zeolites" vs "Zeolites") is dropped, since higher-quality variants are rarer."""
    yielding = [ore for ore in ores if ore.units_per_ore * yields.get(ore.ore_type_id, 0.0) > 0]
    names = {ore.name for ore in yielding if ore.name}
    base = [
        ore
        for ore in yielding
        if not any(other != ore.name and ore.name.endswith(f" {other}") for other in names)
    ]

    def rank_of(ore_type_id: int) -> int:
        return ore_meta.get(ore_type_id, (3, "nullsec"))[0]

    def location_of(ore_type_id: int) -> str:
        return ore_meta.get(ore_type_id, (3, "nullsec"))[1]

    options = [
        OreSource(
            ore.ore_type_id,
            1.0 / (ore.units_per_ore * yields[ore.ore_type_id]),
            location_of(ore.ore_type_id),
            ore.volume,
        )
        for ore in base
    ]
    options.sort(
        key=lambda option: (
            abs(rank_of(option.ore_type_id) - target_rank),  # closest to current security first
            option.ore_units_per_unit,  # then the ore that takes the least mining
        )
    )
    return options


def _refine_sources(
    ore_sources: Mapping[int, list[OreYield]],
    yields: Mapping[int, float],
    ore_meta: Mapping[int, tuple[int, str]],
    target_rank: int,
) -> dict[int, tuple[OreSource, ...]]:
    """How to mine each mineral: a few base-ore options (accessibility-ordered), the first
    being the recommended one and the rest alternatives — no ISK, just where and how much.

    Naive by design (a documented simplification): an ore is only credited for the mineral
    it's densest in, ignoring the other minerals it also yields."""
    sources: dict[int, tuple[OreSource, ...]] = {}
    for mineral_id, ores in ore_sources.items():
        options = _base_ore_options(ores, yields, ore_meta, target_rank)[:_MAX_REFINE_OPTIONS]
        if options:
            sources[mineral_id] = tuple(options)
    return sources


def _tree_type_ids(node: SourceNode) -> set[int]:
    """Every type id in a self-source build tree — products, sub-components and minerals — so
    all of them can be named. (The ores to mine live in the plan, not the tree.)"""
    ids = {node.type_id}
    for child in node.children:
        ids |= _tree_type_ids(child)
    return ids


def _attribute_value(attributes: CharacterAttributes, name: str) -> int:
    """The character's point value for a named learning attribute (e.g. "Memory")."""
    return int(getattr(attributes, name.lower(), 0))


def _required_skills(
    sde: SdeDatabase,
    recipe_map: Mapping[int, Recipe],
    character: CharacterReport,
    skill_levels: Mapping[int, int],
    skill_sp: Mapping[int, int],
) -> tuple[RequiredSkill, ...]:
    """The manufacturing skills every blueprint in the tree requires (deduped to the highest
    level any needs), with the character's current level and the time to train up to it."""
    needed: dict[int, int] = {}
    for recipe in recipe_map.values():
        for skill_id, level in sde.manufacturing_skills(recipe.blueprint_type_id):
            needed[skill_id] = max(needed.get(skill_id, 0), level)
    skills: list[RequiredSkill] = []
    for skill_id, level in sorted(needed.items()):
        current = skill_levels.get(skill_id, 0)
        seconds: float | None = None
        reference = character.skill_reference.get(skill_id)
        if current < level and reference is not None:
            rate = _attribute_value(character.attributes, reference.primary) + _attribute_value(
                character.attributes, reference.secondary
            ) / 2
            if rate > 0:
                target_sp = skill_points_for_level(reference.rank, level)
                remaining = max(0, target_sp - skill_sp.get(skill_id, 0))
                seconds = remaining / rate * 60.0
        skills.append(RequiredSkill(skill_id, level, current, seconds))
    return tuple(skills)


def _build_opportunities(
    config: Config,
    character: CharacterReport,
    sde: SdeDatabase,
    reference_price: dict[int, float],
) -> list[BuildOpportunity]:
    """The build-vs-buy figure and the (cost-free) self-source recipe for each owned,
    manufacturable blueprint, plus quick-train tips that ease self-sourcing. Materials are
    priced only for the buy/sell comparison; the mine/build side carries no ISK."""
    seeds = [
        (item_id, blueprint, recipe)
        for item_id, blueprint in character.blueprints.items()
        if (recipe := sde.manufacturing_recipe(blueprint.type_id)) is not None
    ]
    if not seeds:
        return []

    owned_types = {bp.type_id for bp in character.blueprints.values()}
    skill_levels = {skill.skill_id: skill.active_skill_level for skill in character.skills}
    skill_sp = {skill.skill_id: skill.skillpoints_in_skill for skill in character.skills}
    base_rate = config.refining.base_rate
    horizon_seconds = config.training.quick_horizon_hours * 3600.0
    # Bias ore options toward what's mineable at the character's current system security
    # (commons in highsec); an unknown/unplaced system defaults to commons.
    system_id = character.current_system_id
    security = sde.system_security(system_id) if system_id is not None else None
    target_rank = security_target_rank(security) if security is not None else 0

    def self_source(
        recipe: Recipe, me: int, levels: Mapping[int, int]
    ) -> tuple[SourceNode, SelfSourcePlan]:
        """The full self-source recipe for one blueprint: expand through *every* manufacturable
        sub-component (owned blueprint or not), so the whole material tree is visible at once —
        the byproduct-aware mining plan, the components to build, the blueprints to acquire, and
        the items to buy. ``levels`` set each ore's yield."""
        recipe_map = _expand_all_recipes(sde, [recipe])
        material_types = set(recipe_map) | {
            mat.type_id for sub in recipe_map.values() for mat in sub.materials
        }
        ore_sources = sde.ore_sources(material_types)
        ore_ids = {oy.ore_type_id for ores in ore_sources.values() for oy in ores}
        yields = _ore_yields(sde, base_rate, levels, ore_ids)
        options = _refine_sources(ore_sources, yields, _ore_meta(sde, ore_ids), target_rank)
        tree = build_source_tree(
            recipe, material_efficiency=me, refinable=frozenset(options), recipes=recipe_map
        )
        needs = collect_needs(tree)

        # Each ore's full composition (units of each mineral per ore unit, after yield) — the
        # source of the byproducts the plan credits.
        composition: dict[int, dict[int, float]] = {}
        for mineral, ores in ore_sources.items():
            for ore in ores:
                per = ore.units_per_ore * yields.get(ore.ore_type_id, 0.0)
                composition.setdefault(ore.ore_type_id, {})[mineral] = per
        chosen = {
            mineral: MineableOre(
                options[mineral][0].ore_type_id,
                options[mineral][0].location,
                options[mineral][0].unit_volume,
                composition.get(options[mineral][0].ore_type_id, {}),
            )
            for mineral in needs.minerals
        }
        # Rarest minerals first, so the abundant common-mineral byproducts of their ores are
        # credited before we mine dedicated ore for the common minerals (Tritanium last).
        rarity = tuple(sorted(needs.minerals, key=lambda mid: (-mineral_commonness(mid), mid)))
        mine = plan_ore_mining(needs.minerals, chosen, rarity)

        # Packaged m³ per unit for every type in the tree (materials, components, buy items).
        volumes = sde.volumes(material_types)

        # A direct material is `refine` (mine), `build` (a blueprint you own), `buildable` (you'd
        # need to acquire the blueprint) or `buy` (nothing makes it).
        def source_of(child: SourceNode) -> str:
            if child.source == "mine":
                return "refine"
            if child.source == "build":
                owned = recipe_map[child.type_id].blueprint_type_id in owned_types
                return "build" if owned else "buildable"
            return "buy"

        materials = tuple(
            RequiredMaterial(
                child.type_id,
                child.quantity,
                reference_price.get(child.type_id),
                volumes.get(child.type_id, 0.0),
                source_of(child),
            )
            for child in tree.children
        )
        # Aggregate every sub-component to build (below the top) by product: total units + runs.
        totals: dict[int, tuple[int, int]] = {}

        def collect_builds(node: SourceNode) -> None:
            for child in node.children:
                if child.source == "build":
                    units, runs = totals.get(child.type_id, (0, 0))
                    totals[child.type_id] = (units + child.quantity, runs + child.runs)
                    collect_builds(child)

        collect_builds(tree)

        depth_cache: dict[int, int] = {}

        def build_depth(product: int) -> int:
            recipe_here = recipe_map.get(product)
            if recipe_here is None:
                return -1  # not built
            if product in depth_cache:
                return depth_cache[product]
            depth_cache[product] = 0  # cycle guard
            depth = 0
            for material in recipe_here.materials:
                child_depth = build_depth(material.type_id)
                if child_depth >= 0:
                    depth = max(depth, child_depth + 1)
            depth_cache[product] = depth
            return depth

        build_steps = tuple(
            BuildStep(
                product,
                units,
                runs,
                tuple(
                    BuildInput(
                        material.type_id,
                        (qty := adjusted_material_quantity(material.quantity, runs, 0)),
                        qty * volumes.get(material.type_id, 0.0),
                    )
                    for material in recipe_map[product].materials
                ),
            )
            # Dependencies first (lowest build-depth), so you can build bottom-up.
            for product, (units, runs) in sorted(
                totals.items(), key=lambda item: (build_depth(item[0]), item[0])
            )
        )
        # Buy items — with a hint at how each could instead be made (reaction / PI), which the
        # tool doesn't model yet, so they aren't a hard "buy".
        buy_type_ids = {item.type_id for item in needs.buy}
        reactions = sde.reaction_products(buy_type_ids)
        pi_products = sde.pi_products(buy_type_ids)

        def buy_method(type_id: int) -> str:
            if type_id in reactions:
                return "reaction"
            if type_id in pi_products:
                return "pi"
            return "buy"

        buy_items = tuple(
            BuyItem(
                item.type_id,
                item.quantity,
                reference_price.get(item.type_id),
                volumes.get(item.type_id, 0.0),
                buy_method(item.type_id),
            )
            for item in needs.buy
        )
        # Blueprints to acquire: each manufactured component whose blueprint the character does
        # not own (deduped), with an estimated copy-job cost as a stand-in for the BPC's contract
        # price — EIV (materials at Jita, ME 0) x copying index x runs.
        copy_index = config.industry.copy_cost_index

        def copy_cost(product: int, runs: int) -> float | None:
            sub_recipe = recipe_map.get(product)
            if sub_recipe is None:
                return None
            eiv = 0.0
            priced = False
            for material in sub_recipe.materials:
                price = reference_price.get(material.type_id)
                if price is not None:
                    eiv += material.quantity * price
                    priced = True
            return eiv * copy_index * runs if priced else None

        seen_bp: set[int] = set()
        needed_bps: list[BlueprintNeeded] = []
        for product, sub in sorted(recipe_map.items()):
            bp_type = sub.blueprint_type_id
            if product == recipe.product_type_id or bp_type in owned_types or bp_type in seen_bp:
                continue
            seen_bp.add(bp_type)
            runs = totals.get(product, (0, 1))[1]
            needed_bps.append(BlueprintNeeded(bp_type, product, copy_cost(product, runs)))
        required = _required_skills(sde, recipe_map, character, skill_levels, skill_sp)
        return tree, SelfSourcePlan(
            materials, mine, tuple(build_steps), buy_items, required, tuple(needed_bps)
        )

    def total_ore(recipe: Recipe, me: int, levels: Mapping[int, int]) -> int:
        """Total units of ore the byproduct-aware plan would have you mine."""
        _, plan = self_source(recipe, me, levels)
        return sum(line.quantity for line in plan.mine)

    builds: list[BuildOpportunity] = []
    for item_id, blueprint, recipe in seeds:
        me = blueprint.material_efficiency
        tree, plan = self_source(recipe, me, skill_levels)
        tips = _training_tips(
            recipe, me, character, sde, owned_types, skill_levels, skill_sp,
            plan, total_ore, horizon_seconds,
        )
        builds.append(
            BuildOpportunity(
                blueprint_item_id=item_id,
                blueprint_type_id=blueprint.type_id,
                product_type_id=recipe.product_type_id,
                material_efficiency=me,
                tree=tree,
                plan=plan,
                training_tips=tips,
            )
        )
    builds.sort(key=lambda build: build.product_type_id)  # display order is alphabetical in the TUI
    return builds


def _training_tips(
    recipe: Recipe,
    me: int,
    character: CharacterReport,
    sde: SdeDatabase,
    owned_types: set[int],
    skill_levels: Mapping[int, int],
    skill_sp: Mapping[int, int],
    plan: SelfSourcePlan,
    total_ore: Callable[[Recipe, int, Mapping[int, int]], int],
    horizon_seconds: float,
) -> tuple[TrainingTip, ...]:
    """Quick-train tips for one build: reprocessing skills that cut how much ore it takes to
    self-source, and a manufacturing skill that unlocks self-building a sub-component. The
    pure `training_tips` keeps those trainable within the horizon."""
    candidates: list[TrainingCandidate] = []

    def candidate(skill_id: int, target_level: int, kind: str, **benefit: object) -> None:
        reference = character.skill_reference.get(skill_id)
        if reference is None:
            return
        candidates.append(
            TrainingCandidate(
                skill_id=skill_id,
                current_level=skill_levels.get(skill_id, 0),
                target_level=target_level,
                rank=reference.rank,
                primary_attribute=_attribute_value(character.attributes, reference.primary),
                secondary_attribute=_attribute_value(character.attributes, reference.secondary),
                current_sp=skill_sp.get(skill_id, 0),
                kind=kind,
                **benefit,  # type: ignore[arg-type]
            )
        )

    # (1) Reprocessing skills — how much less ore, at the highest level reachable in the horizon
    # (from scratch that's often several levels, e.g. 0->3 in a couple hours).
    mined = {line.ore_type_id for line in plan.mine}
    if mined:
        base_ore = sum(line.quantity for line in plan.mine)
        ore_skills = sde.ore_reprocessing_skills(mined)
        general = {REPROCESSING_SKILL_ID, REPROCESSING_EFFICIENCY_SKILL_ID}
        relevant = general | set(ore_skills.values())
        for skill_id in relevant:
            level = skill_levels.get(skill_id, 0)
            reference = character.skill_reference.get(skill_id)
            if level >= 5 or base_ore <= 0 or reference is None:
                continue
            sp_per_minute = _attribute_value(
                character.attributes, reference.primary
            ) + _attribute_value(character.attributes, reference.secondary) / 2
            target = max_level_within(
                reference.rank, level, skill_sp.get(skill_id, 0), sp_per_minute, horizon_seconds
            )
            if target <= level:
                continue
            improved = total_ore(recipe, me, {**skill_levels, skill_id: target})
            reduction = (base_ore - improved) / base_ore
            candidate(skill_id, target, "refine", ore_reduction=reduction)

    # Manufacturing skills needed to build the item are covered by the recipe's "skills to build
    # this" section (with their training times), so the tips stay focused on cutting mining.
    return tuple(training_tips(candidates, horizon_seconds=horizon_seconds))


async def _classify_own_listings(
    client: EsiClient, open_orders: list[CharacterOrder]
) -> list[ListingStatus]:
    """Decide, for each open order, whether it still leads its market. This is the one
    live-priced view — "am I being undercut *right now*" needs current competing orders,
    not a smoothed average — so it fetches just the book for each type the character has an
    order in (a few pages each), never the whole region."""
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
    pairs = sorted({(order.region_id, order.type_id) for order in open_orders})
    frames: list[pl.DataFrame] = []
    for region_id, type_id in pairs:
        pages = await client.get_all_pages(
            f"/markets/{region_id}/orders/", params={"order_type": "all", "type_id": type_id}
        )
        frames.append(orders_frame_from_pages(pages))
    book = pl.concat(frames) if frames else pl.DataFrame()
    return classify_listings(book, own)


async def fetch_opportunities(
    client: EsiClient,
    config: Config,
    character: CharacterReport,
    name_cache: NameCache,
    sde: SdeDatabase | None = None,
) -> OpportunityReport:
    """Classify the character's own orders (live), then value their assets and run
    build-vs-buy for owned blueprints against CCP's global average reference prices."""
    listings = await _classify_own_listings(client, character.open_orders)

    # One cheap global call gives an average reference price per type — a realistic figure
    # for EVE's slow mine/build/haul cycles — shared by build-costing and asset valuation.
    reference_price = _reference_prices(await fetch_market_prices(client))
    builds = (
        _build_opportunities(config, character, sde, reference_price)
        if sde is not None
        else []
    )

    # Value the character's holdings at those prices, for the Overview ASSETS tile.
    values_by_place = location_values(character.assets, reference_price)

    listing_buys = [status for status in listings if status.is_buy]
    listing_sells = [status for status in listings if not status.is_buy]
    name_ids = [status.type_id for status in listings]
    name_ids += [build.product_type_id for build in builds]
    # Material type ids too, so a build's bill-of-materials view can name each input.
    name_ids += [
        material.type_id
        for build in builds
        if build.plan is not None
        for material in build.plan.materials
    ]
    # Every type in the self-source recipe tree (minerals, sub-components), the ores in its
    # mining plan, and each quick-train tip's skill, so the recipe/gather views can name them.
    name_ids += [
        type_id
        for build in builds
        if build.tree is not None
        for type_id in _tree_type_ids(build.tree)
    ]
    name_ids += [
        line.ore_type_id
        for build in builds
        if build.plan is not None
        for line in build.plan.mine
    ]
    name_ids += [tip.skill_id for build in builds for tip in build.training_tips]
    # Required-skill names and the blueprints a component would need to be self-built.
    name_ids += [
        skill.type_id
        for build in builds
        if build.plan is not None
        for skill in build.plan.required_skills
    ]
    name_ids += [
        bp.blueprint_type_id
        for build in builds
        if build.plan is not None
        for bp in build.plan.blueprints
    ]
    names = await name_cache.resolve(name_ids)
    return OpportunityReport(
        listing_buys=listing_buys,
        listing_sells=listing_sells,
        names=names,
        builds=builds,
        sde_available=sde is not None,
        location_values=values_by_place,
    )
