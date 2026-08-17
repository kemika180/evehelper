"""End-to-end pipeline over mocked ESI: character + holdings, then buy/sell signals."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
from evetrader.config import Config, HomeMarket, InvestmentParams, RiskPreferences, SpecialMarket
from evetrader.data.sde import SdeDatabase
from evetrader.data.structures import StructureCache
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator
from evetrader.esi.client import EsiClient
from evetrader.data.sde import OreYield
from evetrader.pipeline import (
    _buildable_predicate,
    _expand_recipes,
    _ore_yields,
    _refine_sources,
    fetch_character,
    fetch_opportunities,
)

_STATION = 60003760
_REGION = 10000002
_OWNER = 1000035
_FACTION = 500001
_FUTURE = "Wed, 21 Oct 2099 07:28:00 GMT"
_UNDERVALUED = 34  # a cheap buy candidate
_HELD_DEAR = 35  # held, and currently dear
_STRUCTURE = 1_035_660_376_235  # a player structure the character can dock at
_STRUCT_SYSTEM = 30004759
_PLEX = 44992  # trades on the global market, not the home station book
_PLEX_REGION = 19000001  # EVE's global PLEX market region
_PLEX_LOCATION = 60008494  # a global-market location, away from the home station


def _config() -> Config:
    return Config(
        esi_client_id="cid",
        contact="c@e.com",
        default_home=HomeMarket(region_id=_REGION, station_id=_STATION),
        total_capital_isk=1_000_000_000.0,
        trading_type_ids=(_UNDERVALUED, _HELD_DEAR),
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=0.0, max_capital_per_order_isk=100_000_000.0
        ),
        investment=InvestmentParams(window_days=4, buy_below_position=0.3, sell_above_position=0.7),
    )


class _FakeStore:
    def __init__(self) -> None:
        self.tokens = {42: "r0"}

    def load(self, character_id: int) -> str | None:
        return self.tokens.get(character_id)

    def save(self, character_id: int, refresh_token: str) -> None:
        self.tokens[character_id] = refresh_token

    def delete(self, character_id: int) -> None:
        self.tokens.pop(character_id, None)


def _order(order_id: int, type_id: int, *, is_buy: bool, price: float) -> dict[str, object]:
    return {
        "order_id": order_id,
        "type_id": type_id,
        "location_id": _STATION,
        "system_id": 30000142,
        "is_buy_order": is_buy,
        "price": price,
        "volume_remain": 1000,
        "volume_total": 1000,
        "min_volume": 1,
        "range": "region",
        "duration": 90,
        "issued": "2020-01-01T00:00:00Z",
    }


def _plex_order(order_id: int, *, is_buy: bool, price: float) -> dict[str, object]:
    order = _order(order_id, _PLEX, is_buy=is_buy, price=price)
    order["location_id"] = _PLEX_LOCATION  # global market, not the home station
    return order


def _channel_history() -> list[dict[str, object]]:
    # 4 days with a median ~1000 and a 900-1100 low/high channel.
    return [
        {
            "date": f"2020-01-0{day}",
            "average": average,
            "highest": 1100.0,
            "lowest": 900.0,
            "order_count": 10,
            "volume": 1000,
        }
        for day, average in enumerate([950.0, 1050.0, 950.0, 1050.0], start=1)
    ]


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "login.eveonline.com":
        return httpx.Response(
            200,
            json={"access_token": "atk", "token_type": "Bearer", "expires_in": 1200, "refresh_token": "r1"},
        )
    exp = {"Expires": _FUTURE}
    if "/markets/" in path and path.endswith("/history/"):
        return httpx.Response(200, json=_channel_history(), headers=exp)
    if "/markets/" in path and path.endswith("/orders/"):
        if f"/markets/{_PLEX_REGION}/" in path:
            orders = [
                _plex_order(10, is_buy=False, price=700.0),  # cheap ask on the global market
                _plex_order(11, is_buy=True, price=600.0),
            ]
        else:
            orders = [
                _order(1, _UNDERVALUED, is_buy=False, price=700.0),  # cheap ask
                _order(2, _UNDERVALUED, is_buy=True, price=600.0),
                _order(3, _HELD_DEAR, is_buy=True, price=1300.0),  # dear bid
                _order(4, _HELD_DEAR, is_buy=False, price=1400.0),
            ]
        return httpx.Response(200, json=orders, headers={**exp, "X-Pages": "1"})
    if path.endswith("/assets/"):
        return httpx.Response(
            200,
            json=[
                {
                    "item_id": 1,
                    "type_id": _HELD_DEAR,
                    "quantity": 10,
                    "location_id": _STATION,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "is_singleton": False,
                },
                {
                    "item_id": 2,
                    "type_id": _HELD_DEAR,
                    "quantity": 999,  # 20 jumps away -> must NOT count as sellable here
                    "location_id": 60000001,
                    "location_flag": "Hangar",
                    "location_type": "station",
                    "is_singleton": False,
                },
                {
                    "item_id": 3,
                    "type_id": _UNDERVALUED,
                    "quantity": 5,
                    "location_id": _STRUCTURE,  # a player structure -> named via lookup
                    "location_flag": "Hangar",
                    "location_type": "item",
                    "is_singleton": False,
                },
            ],
            headers=exp,
        )
    if "/universe/structures/" in path:
        return httpx.Response(
            200,
            json={"name": "V-3YG7 Fortizar", "solar_system_id": _STRUCT_SYSTEM},
            headers=exp,
        )
    if path.endswith("/wallet/"):
        return httpx.Response(200, json=5_000_000.0, headers=exp)
    if path.endswith("/blueprints/"):
        return httpx.Response(
            200,
            json=[
                {
                    "item_id": 3,  # the same item that appears in the asset list
                    "type_id": _UNDERVALUED,
                    "location_id": _STRUCTURE,
                    "location_flag": "Hangar",
                    "quantity": -2,
                    "material_efficiency": 8,
                    "time_efficiency": 16,
                    "runs": 10,
                }
            ],
            headers=exp,
        )
    if path.endswith("/industry/jobs/"):
        return httpx.Response(
            200,
            json=[
                {
                    "job_id": 100,
                    "activity_id": 1,
                    "blueprint_type_id": _UNDERVALUED,
                    "product_type_id": _HELD_DEAR,
                    "facility_id": _STRUCTURE,  # named via the same structure lookup as assets
                    "runs": 3,
                    "status": "active",
                    "cost": 12345.0,
                    "start_date": "2020-01-01T00:00:00Z",
                    "end_date": "2020-01-02T00:00:00Z",
                }
            ],
            headers=exp,
        )
    if path.endswith("/skillqueue/"):
        return httpx.Response(200, json=[], headers=exp)
    if path.endswith("/skills/"):
        return httpx.Response(
            200,
            json={
                "skills": [{"skill_id": 3443, "active_skill_level": 5, "trained_skill_level": 5}],
                "total_sp": 10_000_000,
            },
            headers=exp,
        )
    if path.endswith("/attributes/"):
        return httpx.Response(
            200,
            json={
                "charisma": 20,
                "intelligence": 20,
                "memory": 20,
                "perception": 20,
                "willpower": 20,
            },
            headers=exp,
        )
    if "/characters/" in path and path.endswith("/orders/"):
        return httpx.Response(200, json=[], headers=exp)
    if path.endswith("/standings/"):
        return httpx.Response(200, json=[], headers=exp)
    if "/universe/stations/" in path:
        return httpx.Response(
            200,
            json={"station_id": _STATION, "name": "Jita", "system_id": 30000142, "type_id": 1529, "owner": _OWNER},
            headers=exp,
        )
    if "/corporations/" in path:
        return httpx.Response(200, json={"name": "Caldari Navy", "faction_id": _FACTION}, headers=exp)
    if "/universe/names/" in path:
        catalogue = {
            _UNDERVALUED: "Tritanium",
            _HELD_DEAR: "Pyerite",
            _STATION: "Jita IV-4",
            _STRUCT_SYSTEM: "V-3YG7",
        }
        return httpx.Response(
            200,
            json=[
                {"id": i, "name": catalogue.get(i, f"type {i}"), "category": "x"}
                for i in json.loads(request.content)
            ],
        )
    raise AssertionError(f"unexpected {path}")


def _make_recipe_sde(path: Path) -> None:
    """A minimal SDE where the owned blueprint (type _UNDERVALUED) manufactures
    _HELD_DEAR from one unit of _UNDERVALUED — both priced by the mock order book."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE industryActivityProducts "
        "(typeID INT, activityID INT, productTypeID INT, quantity INT)"
    )
    conn.execute(
        "CREATE TABLE industryActivityMaterials "
        "(typeID INT, activityID INT, materialTypeID INT, quantity INT)"
    )
    conn.execute("INSERT INTO industryActivityProducts VALUES (?, 1, ?, 1)", (_UNDERVALUED, _HELD_DEAR))
    conn.execute("INSERT INTO industryActivityMaterials VALUES (?, 1, ?, 1)", (_UNDERVALUED, _UNDERVALUED))
    _add_reprocessing_tables(conn)
    conn.commit()
    conn.close()


