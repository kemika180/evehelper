"""build_asset_tree reconstructs the nested hierarchy from ESI's flat asset list."""

from evetrader.data.assets import build_asset_tree, nameable_item_ids
from evetrader.esi.models import Asset


def _asset(
    item_id: int,
    type_id: int,
    location_id: int,
    *,
    qty: int = 1,
    flag: str = "Hangar",
    singleton: bool = False,
) -> Asset:
    return Asset(
        item_id=item_id,
        type_id=type_id,
        quantity=qty,
        location_id=location_id,
        location_flag=flag,
        location_type="station",
        is_singleton=singleton,
    )


def test_items_nest_under_their_container_grouped_by_place() -> None:
    assets = [
        _asset(1, 34, 60003760, qty=1_000),  # a stack in a hangar
        _asset(2, 17363, 60003760, singleton=True),  # a container in the hangar
        _asset(3, 35, 2, qty=500, flag="Cargo"),  # a stack INSIDE that container
        _asset(4, 34, 60000001, qty=5),  # a stack at a different station
    ]
    tree = build_asset_tree(assets)

    assert [loc.location_id for loc in tree] == [60000001, 60003760]  # sorted by place
    home = next(loc for loc in tree if loc.location_id == 60003760)
    assert [node.item_id for node in home.items] == [1, 2]  # container item 3 is not top-level
    container = next(node for node in home.items if node.item_id == 2)
    assert [child.item_id for child in container.children] == [3]
    assert container.children[0].location_flag == "Cargo"


def test_nameable_ids_are_singleton_containers_and_ships() -> None:
    assets = [
        _asset(1, 34, 60003760, qty=1_000),  # a bare stack — not nameable
        _asset(2, 17363, 60003760, singleton=True),  # a container (holds item 3)
        _asset(3, 35, 2, qty=500),
        _asset(4, 24696, 60003760, singleton=True),  # an empty assembled ship — no contents
    ]
    tree = build_asset_tree(assets)
    # Only the container holds something; the empty ship and the stack are excluded.
    assert nameable_item_ids(tree) == [2]


def test_a_containment_cycle_terminates() -> None:
    # Two items each claiming to be inside the other: neither is a root place, so the
    # tree is empty — and, crucially, the build does not recurse forever.
    assets = [_asset(1, 34, 2), _asset(2, 34, 1)]
    assert build_asset_tree(assets) == []
