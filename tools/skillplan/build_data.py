#!/usr/bin/env python3
"""Extract skills, prerequisites and ship masteries from the local EVE SDE.

This is a one-time (occasional) build step. It reads the Fuzzwork SDE sqlite
dump that the main eve_trading project downloads and distils just the data the
skill-plan tool needs into two small JSON files next to this script:

    skills.json     {id: {name, group, rank, prim, sec, prereqs}}
    masteries.json  {attributes, certs, ships} for ship mastery tiers

The tool itself never touches the ~500 MB dump. Run: ``python build_data.py``
(default SDE path) or ``python build_data.py --sde /path/to/sde.sqlite``.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import struct
from pathlib import Path

# invCategories.categoryName == 'Skill'
_SKILL_CATEGORY = 16

# dogma attribute ids on a skill type.
_RANK_ATTR = 275  # skillTimeConstant ("rank")
_PRIMARY_ATTR = 180  # -> a character attribute id (164-168)
_SECONDARY_ATTR = 181

# character-attribute dogma ids -> name (targets of 180/181).
_CHAR_ATTRS = {
    164: "charisma",
    165: "intelligence",
    166: "memory",
    167: "perception",
    168: "willpower",
}

# (required-skill-type attr, required-level attr) pairs. Note the irregular
# pairing for skills 5 and 6 (1289<->1287, 1290<->1288) in the SDE.
_PREREQ_PAIRS: tuple[tuple[int, int], ...] = (
    (182, 277),
    (183, 278),
    (184, 279),
    (1285, 1286),
    (1289, 1287),
    (1290, 1288),
)

_DEFAULT_SDE = Path.home() / ".local/share/evetrader/sde.sqlite"
_SKILLS_OUT = Path(__file__).with_name("skills.json")
_MASTERIES_OUT = Path(__file__).with_name("masteries.json")
_AIR_OUT = Path(__file__).with_name("air_plans.json")

# The built-in AIR / career skill plans live in the EVE client's static data, not
# the SDE. Common install locations of the CCP/EVE dir (holding tq/ + ResFiles/).
_EVE_CLIENTS = (
    Path.home() / "Games/eve-online/drive_c/CCP/EVE",
    Path.home() / ".local/share/Steam/steamapps/common/Eve Online/drive_c/CCP/EVE",
    Path.home() / ".wine/drive_c/CCP/EVE",
)
_SKILLPLANS_RES = "res:/staticdata/skillplans.fsdbinary"


def _skill_attr_ids() -> list[int]:
    ids = [_RANK_ATTR, _PRIMARY_ATTR, _SECONDARY_ATTR]
    for skill_attr, level_attr in _PREREQ_PAIRS:
        ids.extend((skill_attr, level_attr))
    return ids


def _load_skill_attrs(conn: sqlite3.Connection, skill_ids: set[int]) -> dict[int, dict[int, float]]:
    """Return {attributeID: {typeID: value}} for the skill attributes we need."""
    attrs: dict[int, dict[int, float]] = {}
    placeholders = ",".join("?" for _ in _skill_attr_ids())
    for type_id, attr_id, value in conn.execute(
        "SELECT typeID, attributeID, COALESCE(valueInt, valueFloat) "
        f"FROM dgmTypeAttributes WHERE attributeID IN ({placeholders})",
        _skill_attr_ids(),
    ):
        if int(type_id) not in skill_ids or value is None:
            continue
        attrs.setdefault(int(attr_id), {})[int(type_id)] = float(value)
    return attrs


def extract_skills(conn: sqlite3.Connection) -> dict[str, object]:
    skills: dict[int, dict[str, object]] = {}
    for type_id, name, group in conn.execute(
        "SELECT t.typeID, t.typeName, g.groupName "
        "FROM invTypes t JOIN invGroups g ON t.groupID = g.groupID "
        "WHERE g.categoryID = ? AND t.published = 1",
        (_SKILL_CATEGORY,),
    ):
        skills[int(type_id)] = {
            "name": str(name),
            "group": str(group),
            "rank": 1,
            "prim": 0,
            "sec": 0,
            "prereqs": [],
        }

    attrs = _load_skill_attrs(conn, set(skills))
    for type_id, value in attrs.get(_RANK_ATTR, {}).items():
        skills[type_id]["rank"] = int(value)
    for type_id, value in attrs.get(_PRIMARY_ATTR, {}).items():
        skills[type_id]["prim"] = int(value)
    for type_id, value in attrs.get(_SECONDARY_ATTR, {}).items():
        skills[type_id]["sec"] = int(value)

    for skill_attr, level_attr in _PREREQ_PAIRS:
        req_skill = attrs.get(skill_attr, {})
        req_level = attrs.get(level_attr, {})
        for type_id, req_type in req_skill.items():
            level = req_level.get(type_id)
            prereq_id = int(req_type)
            if level is None or prereq_id not in skills:
                continue
            prereqs = skills[type_id]["prereqs"]
            assert isinstance(prereqs, list)
            prereqs.append([prereq_id, int(level)])

    return {str(type_id): data for type_id, data in sorted(skills.items())}


def _required_skills(conn: sqlite3.Connection, type_ids: set[int]) -> dict[int, list[list[int]]]:
    """{typeID: [[skill_id, level], ...]} — the skills required to use each type.

    Uses the same requiredSkill/level dogma attributes as skill prerequisites, so
    this works for ship hulls (the skills needed to fly them) too.
    """
    attr_ids: list[int] = []
    for skill_attr, level_attr in _PREREQ_PAIRS:
        attr_ids.extend((skill_attr, level_attr))
    placeholders = ",".join("?" for _ in attr_ids)
    per_type: dict[int, dict[int, float]] = {}
    for type_id, attr_id, value in conn.execute(
        "SELECT typeID, attributeID, COALESCE(valueInt, valueFloat) "
        f"FROM dgmTypeAttributes WHERE attributeID IN ({placeholders})",
        attr_ids,
    ):
        if int(type_id) in type_ids and value is not None:
            per_type.setdefault(int(type_id), {})[int(attr_id)] = float(value)

    result: dict[int, list[list[int]]] = {}
    for type_id, attrs in per_type.items():
        reqs: list[list[int]] = []
        for skill_attr, level_attr in _PREREQ_PAIRS:
            if skill_attr in attrs and level_attr in attrs:
                reqs.append([int(attrs[skill_attr]), int(attrs[level_attr])])
        if reqs:
            result[type_id] = reqs
    return result


def extract_masteries(conn: sqlite3.Connection) -> dict[str, object]:
    # certs: {certID: {tier(1-5): [[skillId, level], ...]}}, max level per skill.
    certs: dict[int, dict[int, dict[int, int]]] = {}
    for cert_id, skill_id, tier, skill_level in conn.execute(
        "SELECT certID, skillID, certLevelInt, skillLevel FROM certSkills"
    ):
        # skillLevel 0 means the skill isn't required until a higher mastery tier.
        if int(skill_level) < 1:
            continue
        per_tier = certs.setdefault(int(cert_id), {}).setdefault(int(tier), {})
        prev = per_tier.get(int(skill_id), 0)
        per_tier[int(skill_id)] = max(prev, int(skill_level))

    # ships: {typeID: {name, group, tiers: {tier(1-5): [certID, ...]}}}, from
    # certMasteries (masteryLevel is 0-indexed, so tier = masteryLevel + 1).
    ships: dict[int, dict[str, object]] = {}
    for type_id, mastery_level, cert_id, name, group in conn.execute(
        "SELECT cm.typeID, cm.masteryLevel, cm.certID, t.typeName, g.groupName "
        "FROM certMasteries cm "
        "JOIN invTypes t ON t.typeID = cm.typeID "
        "JOIN invGroups g ON g.groupID = t.groupID "
        "WHERE t.published = 1"
    ):
        ship = ships.setdefault(
            int(type_id), {"name": str(name), "group": str(group), "tiers": {}}
        )
        tiers = ship["tiers"]
        assert isinstance(tiers, dict)
        tiers.setdefault(str(int(mastery_level) + 1), []).append(int(cert_id))

    referenced = {cid for ship in ships.values() for tier in ship["tiers"].values() for cid in tier}  # type: ignore[attr-defined]
    fly = _required_skills(conn, set(ships))  # skills needed to fly each hull
    return {
        "attributes": {str(k): v for k, v in _CHAR_ATTRS.items()},
        "certs": {
            str(cert_id): {
                str(tier): sorted([sid, lvl] for sid, lvl in per_skill.items())
                for tier, per_skill in sorted(by_tier.items())
            }
            for cert_id, by_tier in sorted(certs.items())
            if cert_id in referenced
        },
        "ships": {
            str(type_id): {
                "name": data["name"],
                "group": data["group"],
                "fly": sorted(fly.get(type_id, [])),
                "tiers": {t: sorted(set(cids)) for t, cids in sorted(data["tiers"].items())},  # type: ignore[attr-defined]
            }
            for type_id, data in sorted(ships.items())
        },
    }


def _resolve_res_file(client_dir: Path, res_path: str) -> Path | None:
    """Map a res:/ path to its cached file via the client's resfileindex.txt."""
    index = client_dir / "tq" / "resfileindex.txt"
    if not index.exists():
        return None
    prefix = res_path + ","
    for line in index.read_text(errors="ignore").splitlines():
        if line.startswith(prefix):
            rel = line.split(",")[1]
            return client_dir / "ResFiles" / rel
    return None


