"""The SDE query layer reads a blueprint's manufacturing recipe from a local SQLite."""

import sqlite3
from pathlib import Path

import pytest

from evetrader.data.sde import SdeDatabase, SdeError


def _make_sde(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE industryActivityProducts "
        "(typeID INT, activityID INT, productTypeID INT, quantity INT)"
    )
    conn.execute(
        "CREATE TABLE industryActivityMaterials "
        "(typeID INT, activityID INT, materialTypeID INT, quantity INT)"
    )
    # Rifter Blueprint (938) manufactures one Rifter (587) from tritanium + pyerite.
    conn.execute("INSERT INTO industryActivityProducts VALUES (938, 1, 587, 1)")
    conn.executemany(
        "INSERT INTO industryActivityMaterials VALUES (?, ?, ?, ?)",
        [
            (938, 1, 34, 32000),
            (938, 1, 35, 6000),
            (938, 3, 34, 999),  # a research-activity row that must be excluded
        ],
    )
    conn.commit()
    conn.close()


def test_manufacturing_recipe_reads_product_and_materials(tmp_path: Path) -> None:
    path = tmp_path / "sde.sqlite"
    _make_sde(path)
    sde = SdeDatabase(path)
    recipe = sde.manufacturing_recipe(938)
    assert recipe is not None
    assert recipe.product_type_id == 587
    assert recipe.product_quantity == 1
    # Only the manufacturing (activityID 1) materials, not the research row.
    assert [(m.type_id, m.quantity) for m in recipe.materials] == [(34, 32000), (35, 6000)]
    sde.close()


def test_manufacturing_recipe_none_when_not_buildable(tmp_path: Path) -> None:
    path = tmp_path / "sde.sqlite"
    _make_sde(path)
    sde = SdeDatabase(path)
    assert sde.manufacturing_recipe(999) is None  # no product row for this id
    sde.close()


def test_missing_database_raises_sde_error(tmp_path: Path) -> None:
    with pytest.raises(SdeError):
        SdeDatabase(tmp_path / "absent.sqlite")
