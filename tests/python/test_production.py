"""The pure build-vs-buy engine: ME-adjusted material cost vs fee-adjusted sale value."""

from pytest import approx

from evetrader.market.production import (
    MaterialLine,
    Recipe,
    RecipeMaterial,
    adjusted_material_quantity,
    analyze_build,
    build_unit_cost,
)


def _recipe() -> Recipe:
    return Recipe(
        blueprint_type_id=938,
        product_type_id=587,
        product_quantity=1,
        materials=(RecipeMaterial(34, 100), RecipeMaterial(35, 50)),
    )


def test_adjusted_material_quantity_applies_me_and_floors_at_runs() -> None:
    assert adjusted_material_quantity(100, runs=1, material_efficiency=0) == 100
    assert adjusted_material_quantity(100, runs=1, material_efficiency=10) == 90
    # ME never drops a material below one unit per run.
    assert adjusted_material_quantity(1, runs=10, material_efficiency=10) == 10


def test_analyze_build_costs_materials_and_applies_sale_fees() -> None:
    analysis = analyze_build(
        _recipe(),
        material_efficiency=10,
        material_prices={34: 5.0, 35: 10.0},
        product_price=2000.0,
        sales_tax=0.05,
        broker_fee=0.03,
    )
    # 34: ceil(100*0.9)=90 @5 = 450 ; 35: ceil(50*0.9)=45 @10 = 450 -> 900
    assert analysis.material_cost == 900.0
    assert analysis.product_value == 2000.0
    assert analysis.net_product_value == approx(1840.0)  # 8% total fees
    assert analysis.margin == approx(940.0)
    assert analysis.priced is True
    assert analysis.verdict == "BUILD"


def test_analyze_build_flags_missing_material_prices() -> None:
    analysis = analyze_build(
        _recipe(),
        material_efficiency=0,
        material_prices={34: 5.0},  # no price for 35
        product_price=2000.0,
    )
    assert analysis.missing_material_prices == (35,)
    assert analysis.priced is False
    assert analysis.material_cost == 100 * 5.0  # only the priced material counted


def test_analyze_build_scales_with_runs_and_calls_a_loss_a_buy() -> None:
    analysis = analyze_build(
        _recipe(),
        material_efficiency=0,
        material_prices={34: 5.0, 35: 10.0},
        product_price=100.0,  # cheap product -> building loses
        runs=3,
    )
    assert analysis.material_cost == 3 * (100 * 5.0 + 50 * 10.0)  # 3000
    assert analysis.product_value == 3 * 100.0  # 300
    assert analysis.margin < 0
    assert analysis.verdict == "BUY"


def test_analyze_build_returns_me_adjusted_material_breakdown() -> None:
    analysis = analyze_build(
        _recipe(),
        material_efficiency=10,
        material_prices={34: 5.0, 35: 10.0},
        product_price=2000.0,
    )
    lines = [(m.type_id, m.quantity, m.unit_price, m.line_cost) for m in analysis.materials]
    assert lines == [(34, 90, 5.0, 450.0), (35, 45, 10.0, 450.0)]


def test_material_breakdown_marks_unpriced_lines() -> None:
    analysis = analyze_build(
        _recipe(),
        material_efficiency=0,
        material_prices={34: 5.0},  # no price for 35
        product_price=2000.0,
    )
    unpriced = next(m for m in analysis.materials if m.type_id == 35)
    assert unpriced.unit_price is None
    assert unpriced.line_cost is None
    assert unpriced.quantity == 50  # quantity still reported


def test_margin_fraction_is_none_without_material_cost() -> None:
    empty = Recipe(blueprint_type_id=1, product_type_id=2, product_quantity=1, materials=())
    analysis = analyze_build(empty, material_efficiency=0, material_prices={}, product_price=50.0)
    assert analysis.margin_fraction is None
    assert analysis.priced is True  # no materials to miss a price for


# --- self-source (craft cost) ------------------------------------------------

# product 100 <- 2x component 200 ; component 200 <- 5x mineral 300.
_COMPONENT_RECIPE = Recipe(
    blueprint_type_id=201, product_type_id=200, product_quantity=1, materials=(RecipeMaterial(300, 5),)
)
_TOP_RECIPE = Recipe(
    blueprint_type_id=101, product_type_id=100, product_quantity=1, materials=(RecipeMaterial(200, 2),)
)
_RECIPES = {200: _COMPONENT_RECIPE}