def _add_reprocessing_tables(conn: sqlite3.Connection) -> None:
    """The reprocessing/category/skill tables a real SDE always carries (empty here — no ore
    refines into the toy minerals and no manufacturing skills are required, so `ore_sources`
    and `manufacturing_skills` return nothing)."""
    conn.execute("CREATE TABLE invTypeMaterials (typeID INT, materialTypeID INT, quantity INT)")
    conn.execute(
        "CREATE TABLE invTypes (typeID INT, groupID INT, portionSize INT, typeName TEXT, volume FLOAT)"
    )
    conn.execute("CREATE TABLE invGroups (groupID INT, categoryID INT, groupName TEXT)")
    conn.execute("CREATE TABLE industryActivitySkills (typeID INT, activityID INT, skillID INT, level INT)")
    conn.execute("CREATE TABLE dgmTypeAttributes (typeID INT, attributeID INT, valueInt INT, valueFloat REAL)")
    conn.execute("CREATE TABLE staStations (stationID INT, solarSystemID INT, regionID INT)")
    conn.execute("CREATE TABLE mapSolarSystems (solarSystemID INT, regionID INT, security FLOAT)")
    conn.execute("CREATE TABLE planetSchematicsTypeMap (typeID INT, isInput INT)")


def _make_nested_sde(path: Path) -> None:
    """product 100 <- (bp 101) 2x component 200 ; component 200 <- (bp 201) 5x mineral 300."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE industryActivityProducts "
        "(typeID INT, activityID INT, productTypeID INT, quantity INT)"
    )
    conn.execute(
        "CREATE TABLE industryActivityMaterials "
        "(typeID INT, activityID INT, materialTypeID INT, quantity INT)"
    )
    conn.executemany(
        "INSERT INTO industryActivityProducts VALUES (?, 1, ?, 1)", [(101, 100), (201, 200)]
    )
    conn.executemany(
        "INSERT INTO industryActivityMaterials VALUES (?, 1, ?, ?)",
        [(101, 200, 2), (201, 300, 5)],
    )
    _add_reprocessing_tables(conn)
    conn.commit()
    conn.close()


def test_refine_sources_orders_by_accessibility_then_density_with_volume() -> None:
    # Mineral 300: a dense ore 900 (2 units/ore) and a sparse ore 901 (1), both highsec.
    ore_sources = {
        300: [
            OreYield(ore_type_id=900, units_per_ore=2.0, name="Dense", volume=0.1),
            OreYield(ore_type_id=901, units_per_ore=1.0, name="Sparse", volume=0.2),
        ]
    }
    yields = {900: 0.5, 901: 0.5}
    meta = {900: (0, "highsec"), 901: (0, "highsec")}
    options = _refine_sources(ore_sources, yields, meta, target_rank=0)[300]
    # Same location -> the denser ore (less ore to mine) leads; the other is the alternative.
    assert options[0].ore_type_id == 900
    assert options[0].ore_units_per_unit == 1.0  # 1 / (2 units/ore * 0.5 yield)
    assert options[0].location == "highsec"
    assert options[0].unit_volume == 0.1
    assert [option.ore_type_id for option in options[1:]] == [901]


def test_refine_sources_biases_toward_the_home_security_band() -> None:
    ore_sources = {
        500: [
            OreYield(ore_type_id=600, units_per_ore=1.0, name="Spodumain", volume=16.0),
            OreYield(ore_type_id=601, units_per_ore=1.0, name="Veldspar", volume=0.1),
        ]
    }
    yields = {600: 1.0, 601: 1.0}
    meta = {600: (3, "nullsec"), 601: (0, "highsec")}
    highsec = _refine_sources(ore_sources, yields, meta, target_rank=0)
    assert highsec[500][0].ore_type_id == 601  # a highsec home is steered to the common ore
    null = _refine_sources(ore_sources, yields, meta, target_rank=3)
    assert null[500][0].ore_type_id == 600  # a null home is steered to its local ore


def test_refine_sources_collapses_same_family_variants_to_the_base_ore() -> None:
    # "Brimful Zeolites" (denser) is a variant of "Zeolites" -> dropped for the base rock.
    ore_sources = {
        700: [
            OreYield(ore_type_id=800, units_per_ore=1.0, name="Zeolites", volume=10.0),
            OreYield(ore_type_id=801, units_per_ore=2.0, name="Brimful Zeolites", volume=10.0),
        ]
    }
    meta = {800: (5, "moon"), 801: (5, "moon")}
    options = _refine_sources(ore_sources, {800: 1.0, 801: 1.0}, meta, target_rank=0)[700]
    assert [option.ore_type_id for option in options] == [800]  # base Zeolites only
    assert options[0].location == "moon"


def _make_skill_sde(path: Path) -> None:
    """A tiny SDE with a manufacturing skill requirement and an ore reprocessing skill."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE industryActivitySkills (typeID INT, activityID INT, skillID INT, level INT)"
    )
    conn.execute(
        "CREATE TABLE dgmTypeAttributes (typeID INT, attributeID INT, valueInt INT, valueFloat REAL)"
    )
    conn.execute("INSERT INTO industryActivitySkills VALUES (938, 1, 3380, 3)")  # needs Industry III
    conn.execute("INSERT INTO dgmTypeAttributes VALUES (1230, 790, 60377, NULL)")  # ore -> skill
    conn.commit()
    conn.close()


