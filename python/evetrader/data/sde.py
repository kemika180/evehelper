"""Query the local EVE SDE (Static Data Export) SQLite. Impure — local file I/O only,
no network.

The SDE holds static game data ESI doesn't expose — notably each blueprint's bill of
materials. It's large (~250 MB), so it's downloaded once (see ``sde_download.py``),
kept in the gitignored data dir, and queried read-only here. The pure production engine
(``market/production.py``) consumes the ``Recipe`` values this returns; it never
touches the database itself, keeping the analysis core free of I/O.

Manufacturing is the only activity read today; reactions/invention and cargo volumes
(for hauling) and PI schematics are the same database and slot in later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from evetrader.market.production import Recipe, RecipeMaterial

# industryActivity activityID for manufacturing (the SDE's stable activity codes).
_MANUFACTURING = 1
# invCategories categoryID for asteroid ore — the only reprocessing sources we treat as
# "refine" (modules/ships also reprocess into minerals, but that's salvage, not mining).
_ASTEROID_CATEGORY = 25
# invCategories categoryID for ammunition/charges (turret crystals, missiles, scripts…).
_CHARGE_CATEGORY = 8


@dataclass(frozen=True)
class OreYield:
    """One ore that reprocesses into a wanted mineral, and how much of that mineral a
    single unit of the ore yields before efficiency (``reprocess quantity / portionSize``)."""

    ore_type_id: int
    units_per_ore: float


class SdeError(Exception):
    """The SDE database is missing or unreadable."""


class SdeDatabase:
    """Read-only handle on the local SDE SQLite. Shareable — it never mutates."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise SdeError(f"SDE not found at {path}; run `evetrader sde` to download it")
        # Open immutable/read-only so a stray write can't corrupt the shared dump.
        self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def close(self) -> None:
        self._conn.close()

    def manufacturing_recipe(self, blueprint_type_id: int) -> Recipe | None:
        """The manufacturing recipe for a blueprint, or None if it can't be built
        (not a manufacturing blueprint, or absent from the SDE)."""
        product = self._conn.execute(
            "SELECT productTypeID, quantity FROM industryActivityProducts "
            "WHERE typeID = ? AND activityID = ?",
            (blueprint_type_id, _MANUFACTURING),
        ).fetchone()
        if product is None:
            return None
        materials = self._conn.execute(
            "SELECT materialTypeID, quantity FROM industryActivityMaterials "
            "WHERE typeID = ? AND activityID = ? ORDER BY materialTypeID",
            (blueprint_type_id, _MANUFACTURING),
        ).fetchall()
        return Recipe(
            blueprint_type_id=blueprint_type_id,
            product_type_id=int(product[0]),
            product_quantity=int(product[1]),
            materials=tuple(RecipeMaterial(int(row[0]), int(row[1])) for row in materials),
        )

    def recipe_for_product(self, product_type_id: int) -> Recipe | None:
        """The manufacturing recipe that yields ``product_type_id``, or None if nothing
        manufactures it. The reverse of :meth:`manufacturing_recipe` (which is keyed by
        blueprint) — used to ask "can I build this material myself?" when costing a
        build's inputs. If several blueprints make the same product, the first wins."""
        row = self._conn.execute(
            "SELECT typeID FROM industryActivityProducts "
            "WHERE productTypeID = ? AND activityID = ? ORDER BY typeID LIMIT 1",
            (product_type_id, _MANUFACTURING),
        ).fetchone()
        if row is None:
            return None
        return self.manufacturing_recipe(int(row[0]))

    def charge_type_ids(self, type_ids: set[int]) -> frozenset[int]:
        """Which of these types are ammunition/charges (category 8). ESI gives a loaded
        charge the same slot flag as the weapon holding it, so the asset view needs this
        to tell loaded ammo apart from the module itself."""
        if not type_ids:
            return frozenset()
        placeholders = ",".join("?" * len(type_ids))
        rows = self._conn.execute(
            "SELECT t.typeID FROM invTypes t "
            "JOIN invGroups g ON g.groupID = t.groupID "
            f"WHERE t.typeID IN ({placeholders}) AND g.categoryID = ?",
            (*sorted(type_ids), _CHARGE_CATEGORY),
        ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def ore_sources(self, mineral_type_ids: set[int]) -> dict[int, list[OreYield]]:
        """For each wanted mineral, the asteroid ores that reprocess into it and the
        per-unit yield of that mineral. Ores/items outside the asteroid category (modules,
        ships) are excluded — only mining-and-refining counts as "refine" here. Minerals
        no ore produces are simply absent from the result."""
        if not mineral_type_ids:
            return {}
        placeholders = ",".join("?" * len(mineral_type_ids))
        rows = self._conn.execute(
            "SELECT m.materialTypeID, m.typeID, m.quantity, t.portionSize "
            "FROM invTypeMaterials m "
            "JOIN invTypes t ON t.typeID = m.typeID "
            "JOIN invGroups g ON g.groupID = t.groupID "
            f"WHERE m.materialTypeID IN ({placeholders}) "
            "AND g.categoryID = ? AND t.portionSize > 0",
            (*sorted(mineral_type_ids), _ASTEROID_CATEGORY),
        ).fetchall()
        sources: dict[int, list[OreYield]] = {}
        for mineral_id, ore_id, quantity, portion in rows:
            if int(portion) <= 0:
                continue
            sources.setdefault(int(mineral_id), []).append(
                OreYield(ore_type_id=int(ore_id), units_per_ore=int(quantity) / int(portion))
            )
        return sources
