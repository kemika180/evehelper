"""The pure production engine: build-vs-buy costing, and the byproduct-aware self-source plan."""

from pytest import approx

from evetrader.market.production import (
    BlueprintNeeded,
    BuyLine,
    MineableOre,
    MineLine,
    Recipe,
    RecipeMaterial,
    RequiredMaterial,
    RequiredSkill,
    SelfSourcePlan,
    adjusted_material_quantity,
    build_source_tree,
    collect_needs,
    plan_ore_mining,
)


# --- material-efficiency helper ----------------------------------------------


def test_adjusted_material_quantity_applies_me_and_floors_at_runs() -> None:
    assert adjusted_material_quantity(100, runs=1, material_efficiency=0) == 100
    assert adjusted_material_quantity(100, runs=1, material_efficiency=10) == 90
    assert adjusted_material_quantity(1, runs=10, material_efficiency=10) == 10


# --- self-source build tree --------------------------------------------------

# product 100 <- (bp 101) 2x component 200 ; component 200 <- (bp 201) 5x mineral 300.
_COMPONENT = Recipe(
    blueprint_type_id=201, product_type_id=200, product_quantity=1, materials=(RecipeMaterial(300, 5),)
)
_TOP = Recipe(
    blueprint_type_id=101, product_type_id=100, product_quantity=1, materials=(RecipeMaterial(200, 2),)
)


def test_source_tree_classifies_build_mine_and_buy() -> None:
    # Owns the component blueprint (200) and mineral 300 is refinable.
    tree = build_source_tree(
        _TOP, material_efficiency=0, refinable=frozenset({300}), recipes={200: _COMPONENT}
    )
    assert (tree.type_id, tree.source, tree.runs) == (100, "build", 1)
    component = tree.children[0]
    assert (component.type_id, component.source, component.runs) == (200, "build", 2)
    mineral = component.children[0]
    assert (mineral.type_id, mineral.source, mineral.quantity) == (300, "mine", 10)  # 2 runs x 5


def test_source_tree_buys_a_component_without_an_owned_blueprint() -> None:
    # No owned recipe for 200 and it isn't a mineral -> it must be bought.
    tree = build_source_tree(_TOP, material_efficiency=0, refinable=frozenset(), recipes={})
    component = tree.children[0]
    assert (component.type_id, component.source) == (200, "buy")
    needs = collect_needs(tree)
    assert needs.minerals == {}
    assert needs.buy == (BuyLine(200, 2),)
    assert [(r.product_type_id, r.runs) for r in needs.build] == [(100, 1)]


def test_collect_needs_aggregates_minerals_builds_and_buys() -> None:
    tree = build_source_tree(
        _TOP, material_efficiency=0, refinable=frozenset({300}), recipes={200: _COMPONENT}
    )
    needs = collect_needs(tree)
    assert needs.minerals == {300: 10}
    assert [(r.product_type_id, r.runs) for r in needs.build] == [(100, 1), (200, 2)]
    assert needs.buy == ()


# --- byproduct-aware mining plan ---------------------------------------------


def test_plan_ore_mining_credits_byproducts_of_the_rare_ore() -> None:
    # Rare mineral 40 comes from ore 900, which ALSO yields common mineral 34 as a byproduct.
    # Common mineral 34's own ore is 901. Mining for the rare one first covers some of 34.
    rare_ore = MineableOre(900, "nullsec", 16.0, yields={40: 1.0, 34: 2.0})
    common_ore = MineableOre(901, "highsec", 0.1, yields={34: 4.0})
    plan = plan_ore_mining({40: 10, 34: 100}, {40: rare_ore, 34: common_ore}, rarity_order=(40, 34))
    by_ore = {line.ore_type_id: line for line in plan}
    # 10 units of ore 900 (for mineral 40) -> yields 20 of mineral 34 as byproduct.
    assert by_ore[900].quantity == 10
    assert by_ore[900].volume == approx(160.0)  # 10 x 16 m³
    # Only 80 of the 100 mineral 34 remains -> ceil(80/4) = 20 of ore 901 (not 25).
    assert by_ore[901].quantity == 20


def test_plan_ore_mining_skips_a_mineral_fully_covered_by_byproducts() -> None:
    rare_ore = MineableOre(900, "nullsec", 1.0, yields={40: 1.0, 34: 50.0})
    common_ore = MineableOre(901, "highsec", 0.1, yields={34: 4.0})
    # Mining for 40 yields plenty of 34 -> ore 901 is never mined.
    plan = plan_ore_mining({40: 10, 34: 100}, {40: rare_ore, 34: common_ore}, rarity_order=(40, 34))
    assert [line.ore_type_id for line in plan] == [900]


def test_plan_ore_mining_is_empty_without_needs() -> None:
    assert plan_ore_mining({}, {}, rarity_order=()) == ()


def test_self_source_plan_reports_missing_skills() -> None:
    plan = SelfSourcePlan(
        materials=(RequiredMaterial(200, 1, 5.0, 1.0, "buildable"),),
        mine=(),
        build=(),
        buy=(),
        required_skills=(
            RequiredSkill(100, 3, 3, None),  # met
            RequiredSkill(101, 4, 2, 5400.0),  # missing, ~1h30m to train
        ),
        blueprints=(BlueprintNeeded(201, 200, 2_000_000.0),),
    )
    assert [skill.type_id for skill in plan.missing_skills] == [101]
    assert plan.required_skills[0].met is True and plan.required_skills[1].met is False


def test_mine_line_carries_haulable_volume() -> None:
    ore = MineableOre(900, "highsec", 0.1, yields={34: 4.0})
    (line,) = plan_ore_mining({34: 40}, {34: ore}, rarity_order=(34,))
    assert isinstance(line, MineLine)
    assert line.quantity == 10  # ceil(40 / 4)
    assert line.volume == approx(1.0)  # 10 x 0.1 m³
    assert line.location == "highsec"


def test_source_tree_guards_cycles() -> None:
    loop = Recipe(
        blueprint_type_id=1, product_type_id=5, product_quantity=1, materials=(RecipeMaterial(5, 1),)
    )
    tree = build_source_tree(loop, material_efficiency=0, refinable=frozenset(), recipes={5: loop})
    assert tree.source == "build"
    assert tree.children[0].source == "buy"  # the self-referential input bottoms out