def _find_skillplans_fsd(client_dir: Path | None) -> Path | None:
    for candidate in ([client_dir] if client_dir else list(_EVE_CLIENTS)):
        if candidate is None:
            continue
        fsd = _resolve_res_file(candidate, _SKILLPLANS_RES)
        if fsd is not None and fsd.exists():
            return fsd
    return None


def extract_air_plans(fsd_path: Path, skill_ids: set[int]) -> dict[str, list[list[int]]]:
    """Parse the built-in AIR / career skill plans out of skillplans.fsdbinary.

    Layout is a repeating ``[plan name][per-level (level, typeID) entries]``; each
    plan's entries run until the next plan name. Entries are validated against the
    known skill ids, and collapsed to one max level per skill.
    """
    data = fsd_path.read_bytes()

    def is_entry(offset: int) -> bool:
        if offset < 0 or offset + 8 > len(data):
            return False
        level, type_id = struct.unpack_from("<II", data, offset)
        return 1 <= level <= 5 and type_id in skill_ids

    names = sorted(
        (m.start(), m.group().decode("ascii"))
        for m in re.finditer(rb"[ -~]{6,}", data)
        if re.search(rb"[A-Za-z]", m.group())
    )
    plans: dict[str, list[list[int]]] = {}
    for index, (offset, name) in enumerate(names):
        end = names[index + 1][0] if index + 1 < len(names) else len(data)
        cursor = (offset + len(name) + 3) & ~3  # entries are 4-byte aligned
        while cursor < end and not is_entry(cursor):
            cursor += 4
        levels: dict[int, int] = {}
        while cursor + 8 <= end and is_entry(cursor):
            level, type_id = struct.unpack_from("<II", data, cursor)
            levels[type_id] = max(levels.get(type_id, 0), level)
            cursor += 8
        if levels:
            plans[name] = sorted([tid, lvl] for tid, lvl in levels.items())
    return plans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sde", type=Path, default=_DEFAULT_SDE, help="SDE sqlite path")
    parser.add_argument(
        "--eve-client",
        type=Path,
        default=None,
        help="CCP/EVE dir (holding tq/ + ResFiles/) for AIR plans; auto-detected if omitted",
    )
    args = parser.parse_args()

    if not args.sde.exists():
        raise SystemExit(f"SDE not found at {args.sde} (download it via the main app first)")

    conn = sqlite3.connect(f"file:{args.sde}?mode=ro", uri=True)
    try:
        skills = extract_skills(conn)
        masteries = extract_masteries(conn)
    finally:
        conn.close()

    _SKILLS_OUT.write_text(json.dumps(skills, separators=(",", ":")) + "\n")
    _MASTERIES_OUT.write_text(json.dumps(masteries, separators=(",", ":")) + "\n")
    ships = masteries["ships"]
    assert isinstance(ships, dict)
    skills_kb = _SKILLS_OUT.stat().st_size // 1024
    ships_kb = _MASTERIES_OUT.stat().st_size // 1024
    print(f"Wrote {len(skills)} skills to {_SKILLS_OUT} ({skills_kb} KB)")
    print(f"Wrote {len(ships)} ships to {_MASTERIES_OUT} ({ships_kb} KB)")

    # AIR / career plans come from the EVE client, not the SDE. Skip quietly if the
    # client isn't installed, leaving any previously-bundled air_plans.json intact.
    fsd = _find_skillplans_fsd(args.eve_client)
    if fsd is None:
        print("EVE client not found — kept existing AIR plans (if any); pass --eve-client to add")
        return
    air = extract_air_plans(fsd, {int(k) for k in skills})
    if not air:
        print(f"No AIR plans parsed from {fsd}")
        return
    _AIR_OUT.write_text(json.dumps(air, separators=(",", ":")) + "\n")
    print(f"Wrote {len(air)} AIR plans to {_AIR_OUT} ({_AIR_OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
