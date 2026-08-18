"""Self-source recipe engine. PURE — no I/O.

Given (recipe, ore sources) handed in by the impure layer, ``build_source_tree`` /
``flatten_plan`` produce the **self-source recipe**: how to make an item yourself, expanding
each material into a sub-build or the ore to mine. This side is **cost-free by design** —
mining's real cost is time, travel and risk, not a market price — so it says only *what* to
gather, roughly *where* (highsec/lowsec/null/abyssal/moon), and how much volume it is (to
judge hauling trips), leaving the worth-it call to the player.

The ``Recipe`` shape is activity-agnostic: manufacturing is the first consumer, but reactions
and planetary interaction fit the same shape later.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RecipeMaterial:
    """One input line of a recipe: a material type and the base quantity one run needs,
    before material-efficiency reductions."""

    type_id: int
    quantity: int


@dataclass(frozen=True)
class Recipe:
    """A production recipe — one blueprint's product and the materials a single run
    consumes. Material quantities are pre-ME; the engine applies the blueprint's
    material-efficiency level when costing a build."""

    blueprint_type_id: int
    product_type_id: int
    product_quantity: int  # units produced per run
    materials: tuple[RecipeMaterial, ...]


def adjusted_material_quantity(base_quantity: int, runs: int, material_efficiency: int) -> int:
    """Units of a material a job actually consumes, after the blueprint's ME.

    Follows EVE's job formula ``max(runs, ceil(round(base * runs * (1 - ME/100), 2)))``
    -- ME never takes a material below one unit per run. Structure/rig bonuses are not
    modelled yet (a documented simplification); they'd multiply into the same factor."""
    reduced = base_quantity * runs * (1 - material_efficiency / 100)
    return max(runs, math.ceil(round(reduced, 2)))


# --- self-source recipe tree (cost-free) -------------------------------------
# How to make an item yourself: each material is either built (its own sub-recipe), mined
# (refined from ore), or bought. No ISK — the self-source cost is time/travel/risk, not a
# price — just *what* to gather, *where*, and how much volume. `flatten_plan` rolls the tree
# up into a mining + building + buying to-do list.


@dataclass(frozen=True)
class OreSource:
    """One ore a mineral can be refined from: the ore, how many units to mine per unit of
    the mineral (after the character's reprocessing yield), its rough location band
    (highsec/lowsec/nullsec/abyssal/moon), and the per-unit volume (m³, for hauling)."""

    ore_type_id: int
    ore_units_per_unit: float
    location: str
    unit_volume: float

    def ore_units_for(self, mineral_quantity: int) -> int:
        """Whole units of ore to mine for ``mineral_quantity`` units of the mineral."""
        return math.ceil(mineral_quantity * self.ore_units_per_unit)


# Cap on how deep the self-build recursion follows a chain of sub-components — a safety
# bound, not a real-tree limit.
_MAX_BUILD_DEPTH = 5


@dataclass(frozen=True)
class SourceNode:
    """One node of a self-source recipe tree. ``source`` is how this type is obtained:
    ``build`` (children are its sub-materials, ``runs`` blueprint runs), ``mine`` (a mineral
    refined from ore — the ore itself comes from the byproduct-aware mining plan, not the
    tree), ``buy`` (a leaf), or ``?``. ``quantity`` is the units of this type supplied."""

    type_id: int
    quantity: int
    source: str
    runs: int = 0
    children: tuple[SourceNode, ...] = ()


def build_source_tree(
    recipe: Recipe,
    *,
    material_efficiency: int,
    refinable: frozenset[int],
    recipes: Mapping[int, Recipe],
    runs: int = 1,
) -> SourceNode:
    """The self-source build tree for ``runs`` runs of ``recipe`` — the top build node and
    every input classified build / mine / buy (``refinable`` is the set of minerals an ore
    refines into). Pure; cycle-guarded and depth-capped, applying the top blueprint's ME while
    sub-builds assume ME 0 (a conservative estimate). The ore *behind* each mined mineral is
    decided holistically by ``plan_ore_mining``, not here, so byproducts can be credited."""
    return _build_node(
        recipe.product_type_id,
        recipe.product_quantity * runs,
        recipe,
        material_efficiency,
        refinable,
        recipes,
        _MAX_BUILD_DEPTH,
        frozenset(),
    )


def _build_node(
    type_id: int,
    quantity: int,
    recipe: Recipe,
    material_efficiency: int,
    refinable: frozenset[int],
    recipes: Mapping[int, Recipe],
    depth: int,
    stack: frozenset[int],
) -> SourceNode:
    """A ``build`` node making ``quantity`` units of ``type_id`` and its material children."""
    runs_needed = max(1, math.ceil(quantity / recipe.product_quantity))
    inner = stack | {type_id}
    children = tuple(
        _material_node(
            material.type_id,
            adjusted_material_quantity(material.quantity, runs_needed, material_efficiency),
            refinable,
            recipes,
            depth - 1,
            inner,
        )
        for material in recipe.materials
    )
    return SourceNode(type_id, quantity, "build", runs=runs_needed, children=children)


