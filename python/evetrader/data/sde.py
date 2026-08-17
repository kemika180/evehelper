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

# industryActivity activityIDs (the SDE's stable activity codes).
_MANUFACTURING = 1
_REACTION = 11
# dgmTypeAttributes attributeID naming an ore's reprocessing skill (the tiered "… Ore
# Processing" skill governing its yield). Read per ore rather than hardcoding a mapping.
_REPROCESSING_SKILL_ATTR = 790
# invCategories categoryID for asteroid ore — the only reprocessing sources we treat as
# "refine" (modules/ships also reprocess into minerals, but that's salvage, not mining).
_ASTEROID_CATEGORY = 25
# invCategories categoryID for ammunition/charges (turret crystals, missiles, scripts…).
_CHARGE_CATEGORY = 8


@dataclass(frozen=True)
class OreYield:
    """One ore that reprocesses into a wanted mineral, and how much of that mineral a
    single unit of the ore yields before efficiency (``reprocess quantity / portionSize``).
    ``name`` carries the ore's SDE name so the refine model can collapse same-family
    variants (e.g. drop "Brimful Zeolites" when plain "Zeolites" is also available), and
    ``volume`` the ore's m³ per unit (for the hauling / trips estimate)."""

    ore_type_id: int
    units_per_ore: float
    name: str = ""
    volume: float = 0.0


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

    def manufacturing_skills(self, blueprint_type_id: int) -> list[tuple[int, int]]:
        """The (skill type id, required level) pairs a blueprint's manufacturing job
        needs — used to tell whether the character can actually build it, and which skill
        (and level) would unlock the build if not. Empty when the blueprint has no
        manufacturing skill requirements or isn't in the SDE."""
        rows = self._conn.execute(
            "SELECT skillID, level FROM industryActivitySkills "
            "WHERE typeID = ? AND activityID = ? ORDER BY skillID",
            (blueprint_type_id, _MANUFACTURING),
        ).fetchall()
        return [(int(skill_id), int(level)) for skill_id, level in rows]

    def reaction_products(self, type_ids: set[int]) -> frozenset[int]:
        """Which of these types are produced by a reaction (industryActivity activityID 11) —
        so the recipe can note a bought material is reaction-makeable rather than pure buy."""
        if not type_ids:
            return frozenset()
        placeholders = ",".join("?" * len(type_ids))
        rows = self._conn.execute(
            "SELECT productTypeID FROM industryActivityProducts "
            f"WHERE activityID = ? AND productTypeID IN ({placeholders})",
            (_REACTION, *sorted(type_ids)),
        ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def pi_products(self, type_ids: set[int]) -> frozenset[int]:
        """Which of these types are planetary-industry outputs (a ``planetSchematicsTypeMap``
        row with ``isInput = 0``) — so the recipe can note a bought material is PI-makeable."""
        if not type_ids:
            return frozenset()
        placeholders = ",".join("?" * len(type_ids))
        rows = self._conn.execute(
            "SELECT typeID FROM planetSchematicsTypeMap "
            f"WHERE isInput = 0 AND typeID IN ({placeholders})",
            tuple(sorted(type_ids)),
        ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def volumes(self, type_ids: set[int]) -> dict[int, float]:
        """Packaged m³ per unit for each type (from ``invTypes.volume``) — for the materials'
        volume and the mining haul estimate. Types absent from the SDE are simply omitted."""
        if not type_ids:
            return {}
        placeholders = ",".join("?" * len(type_ids))
        rows = self._conn.execute(
            f"SELECT typeID, volume FROM invTypes WHERE typeID IN ({placeholders})",
            tuple(sorted(type_ids)),
        ).fetchall()
        return {int(type_id): float(volume) for type_id, volume in rows if volume is not None}

    def station_security(self, station_id: int) -> float | None:
        """The security status of an NPC station's solar system, or None if the station
        isn't in the SDE (a player structure, which the SDE doesn't carry). Lets the refine
        model bias ore options toward what's mineable at the character's home security."""
        row = self._conn.execute(
            "SELECT s.security FROM staStations st "
            "JOIN mapSolarSystems s ON s.solarSystemID = st.solarSystemID "
            "WHERE st.stationID = ?",
            (station_id,),
        ).fetchone()
        return float(row[0]) if row is not None and row[0] is not None else None

    def ore_reprocessing_skills(self, ore_type_ids: set[int]) -> dict[int, int]:
        """For each ore, the reprocessing skill type id that governs its yield (SDE
        attribute 790). Ores without the attribute are simply absent. Lets the refine
        model raise yield by the character's actual ore-specific processing level."""
        if not ore_type_ids:
            return {}
        placeholders = ",".join("?" * len(ore_type_ids))
        rows = self._conn.execute(
            "SELECT typeID, COALESCE(valueInt, valueFloat) FROM dgmTypeAttributes "
            f"WHERE attributeID = ? AND typeID IN ({placeholders})",
            (_REPROCESSING_SKILL_ATTR, *sorted(ore_type_ids)),
        ).fetchall()
        return {int(ore_id): int(skill_id) for ore_id, skill_id in rows if skill_id is not None}

    def ore_sources(self, mineral_type_ids: set[int]) -> dict[int, list[OreYield]]:
        """For each wanted mineral, the **base** asteroid ores that reprocess into it and the
        per-unit yield of that mineral. Ores/items outside the asteroid category (modules,
        ships) are excluded — only mining-and-refining counts as "refine" here.

        Only **base** ores are returned (moon ores included, tagged by location downstream so
        the player can judge accessibility): the compressed / batch-compressed forms and the
        graded variants (``… II-Grade`` … ``IV-Grade`` and the newbie-area ``0-Grade``) and ice
        ``Isotope`` forms are filtered out, since higher grades are rare and the base rock is
        what a miner actually finds. Same-name quality *prefixes* (Brimful, Glistening, …) can't
        be told apart in SQL and are collapsed downstream by name. Minerals no ore produces are
        simply absent from the result."""
        if not mineral_type_ids:
            return {}
        placeholders = ",".join("?" * len(mineral_type_ids))
        rows = self._conn.execute(
            "SELECT m.materialTypeID, m.typeID, m.quantity, t.portionSize, t.typeName, t.volume "
            "FROM invTypeMaterials m "
            "JOIN invTypes t ON t.typeID = m.typeID "
            "JOIN invGroups g ON g.groupID = t.groupID "
            f"WHERE m.materialTypeID IN ({placeholders}) "
            "AND g.categoryID = ? AND t.portionSize > 0 "
            "AND t.typeName NOT LIKE '%Compressed%' "
            "AND t.typeName NOT LIKE '%-Grade%' "
            "AND t.typeName NOT LIKE '%Isotope%'",
            (*sorted(mineral_type_ids), _ASTEROID_CATEGORY),
        ).fetchall()
        sources: dict[int, list[OreYield]] = {}
        for mineral_id, ore_id, quantity, portion, name, volume in rows:
            if int(portion) <= 0:
                continue
            sources.setdefault(int(mineral_id), []).append(
                OreYield(
                    ore_type_id=int(ore_id),
                    units_per_ore=int(quantity) / int(portion),
                    name=str(name),
                    volume=float(volume) if volume is not None else 0.0,
                )
            )
        return sources
