"""Reconstruct the nested asset tree from ESI's flat asset list. Pure.

ESI returns assets as a flat list: each row's ``location_id`` is either a *place*
(a station, structure, or solar system) or another asset's ``item_id`` — meaning
the row sits inside that container or ship. This rebuilds the hierarchy: places at
the top, then their items, then whatever is packed inside those items, recursively.

Deterministic given the flat list; no I/O. Names are resolved separately (they need
the network) and applied at the display layer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from evetrader.esi.models import Asset


@dataclass(frozen=True)
class AssetNode:
    """One asset and whatever is nested inside it (a container's or ship's contents)."""

    item_id: int
    type_id: int
    quantity: int
    location_flag: str  # where it sits in its parent: "Hangar", "Cargo", "HighSlot0"…
    is_singleton: bool  # an unpackaged, single item (a ship or fitted module), not a stack
    children: tuple[AssetNode, ...]


@dataclass(frozen=True)
class AssetLocation:
    """A place the character has assets — a station, structure, or solar system."""

    location_id: int
    items: tuple[AssetNode, ...]


def _sort_key(asset: Asset) -> tuple[int, int]:
    return (asset.type_id, asset.item_id)


def build_asset_tree(assets: list[Asset]) -> list[AssetLocation]:
    """Group top-level assets by place and nest contained items under their parent."""
    by_item = {asset.item_id: asset for asset in assets}
    children: dict[int, list[Asset]] = defaultdict(list)
    roots: dict[int, list[Asset]] = defaultdict(list)
    for asset in assets:
        bucket = children if asset.location_id in by_item else roots
        bucket[asset.location_id].append(asset)

    def build(asset: Asset, ancestors: frozenset[int]) -> AssetNode:
        # `ancestors` guards against a pathological containment cycle (ESI shouldn't
        # produce one, but recursion must terminate regardless).
        kids: tuple[AssetNode, ...] = ()
        if asset.item_id not in ancestors:
            deeper = ancestors | {asset.item_id}
            kids = tuple(build(c, deeper) for c in sorted(children[asset.item_id], key=_sort_key))
        return AssetNode(
            item_id=asset.item_id,
            type_id=asset.type_id,
            quantity=asset.quantity,
            location_flag=asset.location_flag,
            is_singleton=asset.is_singleton,
            children=kids,
        )

    locations = [
        AssetLocation(
            location_id=location_id,
            items=tuple(build(asset, frozenset()) for asset in sorted(items, key=_sort_key)),
        )
        for location_id, items in roots.items()
    ]
    return sorted(locations, key=lambda location: location.location_id)


def nameable_item_ids(locations: list[AssetLocation]) -> list[int]:
    """Item ids of singleton containers/ships (things that hold items and can carry a
    player-assigned name) — the ids worth asking POST /assets/names/ about."""
    ids: list[int] = []

    def walk(nodes: tuple[AssetNode, ...]) -> None:
        for node in nodes:
            if node.children and node.is_singleton:
                ids.append(node.item_id)
            walk(node.children)

    for location in locations:
        walk(location.items)
    return ids