def _material_node(
    type_id: int,
    quantity: int,
    refinable: frozenset[int],
    recipes: Mapping[int, Recipe],
    depth: int,
    stack: frozenset[int],
) -> SourceNode:
    """One material: built if it has an (owned, in-scope) sub-recipe, else mined if an ore
    refines into it, else bought."""
    recipe = recipes.get(type_id)
    if recipe is not None and depth > 0 and type_id not in stack and recipe.product_quantity > 0:
        return _build_node(type_id, quantity, recipe, 0, refinable, recipes, depth, stack)
    if type_id in refinable:
        return SourceNode(type_id, quantity, "mine")
    return SourceNode(type_id, quantity, "buy")


@dataclass(frozen=True)
class BuildRun:
    """A sub-build to run to self-source the product: which product, and how many runs."""

    product_type_id: int
    runs: int


@dataclass(frozen=True)
class BuyLine:
    """A material that must be bought (nothing mines or builds it): the type and quantity."""

    type_id: int
    quantity: int


@dataclass(frozen=True)
class RecipeNeeds:
    """What a self-source build needs at the raw level: total units of each mineral to
    refine, sub-builds to run, and materials to buy."""

    minerals: dict[int, int]
    build: tuple[BuildRun, ...]
    buy: tuple[BuyLine, ...]


def collect_needs(tree: SourceNode) -> RecipeNeeds:
    """Walk a build tree into raw requirements: mineral units to refine, sub-blueprint runs,
    and buy quantities. The ore to mine for the minerals is decided later (byproduct-aware)."""
    minerals: dict[int, int] = {}
    builds: list[BuildRun] = []
    buys: dict[int, int] = {}

    def walk(node: SourceNode) -> None:
        if node.source == "build":
            builds.append(BuildRun(node.type_id, node.runs))
            for child in node.children:
                walk(child)
        elif node.source == "mine":
            minerals[node.type_id] = minerals.get(node.type_id, 0) + node.quantity
        elif node.source == "buy":
            buys[node.type_id] = buys.get(node.type_id, 0) + node.quantity

    walk(tree)
    return RecipeNeeds(
        minerals,
        tuple(builds),
        tuple(BuyLine(type_id, qty) for type_id, qty in sorted(buys.items())),
    )


@dataclass(frozen=True)
class MineableOre:
    """The ore to mine for a mineral: which ore, where, its m³ per unit, and how much of each
    mineral one unit yields after the character's reprocessing (its full composition — the
    source of the byproducts)."""

    ore_type_id: int
    location: str
    unit_volume: float
    yields: Mapping[int, float]  # mineral id -> units per ore unit, after reprocessing


@dataclass(frozen=True)
class MineLine:
    """One ore to mine: the ore, total units, total volume (m³), and rough location."""

    ore_type_id: int
    quantity: int
    volume: float
    location: str


def plan_ore_mining(
    needs: Mapping[int, int],
    chosen: Mapping[int, MineableOre],
    rarity_order: tuple[int, ...],
) -> tuple[MineLine, ...]:
    """The ore-mining plan that satisfies every mineral need, crediting byproducts. Pure.

    Minerals are handled in ``rarity_order`` (least-commonly-refined first): mining the ore for
    a rare mineral also yields the common minerals, so those are credited *before* we decide to
    mine for them — you top up the common minerals only for whatever the byproducts didn't
    already cover. ``chosen`` maps a mineral to the ore to mine for it."""
    remaining: dict[int, float] = {mineral: float(units) for mineral, units in needs.items()}
    mined: dict[int, int] = {}
    ore: dict[int, MineableOre] = {}
    for mineral in rarity_order:
        if remaining.get(mineral, 0) <= 0:
            continue
        source = chosen.get(mineral)
        per_ore = source.yields.get(mineral, 0.0) if source is not None else 0.0
        if source is None or per_ore <= 0:
            continue
        units = math.ceil(remaining[mineral] / per_ore)
        mined[source.ore_type_id] = mined.get(source.ore_type_id, 0) + units
        ore[source.ore_type_id] = source
        for yielded, per in source.yields.items():
            if yielded in remaining and per > 0:
                remaining[yielded] -= units * per
    lines = [
        MineLine(ore_id, units, units * ore[ore_id].unit_volume, ore[ore_id].location)
        for ore_id, units in mined.items()
    ]
    return tuple(sorted(lines, key=lambda line: (line.location, line.ore_type_id)))