def test_buildable_predicate_gates_on_manufacturing_skills(tmp_path: Path) -> None:
    path = tmp_path / "skill.sqlite"
    _make_skill_sde(path)
    sde = SdeDatabase(path)
    assert _buildable_predicate(sde, {3380: 3})(938) is True  # meets Industry III
    assert _buildable_predicate(sde, {3380: 2})(938) is False  # one level short
    assert _buildable_predicate(sde, {})(999) is True  # a blueprint with no requirements
    sde.close()


def test_ore_yields_apply_the_characters_reprocessing_skills(tmp_path: Path) -> None:
    path = tmp_path / "skill.sqlite"
    _make_skill_sde(path)
    sde = SdeDatabase(path)
    # Ore 1230's specific skill is 60377; at L4 with Reprocessing 5 / Efficiency 5 on base 0.5.
    yields = _ore_yields(sde, 0.5, {3385: 5, 3389: 5, 60377: 4}, {1230})
    assert yields[1230] == 0.5 * 1.15 * 1.10 * 1.08
    sde.close()


def test_expand_recipes_only_follows_owned_sub_blueprints(tmp_path: Path) -> None:
    path = tmp_path / "nested.sqlite"
    _make_nested_sde(path)
    sde = SdeDatabase(path)
    top = sde.manufacturing_recipe(101)
    assert top is not None
    # Owning both blueprints -> the sub-component (200) is expanded and self-costable.
    assert set(_expand_recipes(sde, [top], {101, 201})) == {100, 200}
    # Owning only the top blueprint -> 200 is left to be bought (no owned BP to build it).
    assert set(_expand_recipes(sde, [top], {101})) == {100}
    sde.close()


