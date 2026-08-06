"""Build-vs-buy production engine. PURE — no I/O.

Given a recipe (a product and the materials it needs) plus a price for each type, it
decides whether *building* an item beats *buying* it. Deterministic and unit-tested on
fixtures — the recipe and prices are handed in by the impure layer (``data/sde.py``
for recipes, the market snapshot for prices), never fetched here.

The ``Recipe`` shape is deliberately activity-agnostic: manufacturing is the first
consumer, but reactions and planetary interaction produce an output from a set of
inputs too, so they can feed their own recipes through the same engine later.
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


def _cheapest(*options: float | None) -> float | None:
    """The smallest of the priced options, or None when none is priced."""
    present = [option for option in options if option is not None]
    return min(present) if present else None


@dataclass(frozen=True)
class MaterialLine:
    """One input of a build: the material, its ME-adjusted quantity for the analyzed run
    count, and the per-unit cost of each way to obtain it — buying it at the reference
    market, self-building it, or mining-and-refining ore into it (each None when that
    source doesn't apply or can't be priced)."""

    type_id: int
    quantity: int
    unit_price: float | None
    build_unit_cost: float | None = None
    refine_unit_cost: float | None = None

    @property
    def line_cost(self) -> float | None:
        """Cost to buy this line outright: quantity times unit price, or None."""
        return self.quantity * self.unit_price if self.unit_price is not None else None

    @property
    def best_unit_cost(self) -> float | None:
        """The cheapest of buying, self-building, and refining, or None if unpriced."""
        return _cheapest(self.unit_price, self.build_unit_cost, self.refine_unit_cost)

    @property
    def best_line_cost(self) -> float | None:
        """Cost to acquire this line at its cheapest source, or None if unpriceable."""
        best = self.best_unit_cost
        return self.quantity * best if best is not None else None

    @property
    def source(self) -> str:
        """The cheapest source: 'buy', 'build', 'refine', or '?' (unpriceable). Ties
        prefer buying (no production effort), then building, then refining."""
        best = self.best_unit_cost
        if best is None:
            return "?"
        if self.unit_price is not None and self.unit_price <= best:
            return "buy"
        if self.build_unit_cost is not None and self.build_unit_cost <= best:
            return "build"
        return "refine"


@dataclass(frozen=True)
class BuildAnalysis:
    """The build-vs-buy verdict for one recipe at a given ME, run count, and prices."""

    runs: int
    material_cost: float  # cost to BUY every (priced) material outright, for `runs` runs
    product_value: float  # gross sale value of the output before fees (0 if unpriced)
    net_product_value: float  # after sales tax + broker fee
    # Missing prices leave the margin unreliable, so a verdict isn't trustworthy until
    # `priced` — the caller should surface that rather than trust `verdict`.
    missing_material_prices: tuple[int, ...]
    # False when the product has no sell price at the reference market, so its value
    # (and therefore the margin) is unknown — the build is still listed, just unvalued.
    product_priced: bool = True
    # The per-material breakdown (the bill of materials), for a build's detail view.
    materials: tuple[MaterialLine, ...] = ()
    # Cost to CRAFT: each material taken at its cheapest source (buy vs self-build).
    craft_cost: float = 0.0
    # Materials with no source price at all (neither buyable nor buildable) — leaves
    # `craft_cost` partial, the crafting analogue of `missing_material_prices`.
    craft_missing_prices: tuple[int, ...] = ()

    @property
    def margin(self) -> float:
        """Net sale value minus material cost — the profit of building over buying."""
        return self.net_product_value - self.material_cost

    @property
    def margin_fraction(self) -> float | None:
        """Margin as a fraction of material cost (return on the build), or None when
        there's nothing to divide by."""
        return self.margin / self.material_cost if self.material_cost > 0 else None

    @property
    def savings(self) -> float:
        """How much self-sourcing the materials saves over buying them all outright —
        the buy-everything cost minus the cheapest-source craft cost."""
        return self.material_cost - self.craft_cost

    @property
    def savings_fraction(self) -> float | None:
        """Savings as a fraction of the buy-everything cost, or None with nothing to
        divide by."""
        return self.savings / self.material_cost if self.material_cost > 0 else None

    @property
    def craft_priced(self) -> bool:
        """Whether every material had at least one source price, so ``craft_cost`` is
        complete. The savings comparison is only trustworthy when this and no material
        was missing a *buy* price (``missing_material_prices`` empty)."""
        return not self.craft_missing_prices

    @property
    def priced(self) -> bool:
        """Whether the product and every material had a price — i.e. the margin is
        complete and the verdict trustworthy."""
        return self.product_priced and not self.missing_material_prices

    @property
    def verdict(self) -> str:
        """BUILD when building profits over buying, else BUY. Only meaningful when
        ``priced`` (an incomplete cost can flip it)."""
        return "BUILD" if self.margin > 0 else "BUY"


def adjusted_material_quantity(base_quantity: int, runs: int, material_efficiency: int) -> int:
    """Units of a material a job actually consumes, after the blueprint's ME.

    Follows EVE's job formula ``max(runs, ceil(round(base * runs * (1 - ME/100), 2)))``
    -- ME never takes a material below one unit per run. Structure/rig bonuses are not
    modelled yet (a documented simplification); they'd multiply into the same factor."""
    reduced = base_quantity * runs * (1 - material_efficiency / 100)
    return max(runs, math.ceil(round(reduced, 2)))