@dataclass(frozen=True)
class RequiredMaterial:
    """One direct material of a blueprint (the recipe line): the type, ME-adjusted quantity,
    its Jita buy price and volume, and how it's self-sourced — ``refine`` (mine ore), ``build``
    (a sub-blueprint you own), ``buildable`` (manufacturable, but you'd need to acquire its
    blueprint) or ``buy`` (nothing makes it)."""

    type_id: int
    quantity: int
    buy_unit_price: float | None
    unit_volume: float
    source: str

    @property
    def buy_cost(self) -> float | None:
        return self.quantity * self.buy_unit_price if self.buy_unit_price is not None else None

    @property
    def volume(self) -> float:
        return self.quantity * self.unit_volume


@dataclass(frozen=True)
class BuildInput:
    """One material a build step consumes: the type, total quantity, and total volume (m³)."""

    type_id: int
    quantity: int
    volume: float


@dataclass(frozen=True)
class BuildStep:
    """A sub-component to build to self-source the product, aggregated across the whole tree:
    the product, total units and blueprint runs, and its material inputs (each with volume) so
    the recipe shows *how* to build it — one material per line, not a wall."""

    product_type_id: int
    quantity: int
    runs: int
    inputs: tuple[BuildInput, ...]


@dataclass(frozen=True)
class BuyItem:
    """A material the tool doesn't self-source: type, quantity, Jita price, volume. ``method``
    hints how it could be made if you don't buy it — ``buy`` (nothing makes it), ``reaction`` (a
    reaction output) or ``pi`` (planetary industry) — those chains just aren't modelled yet."""

    type_id: int
    quantity: int
    buy_unit_price: float | None
    unit_volume: float
    method: str = "buy"

    @property
    def buy_cost(self) -> float | None:
        return self.quantity * self.buy_unit_price if self.buy_unit_price is not None else None


@dataclass(frozen=True)
class RequiredSkill:
    """A manufacturing skill needed to build the item (or one of its components): the skill,
    the highest level any of those blueprints requires, the character's current level, and the
    time to train up to it (``None`` if already met or the rate is unknown)."""

    type_id: int
    level: int
    current_level: int
    train_seconds: float | None

    @property
    def met(self) -> bool:
        return self.current_level >= self.level


@dataclass(frozen=True)
class BlueprintNeeded:
    """A blueprint the character would have to acquire to self-build a component: the blueprint
    type, the component it manufactures, and an estimate of the **copy-job cost** to make the BPC
    yourself (EIV x copying cost index x runs) — a proxy for the BPC's contract price, since BPCs
    aren't on the market. ``copy_cost`` is None when the inputs couldn't be priced."""

    blueprint_type_id: int
    product_type_id: int
    copy_cost: float | None


@dataclass(frozen=True)
class SelfSourcePlan:
    """Everything the crafting popup shows for one build: the recipe's direct materials (with
    buy price + volume + how each is self-sourced), the skills the blueprint requires, and — to
    self-source them — the ore to mine (byproduct-aware), the sub-components to build, the
    blueprints you'd need to acquire, and the items to buy."""

    materials: tuple[RequiredMaterial, ...]
    mine: tuple[MineLine, ...]
    build: tuple[BuildStep, ...]
    buy: tuple[BuyItem, ...]
    required_skills: tuple[RequiredSkill, ...] = ()
    blueprints: tuple[BlueprintNeeded, ...] = ()

    @property
    def missing_skills(self) -> tuple[RequiredSkill, ...]:
        """The required skills the character doesn't yet meet."""
        return tuple(skill for skill in self.required_skills if not skill.met)

    @property
    def total_training_seconds(self) -> float:
        """Total time to train every not-yet-met skill (skills train one at a time)."""
        return sum(skill.train_seconds or 0.0 for skill in self.missing_skills)

    @property
    def total_buy_cost_items(self) -> float | None:
        """Cost to buy every buy-list item at Jita. None if none is priced."""
        priced = [item.buy_cost for item in self.buy if item.buy_cost is not None]
        return sum(priced) if priced else None

    @property
    def total_blueprint_cost(self) -> float | None:
        """Estimated total copy-job cost for every needed blueprint. None if none is priced."""
        priced = [bp.copy_cost for bp in self.blueprints if bp.copy_cost is not None]
        return sum(priced) if priced else None

    @property
    def total_buy_cost(self) -> float | None:
        """Cost to buy every direct material at Jita — None if any isn't priced."""
        if any(material.buy_cost is None for material in self.materials):
            return None
        return sum(material.buy_cost or 0.0 for material in self.materials)

    @property
    def total_material_volume(self) -> float:
        """Total m³ of the finished materials (packaged) — for hauling the bought version."""
        return sum(material.volume for material in self.materials)

    @property
    def total_mine_volume(self) -> float:
        """Total m³ of ore to haul if self-sourced."""
        return sum(line.volume for line in self.mine)