def test_pipeline_produces_buys_and_sells(tmp_path: Path) -> None:
    async def go() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(_config(), http)
            authenticator = Authenticator(_config(), http, _FakeStore())
            name_cache = NameCache(tmp_path / "names.json", client)
            structure_cache = StructureCache(client)
            now = lambda: datetime(2020, 1, 1, tzinfo=UTC)  # noqa: E731

            home = _config().default_home
            character = await fetch_character(
                client, authenticator, _config(), 42, home, name_cache, structure_cache, now=now
            )
            assert character.holdings == {_HELD_DEAR: 10}
            # Blueprint research is keyed by asset item_id for the browser popup.
            assert character.blueprints[3].runs == 10
            assert character.blueprints[3].material_efficiency == 8
            # Industry jobs come through, and the job's facility is named like any place.
            assert [job.job_id for job in character.industry_jobs] == [100]
            assert character.industry_jobs[0].product_type_id == _HELD_DEAR
            assert character.station_name == "Jita IV-4"
            # A player structure is named via /universe/structures + its system name.
            assert character.names[_STRUCTURE] == "V-3YG7 Fortizar · V-3YG7"

            sde_path = tmp_path / "sde.sqlite"
            _make_recipe_sde(sde_path)
            sde = SdeDatabase(sde_path)
            report = await fetch_opportunities(
                client, _config(), character, home, name_cache, sde
            )
            # One verdict per tracked item, in config order; the undervalued item reads
            # BUY, the held-and-dear one SELL.
            assert [s.type_id for s in report.tracked] == [_UNDERVALUED, _HELD_DEAR]
            verdicts = {s.type_id: s.verdict for s in report.tracked}
            assert verdicts[_UNDERVALUED] == "BUY"
            assert verdicts[_HELD_DEAR] == "SELL"
            held = {s.type_id: s.held for s in report.tracked}
            assert held[_HELD_DEAR] == 10  # sellable holdings at the home station
            assert report.names[_UNDERVALUED] == "Tritanium"
            # History for tracked items is retained for plotting.
            assert _UNDERVALUED in report.history and len(report.history[_UNDERVALUED]) == 4

            # Build-vs-buy: the owned blueprint's product is priced at Jita and ranked.
            assert len(report.builds) == 1
            build = report.builds[0]
            assert build.blueprint_type_id == _UNDERVALUED
            assert build.product_type_id == _HELD_DEAR
            assert build.analysis.priced
            assert build.analysis.verdict == "BUILD"
            assert report.names[_HELD_DEAR] == "Pyerite"  # product name resolved
            sde.close()

    asyncio.run(go())


