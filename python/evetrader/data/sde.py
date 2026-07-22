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
from pathlib import Path

from evetrader.market.production import Recipe, RecipeMaterial

# industryActivity activityID for manufacturing (the SDE's stable activity codes).
_MANUFACTURING = 1


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
