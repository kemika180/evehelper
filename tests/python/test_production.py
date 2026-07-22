"""The pure build-vs-buy engine: ME-adjusted material cost vs fee-adjusted sale value."""

from pytest import approx

from evetrader.market.production import (
    Recipe,
    RecipeMaterial,
    adjusted_material_quantity,
    analyze_build,
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


def test_margin_fraction_is_none_without_material_cost() -> None:
    empty = Recipe(blueprint_type_id=1, product_type_id=2, product_quantity=1, materials=())
    analysis = analyze_build(empty, material_efficiency=0, material_prices={}, product_price=50.0)
    assert analysis.margin_fraction is None
    assert analysis.priced is True  # no materials to miss a price for