def test_build_unit_cost_recurses_through_subcomponents() -> None:
    # 200 costs 5 units of 300 @2 = 10; 100 needs 2 of those -> per-unit build cost 20.
    assert build_unit_cost(200, _RECIPES, {300: 2.0}) == 10.0
    assert build_unit_cost(100, {**_RECIPES, 100: _TOP_RECIPE}, {300: 2.0}) == 20.0


def test_build_unit_cost_is_none_without_a_recipe_or_price() -> None:
    assert build_unit_cost(999, _RECIPES, {300: 2.0}) is None  # no recipe for 999
    assert build_unit_cost(200, _RECIPES, {}) is None  # mineral 300 has no price


def test_build_unit_cost_guards_cycles() -> None:
    # A recipe that (absurdly) consumes itself must not recurse forever.
    loop = Recipe(blueprint_type_id=1, product_type_id=5, product_quantity=1, materials=(RecipeMaterial(5, 1),))
    assert build_unit_cost(5, {5: loop}, {}) is None


def test_analyze_build_self_sources_a_material_when_cheaper_to_build() -> None:
    # Component 200 buys at 100 but builds (from 300) at 10 -> craft picks build.
    analysis = analyze_build(
        _TOP_RECIPE,
        material_efficiency=0,
        material_prices={200: 100.0, 300: 2.0},
        product_price=None,
        recipes=_RECIPES,
    )
    line = analysis.materials[0]
    assert line.unit_price == 100.0
    assert line.build_unit_cost == 10.0
    assert line.source == "build"
    assert line.best_line_cost == 20.0  # 2 units @ built cost 10
    assert analysis.material_cost == 200.0  # buy-everything: 2 @ 100
    assert analysis.craft_cost == 20.0  # self-source: 2 @ 10
    assert analysis.savings == 180.0
    assert analysis.craft_priced is True


def test_analyze_build_buys_a_material_when_cheaper_than_building() -> None:
    analysis = analyze_build(
        _TOP_RECIPE,
        material_efficiency=0,
        material_prices={200: 8.0, 300: 2.0},  # buy 200 @8 beats build @10
        product_price=None,
        recipes=_RECIPES,
    )
    line = analysis.materials[0]
    assert line.source == "buy"
    assert analysis.craft_cost == 16.0  # 2 @ 8
    assert analysis.savings == 0.0  # buying was already cheapest


def test_analyze_build_refines_a_material_when_cheapest() -> None:
    # Component 200 buys @100, builds @10 (from 300 @2), but refines @6 -> refine wins.
    analysis = analyze_build(
        _TOP_RECIPE,
        material_efficiency=0,
        material_prices={200: 100.0, 300: 2.0},
        product_price=None,
        recipes=_RECIPES,
        refine_prices={200: 6.0},
    )
    line = analysis.materials[0]
    assert line.refine_unit_cost == 6.0
    assert line.source == "refine"
    assert line.best_line_cost == 12.0  # 2 units @ 6
    assert analysis.craft_cost == 12.0


def test_refine_feeds_into_sub_build_costing() -> None:
    # Building 200 needs 5x mineral 300: buy 300 @2, but refine it @1 -> build uses refine.
    unit = build_unit_cost(200, _RECIPES, {300: 2.0}, {300: 1.0})
    assert unit == 5.0  # 5 units of 300 @ the refined price of 1


def test_source_prefers_buying_on_a_tie() -> None:
    line = MaterialLine(1, 10, unit_price=5.0, build_unit_cost=5.0, refine_unit_cost=5.0)
    assert line.source == "buy"


def test_craft_cost_flags_a_material_with_no_source() -> None:
    analysis = analyze_build(
        _TOP_RECIPE,
        material_efficiency=0,
        material_prices={},  # 200 not sold, and 300 (its input) not priced -> unbuildable
        product_price=None,
        recipes=_RECIPES,
    )
    assert analysis.craft_missing_prices == (200,)
    assert analysis.craft_priced is False
    assert analysis.materials[0].source == "?"
