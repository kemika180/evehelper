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
    conn.execute(
        "CREATE TABLE invTypes (typeID INT, groupID INT, portionSize INT, typeName TEXT, volume FLOAT)"
    )
    conn.execute("CREATE TABLE invGroups (groupID INT, categoryID INT, groupName TEXT)")
    conn.execute("INSERT INTO invGroups VALUES (450, 25, 'Veldspar')")  # 25 = asteroid ore category
    conn.execute("INSERT INTO invGroups VALUES (99, 7, 'Module')")  # a non-ore (module) category
    conn.execute("INSERT INTO invGroups VALUES (1884, 25, 'Ubiquitous Moon Asteroids')")  # moon ores
    # Veldspar (1230): a 100-unit batch reprocesses into 400 Tritanium (34); 0.1 m³ each.
    conn.execute("INSERT INTO invTypes VALUES (1230, 450, 100, 'Veldspar', 0.1)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (1230, 34, 400)")
    # A higher-grade variant of Veldspar (denser) — must be excluded (not a base ore).
    conn.execute("INSERT INTO invTypes VALUES (1231, 450, 100, 'Veldspar IV-Grade', 0.1)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (1231, 34, 460)")
    # A compressed form — also excluded.
    conn.execute("INSERT INTO invTypes VALUES (1232, 450, 100, 'Compressed Veldspar', 0.1)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (1232, 34, 400)")
    # A moon ore that also yields Tritanium — INCLUDED now, tagged by location downstream.
    conn.execute("INSERT INTO invTypes VALUES (1235, 1884, 100, 'Zeolites', 10.0)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (1235, 34, 500)")
    # A module (2000) that also reprocesses into Tritanium — must be excluded (not ore).
    conn.execute("INSERT INTO invTypes VALUES (2000, 99, 1, 'Some Module', 5.0)")
    conn.execute("INSERT INTO invTypeMaterials VALUES (2000, 34, 5)")
    conn.commit()
    conn.close()


def test_ore_sources_yields_base_ores_with_volume(tmp_path: Path) -> None:
    path = tmp_path / "ore.sqlite"
    _make_ore_sde(path)
    sde = SdeDatabase(path)
    sources = sde.ore_sources({34, 35})
    # Base Veldspar + the moon ore Zeolites for Tritanium; the module and the graded/compressed
    # variants are filtered out; Pyerite has no ore.
    assert set(sources) == {34}
    by_id = {ore.ore_type_id: ore for ore in sources[34]}
    assert set(by_id) == {1230, 1235}
    assert by_id[1230].name == "Veldspar"
    assert by_id[1230].units_per_ore == 4.0  # 400 per 100-unit batch
    assert by_id[1230].volume == 0.1
    assert by_id[1235].name == "Zeolites"  # moon ore included
    assert sde.ore_sources(set()) == {}
    sde.close()


def _make_skill_sde(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE industryActivitySkills (typeID INT, activityID INT, skillID INT, level INT)"
    )
    conn.execute(
        "CREATE TABLE dgmTypeAttributes (typeID INT, attributeID INT, valueInt INT, valueFloat REAL)"
    )
    # Blueprint 938 manufacturing needs Industry III; a research-activity skill row is excluded.
    conn.executemany(
        "INSERT INTO industryActivitySkills VALUES (?, ?, ?, ?)",
        [(938, 1, 3380, 3), (938, 3, 3403, 1)],
    )
    # Ore 1230's reprocessing skill (attribute 790) is skill 60377; another attribute is ignored.
    conn.executemany(
        "INSERT INTO dgmTypeAttributes VALUES (?, ?, ?, ?)",
        [(1230, 790, 60377, None), (1230, 161, 10, None)],
    )
    conn.commit()
    conn.close()


def test_manufacturing_skills_reads_only_the_manufacturing_activity(tmp_path: Path) -> None:
    path = tmp_path / "skill.sqlite"
    _make_skill_sde(path)
    sde = SdeDatabase(path)
    assert sde.manufacturing_skills(938) == [(3380, 3)]  # the research-activity row excluded
    assert sde.manufacturing_skills(999) == []  # a blueprint with no requirements
    sde.close()


def test_ore_reprocessing_skills_reads_attribute_790(tmp_path: Path) -> None:
    path = tmp_path / "skill.sqlite"
    _make_skill_sde(path)
    sde = SdeDatabase(path)
    assert sde.ore_reprocessing_skills({1230, 1231}) == {1230: 60377}  # 1231 has no attribute
    assert sde.ore_reprocessing_skills(set()) == {}
    sde.close()


def test_station_security_resolves_a_stations_system_security(tmp_path: Path) -> None:
    path = tmp_path / "sec.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE staStations (stationID INT, solarSystemID INT, regionID INT)")
    conn.execute("CREATE TABLE mapSolarSystems (solarSystemID INT, regionID INT, security FLOAT)")
    conn.execute("INSERT INTO staStations VALUES (60003760, 30000142, 10000002)")  # Jita 4-4
    conn.execute("INSERT INTO mapSolarSystems VALUES (30000142, 10000002, 0.9459)")  # Jita, highsec
    conn.commit()
    conn.close()
    sde = SdeDatabase(path)
    assert sde.station_security(60003760) == pytest.approx(0.9459)
    assert sde.station_security(99999999) is None  # a player structure isn't in the SDE
    sde.close()


def test_volumes_reactions_and_pi_products(tmp_path: Path) -> None:
    path = tmp_path / "meta.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE invTypes (typeID INT, volume FLOAT)")
    conn.execute("CREATE TABLE industryActivityProducts (typeID INT, activityID INT, productTypeID INT)")
    conn.execute("CREATE TABLE planetSchematicsTypeMap (typeID INT, isInput INT)")
    conn.execute("INSERT INTO invTypes VALUES (34, 0.01)")  # Tritanium
    conn.execute("INSERT INTO industryActivityProducts VALUES (900, 11, 16671)")  # reaction output
    conn.execute("INSERT INTO industryActivityProducts VALUES (901, 1, 587)")  # a manufacturing row
    conn.execute("INSERT INTO planetSchematicsTypeMap VALUES (3828, 0)")  # a PI output
    conn.execute("INSERT INTO planetSchematicsTypeMap VALUES (2268, 1)")  # a PI input (not an output)
    conn.commit()
    conn.close()
    sde = SdeDatabase(path)
    assert sde.volumes({34, 99}) == {34: 0.01}
    assert sde.reaction_products({16671, 587}) == frozenset({16671})  # not the manufactured one
    assert sde.pi_products({3828, 2268}) == frozenset({3828})  # only the output
    sde.close()


def test_missing_database_raises_sde_error(tmp_path: Path) -> None:
    with pytest.raises(SdeError):
        SdeDatabase(tmp_path / "absent.sqlite")