# Cap on how deep the self-build recursion follows a chain of sub-components before it
# gives up and prices a material by buying — a safety bound, not a real-tree limit.
_MAX_BUILD_DEPTH = 5


def build_unit_cost(
    product_type_id: int,
    recipes: Mapping[int, Recipe],
    material_prices: Mapping[int, float],
    refine_prices: Mapping[int, float] | None = None,
) -> float | None:
    """Per-unit cost to manufacture ``product_type_id`` from its cheapest-sourced
    inputs, or None if it has no recipe here or any input can't be priced. Pure.

    ``recipes`` maps a product type id to the recipe that makes it (the transitive
    closure the caller has resolved — typically only components the character can build).
    Each input is costed at the cheapest of buying it, self-building it, or refining ore
    into it (``refine_prices``); sub-builds assume ME 0 (the sub-blueprint's researched
    ME isn't threaded through here), a conservative estimate. Recursion is cycle-guarded
    and depth-capped."""
    return _build_unit_cost(
        product_type_id,
        recipes,
        material_prices,
        refine_prices or {},
        _MAX_BUILD_DEPTH,
        frozenset(),
    )


def _build_unit_cost(
    product_type_id: int,
    recipes: Mapping[int, Recipe],
    material_prices: Mapping[int, float],
    refine_prices: Mapping[int, float],
    depth: int,
    stack: frozenset[int],
) -> float | None:
    recipe = recipes.get(product_type_id)
    if recipe is None or depth <= 0 or product_type_id in stack or recipe.product_quantity <= 0:
        return None
    inner = stack | {product_type_id}
    total = 0.0
    for material in recipe.materials:
        quantity = adjusted_material_quantity(material.quantity, 1, 0)  # sub-builds at ME 0
        buy = material_prices.get(material.type_id)
        refine = refine_prices.get(material.type_id)
        build = _build_unit_cost(
            material.type_id, recipes, material_prices, refine_prices, depth - 1, inner
        )
        unit = _cheapest(buy, build, refine)
        if unit is None:
            return None  # an input can't be sourced -> this build cost is unknown
        total += quantity * unit
    return total / recipe.product_quantity


def analyze_build(
    recipe: Recipe,
    *,
    material_efficiency: int,
    material_prices: Mapping[int, float],
    product_price: float | None,
    sales_tax: float = 0.0,
    broker_fee: float = 0.0,
    runs: int = 1,
    recipes: Mapping[int, Recipe] | None = None,
    refine_prices: Mapping[int, float] | None = None,
) -> BuildAnalysis:
    """Cost a build two ways — buying every material, and crafting each at its cheapest
    source — and (if the product is priced) compare it to buying the product. Pure.

    ``material_prices`` is the unit buy cost per material type; ``product_price`` the
    unit sale value of the output (``None`` if it isn't sold at the reference market —
    the build is still returned, just unvalued). ``recipes`` (product id -> recipe) lets
    each material be self-built when that's cheaper than buying it, and ``refine_prices``
    (mineral id -> unit cost of refining ore into it) lets it be mined-and-refined; omit
    both for a plain buy-only costing. Sale fees (``sales_tax`` + ``broker_fee``) apply to
    the product side. Prices, recipes and fees are policy handed in by the caller — the
    engine is just arithmetic, so it stays deterministic and reusable."""
    recipe_map = recipes or {}
    refine_map = refine_prices or {}
    material_cost = 0.0
    craft_cost = 0.0
    missing: list[int] = []
    craft_missing: list[int] = []
    lines: list[MaterialLine] = []
    for material in recipe.materials:
        quantity = adjusted_material_quantity(material.quantity, runs, material_efficiency)
        price = material_prices.get(material.type_id)
        build = build_unit_cost(material.type_id, recipe_map, material_prices, refine_map)
        refine = refine_map.get(material.type_id)
        line = MaterialLine(material.type_id, quantity, price, build, refine)
        lines.append(line)
        if price is None:
            missing.append(material.type_id)
        else:
            material_cost += quantity * price
        best = line.best_line_cost
        if best is None:
            craft_missing.append(material.type_id)
        else:
            craft_cost += best
    materials = tuple(lines)

    if product_price is None:
        return BuildAnalysis(
            runs=runs,
            material_cost=material_cost,
            product_value=0.0,
            net_product_value=0.0,
            missing_material_prices=tuple(missing),
            product_priced=False,
            materials=materials,
            craft_cost=craft_cost,
            craft_missing_prices=tuple(craft_missing),
        )
    product_value = recipe.product_quantity * runs * product_price
    net_product_value = product_value * (1 - sales_tax - broker_fee)
    return BuildAnalysis(
        runs=runs,
        material_cost=material_cost,
        product_value=product_value,
        net_product_value=net_product_value,
        missing_material_prices=tuple(missing),
        materials=materials,
        craft_cost=craft_cost,
        craft_missing_prices=tuple(craft_missing),
    )
