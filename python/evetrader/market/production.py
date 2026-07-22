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


@dataclass(frozen=True)
class BuildAnalysis:
    """The build-vs-buy verdict for one recipe at a given ME, run count, and prices."""

    runs: int
    material_cost: float  # total cost of the (priced) materials for `runs` runs
    product_value: float  # gross sale value of the output before fees
    net_product_value: float  # after sales tax + broker fee
    # Missing prices leave the material cost understated, so a verdict isn't trustworthy
    # until this is empty — the caller should surface that rather than trust `verdict`.
    missing_material_prices: tuple[int, ...]

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
    def priced(self) -> bool:
        """Whether every material had a price — i.e. the margin is complete."""
        return not self.missing_material_prices

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


def analyze_build(
    recipe: Recipe,
    *,
    material_efficiency: int,
    material_prices: Mapping[int, float],
    product_price: float,
    sales_tax: float = 0.0,
    broker_fee: float = 0.0,
    runs: int = 1,
) -> BuildAnalysis:
    """Cost a build and compare it to buying the product outright. Pure.

    ``material_prices`` is the unit acquisition cost per material type; ``product_price``
    the unit sale value of the output. Sale fees (``sales_tax`` + ``broker_fee``, as
    fractions) are applied to the product side. Prices and fees are policy handed in by
    the caller — the engine just arithmetic, so it stays deterministic and reusable."""
    material_cost = 0.0
    missing: list[int] = []
    for material in recipe.materials:
        price = material_prices.get(material.type_id)
        if price is None:
            missing.append(material.type_id)
            continue
        quantity = adjusted_material_quantity(material.quantity, runs, material_efficiency)
        material_cost += quantity * price

    product_value = recipe.product_quantity * runs * product_price
    net_product_value = product_value * (1 - sales_tax - broker_fee)
    return BuildAnalysis(
        runs=runs,
        material_cost=material_cost,
        product_value=product_value,
        net_product_value=net_product_value,
        missing_material_prices=tuple(missing),
    )