def test_pipeline_prices_plex_from_the_global_market(tmp_path: Path) -> None:
    config = Config(
        esi_client_id="cid",
        contact="c@e.com",
        default_home=HomeMarket(region_id=_REGION, station_id=_STATION),
        total_capital_isk=1_000_000_000.0,
        trading_type_ids=(_PLEX,),
        special_markets={_PLEX: SpecialMarket(region_id=_PLEX_REGION)},
        risk=RiskPreferences(
            min_margin=0.05, min_daily_isk_volume=0.0, max_capital_per_order_isk=100_000_000.0
        ),
        investment=InvestmentParams(window_days=4, buy_below_position=0.3, sell_above_position=0.7),
    )

    async def go() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = EsiClient(config, http)
            authenticator = Authenticator(config, http, _FakeStore())
            name_cache = NameCache(tmp_path / "names.json", client)
            structure_cache = StructureCache(client)
            now = lambda: datetime(2020, 1, 1, tzinfo=UTC)  # noqa: E731

            home = config.default_home
            character = await fetch_character(
                client, authenticator, config, 42, home, name_cache, structure_cache, now=now
            )
            report = await fetch_opportunities(client, config, character, home, name_cache)

            # PLEX has no orders on the home station book; priced from its global market
            # region it still comes through with a live quote and a verdict, not "no data".
            assert [s.type_id for s in report.tracked] == [_PLEX]
            plex = report.tracked[0]
            assert plex.has_data
            assert plex.price == 700.0  # the global-market ask, relabelled to the home station
            assert plex.verdict == "BUY"

    asyncio.run(go())
