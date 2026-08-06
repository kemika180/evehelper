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


def test_recipe_for_product_reverse_looks_up_the_blueprint(tmp_path: Path) -> None:
    path = tmp_path / "sde.sqlite"
    _make_sde(path)
    sde = SdeDatabase(path)
    # The Rifter (587) is manufactured by blueprint 938 — found via its product id.
    recipe = sde.recipe_for_product(587)
    assert recipe is not None
    assert recipe.blueprint_type_id == 938
    assert [(m.type_id, m.quantity) for m in recipe.materials] == [(34, 32000), (35, 6000)]
    # A material that nothing manufactures (tritanium) has no recipe.
    assert sde.recipe_for_product(34) is None
    sde.close()


def _make_ore_sde(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE invTypeMaterials (typeID INT, materialTypeID INT, quantity INT)")
    conn.execute("CREATE TABLE invTypes (typeID INT, groupID INT, portionSize INT)")
    conn.execute("CREATE TABLE invGroups (groupID INT, categoryID INT)")
    conn.execute("INSERT INTO invGroups VALUES (450, 25)")  # 25 = asteroid ore category
    conn.execute("INSERT INTO invGroups VALUES (99, 7)")  # a non-ore (module) category
    # Veldspar (1230): a 100-unit batch reprocesses into 400 Tritanium (34).
    conn.execute("INSERT INTO invTypes VALUES (1230, 450, 100)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (1230, 34, 400)")
    # A module (2000) that also reprocesses into Tritanium — must be excluded (not ore).
    conn.execute("INSERT INTO invTypes VALUES (2000, 99, 1)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (2000, 34, 5)")
    conn.commit()
    conn.close()


def test_ore_sources_yields_only_asteroid_ore(tmp_path: Path) -> None:
    path = tmp_path / "ore.sqlite"
    _make_ore_sde(path)
    sde = SdeDatabase(path)
    sources = sde.ore_sources({34, 35})
    # Only the ore source for Tritanium; the module is filtered out, Pyerite has none.
    assert set(sources) == {34}
    assert len(sources[34]) == 1
    yield_ = sources[34][0]
    assert yield_.ore_type_id == 1230
    assert yield_.units_per_ore == 4.0  # 400 per 100-unit batch
    assert sde.ore_sources(set()) == {}
    sde.close()


def test_missing_database_raises_sde_error(tmp_path: Path) -> None:
    with pytest.raises(SdeError):
        SdeDatabase(tmp_path / "absent.sqlite")
