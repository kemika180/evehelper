"""Pure skill-plan core: models, SP/time math, expansion, scheduling, remap.

Standalone (stdlib only) and free of any UI or I/O beyond reading the two
bundled JSON files. Deterministic given (goals, skills, attributes), so every
algorithm here is unit-tested without Textual.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cache
from itertools import product
from pathlib import Path

_SKILLS_DATA = Path(__file__).with_name("skills.json")
_MASTERIES_DATA = Path(__file__).with_name("masteries.json")
_AIR_DATA = Path(__file__).with_name("air_plans.json")

MAX_LEVEL = 5
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}
_ROMAN_OUT = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}

# Character attribute dogma ids, in a fixed order for the remap search.
_ATTR_ORDER: tuple[int, ...] = (164, 165, 166, 167, 168)  # cha, int, mem, per, wil

# EVE remap: every attribute starts at 17, with 14 spare points to distribute,
# no single attribute raised more than +10.
_REMAP_BASE = 17
_REMAP_POOL = 14
_REMAP_CAP = 10

MINUTES_PER_DAY = 60 * 24


class PlanError(Exception):
    """A user-facing error (unknown skill/ship, bad level, cyclic prerequisites)."""


class SortMode(Enum):
    """How to order skills within the prerequisite + goal-priority constraints."""

    OPTIMIZED = "optimized"  # goal priority, then shortest training time (quick wins)
    ENTERED = "entered"  # keep the order skills were added / pasted
    LONGEST = "longest"  # goal priority, then longest training time
    SHORTEST = "shortest"  # shortest training time overall, goals only break ties


@dataclass(frozen=True)
class Skill:
    type_id: int
    name: str
    group: str
    rank: int
    prim: int  # primary character-attribute id (164-168)
    sec: int  # secondary character-attribute id
    prereqs: tuple[tuple[int, int], ...]  # (prerequisite type_id, required level)


@dataclass(frozen=True)
class Ship:
    type_id: int
    name: str
    group: str
    fly: tuple[tuple[int, int], ...]  # (skill_id, level) required just to fly the hull
    tiers: Mapping[int, tuple[int, ...]]  # mastery tier (1-5) -> cert ids


@dataclass(frozen=True)
class Attributes:
    charisma: int = 19
    intelligence: int = 20
    memory: int = 20
    perception: int = 20
    willpower: int = 20
    implant: int = 0  # flat bonus applied to every attribute

    def points(self, attr_id: int) -> int:
        base = {
            164: self.charisma,
            165: self.intelligence,
            166: self.memory,
            167: self.perception,
            168: self.willpower,
        }[attr_id]
        return base + self.implant


@dataclass(frozen=True)
class Goal:
    """A prioritised target: a resolved {skill_id: level} set with a label."""

    label: str
    skills: Mapping[int, int]


@dataclass(frozen=True)
class PlanEntry:
    skill: Skill
    level: int
    sp: int
    minutes: float
    goal_index: int  # index of the highest-priority goal this skill serves


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def load_skills(path: Path = _SKILLS_DATA) -> dict[int, Skill]:
    if not path.exists():
        raise PlanError(f"missing skill data at {path} — run build_data.py first")
    raw = json.loads(path.read_text())
    skills: dict[int, Skill] = {}
    for type_id_str, data in raw.items():
        type_id = int(type_id_str)
        prereqs = tuple((int(pid), int(lvl)) for pid, lvl in data["prereqs"])
        skills[type_id] = Skill(
            type_id=type_id,
            name=str(data["name"]),
            group=str(data["group"]),
            rank=int(data["rank"]),
            prim=int(data["prim"]),
            sec=int(data["sec"]),
            prereqs=prereqs,
        )
    return skills


@dataclass(frozen=True)
class Masteries:
    ships: dict[int, Ship]
    # cert id -> tier (1-5) -> ((skill_id, level), ...)
    certs: dict[int, dict[int, tuple[tuple[int, int], ...]]]
    attributes: dict[int, str]

    def find_ship(self, name: str) -> Ship | None:
        wanted = name.strip().lower()
        for ship in self.ships.values():
            if ship.name.lower() == wanted:
                return ship
        return None

    def ship_names(self) -> list[str]:
        return [ship.name for ship in self.ships.values()]

    def resolve(self, ship_id: int, tier: int) -> dict[int, int]:
        """Skills for a ship's mastery tier, including what's needed to fly the hull.

        Mastery certificates cover using the ship well but omit the skills to fly
        it at all (e.g. a Tengu's Strategic Cruiser + subsystem skills), so the
        hull's own requirements are always folded in.
        """
        ship = self.ships.get(ship_id)
        if ship is None:
            raise PlanError(f"unknown ship id {ship_id}")
        wanted: dict[int, int] = {}
        for skill_id, level in ship.fly:  # required just to undock the hull
            wanted[skill_id] = max(wanted.get(skill_id, 0), level)
        for cert_id in ship.tiers.get(tier, ()):
            for skill_id, level in self.certs.get(cert_id, {}).get(tier, ()):
                if level < 1:  # not required until a higher tier
                    continue
                wanted[skill_id] = max(wanted.get(skill_id, 0), level)
        return wanted


def load_masteries(path: Path = _MASTERIES_DATA) -> Masteries:
    if not path.exists():
        raise PlanError(f"missing mastery data at {path} — run build_data.py first")
    raw = json.loads(path.read_text())
    ships = {
        int(sid): Ship(
            type_id=int(sid),
            name=str(data["name"]),
            group=str(data["group"]),
            fly=tuple((int(s), int(lvl)) for s, lvl in data.get("fly", [])),
            tiers={int(t): tuple(int(c) for c in cids) for t, cids in data["tiers"].items()},
        )
        for sid, data in raw["ships"].items()
    }
    certs = {
        int(cid): {
            int(tier): tuple((int(s), int(lvl)) for s, lvl in pairs)
            for tier, pairs in by_tier.items()
        }
        for cid, by_tier in raw["certs"].items()
    }
    attributes = {int(k): str(v) for k, v in raw["attributes"].items()}
    return Masteries(ships=ships, certs=certs, attributes=attributes)


def load_air_plans(path: Path = _AIR_DATA) -> dict[str, dict[int, int]]:
    """The built-in AIR / career skill plans: {plan name: {skill_id: level}}."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {name: {int(s): int(lvl) for s, lvl in pairs} for name, pairs in raw.items()}


def match_air_plan(query: str, plans: Mapping[str, object]) -> str | None:
    """Resolve a typed name to a plan: exact, else a unique case-insensitive substring."""
    wanted = query.strip().lower()
    if not wanted:
        return None
    for name in plans:
        if name.lower() == wanted:
            return name
    matches = [name for name in plans if wanted in name.lower()]
    return matches[0] if len(matches) == 1 else None


# --------------------------------------------------------------------------- #
# Parsing helpers (skill goals typed / pasted by the user)
# --------------------------------------------------------------------------- #
def parse_level(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
    elif token in _ROMAN:
        value = _ROMAN[token]
    else:
        return None
    return value if 1 <= value <= MAX_LEVEL else None


def _suggest(name: str, options: Sequence[str]) -> str:
    match = difflib.get_close_matches(name, options, n=1)
    return f" (did you mean {match[0]!r}?)" if match else ""


def find_skill(name: str, skills: Mapping[int, Skill]) -> Skill | None:
    wanted = name.strip().lower()
    for skill in skills.values():
        if skill.name.lower() == wanted:
            return skill
    return None


# The "Magic 14" core support skills every pilot is advised to train early.
MAGIC_14: tuple[str, ...] = (
    "CPU Management",
    "Power Grid Management",
    "Capacitor Management",
    "Capacitor Systems Operation",
    "Energy Grid Upgrades",
    "Weapon Upgrades",
    "Hull Upgrades",
    "Mechanics",
    "Navigation",
    "Shield Management",
    "Shield Operation",
    "Long Range Targeting",
    "Signature Analysis",
    "Target Management",
)


def magic_14(skills: Mapping[int, Skill], level: int) -> dict[int, int]:
    """The Magic 14 core skills, each at `level`, as a {skill_id: level} goal set."""
    resolved: dict[int, int] = {}
    for name in MAGIC_14:
        skill = find_skill(name, skills)
        if skill is None:
            raise PlanError(f"core skill {name!r} not found in data — rerun build_data.py")
        resolved[skill.type_id] = level
    return resolved


def parse_skill_line(line: str, skills: Mapping[int, Skill]) -> tuple[int, int]:
    """Parse one 'Skill Name <level>' line, raising PlanError on any problem."""
    match = re.match(r"^(.*?)[\s:]+([A-Za-z0-9]+)$", line.strip())
    level = parse_level(match.group(2)) if match else None
    if match is None or level is None:
        raise PlanError(f"{line.strip()!r} — expected 'Skill Name <1-5>'")
    name = match.group(1).strip()
    skill = find_skill(name, skills)
    if skill is None:
        hint = _suggest(name, [s.name for s in skills.values()])
        raise PlanError(f"unknown skill {name!r}{hint}")
    return skill.type_id, level


def parse_wishlist(text: str, skills: Mapping[int, Skill]) -> dict[int, int]:
    """Parse a block of skill lines into {skill_id: level}, reporting all errors."""
    wanted: dict[int, int] = {}
    errors: list[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            skill_id, level = parse_skill_line(line, skills)
        except PlanError as error:
            errors.append(f"  line {lineno}: {error}")
            continue
        wanted[skill_id] = max(wanted.get(skill_id, 0), level)
    if errors:
        raise PlanError("could not read skills:\n" + "\n".join(errors))
    if not wanted:
        raise PlanError("no skills given")
    return wanted


# --------------------------------------------------------------------------- #
# SP / training-time maths
# --------------------------------------------------------------------------- #
def sp_for_level(level: int, rank: int) -> int:
    """Cumulative SP to complete a level (rank-1: L1=250 … L5=256000)."""
    if level <= 0:
        return 0
    return round(250 * rank * 2 ** (2.5 * (level - 1)))


def sp_per_min(skill: Skill, attrs: Attributes) -> float:
    return max(1.0, attrs.points(skill.prim) + attrs.points(skill.sec) / 2)


def _training_minutes(skill: Skill, sp: int, attrs: Attributes) -> float:
    return sp / sp_per_min(skill, attrs)


# --------------------------------------------------------------------------- #
# Expansion + scheduling
# --------------------------------------------------------------------------- #
def _expand_levels(seed: Mapping[int, int], skills: Mapping[int, Skill]) -> dict[int, int]:
    """Full {skill_id: level} needed for `seed`, propagating prerequisite levels.

    Run per goal, this yields exactly the levels that goal requires — so a level
    only pushed higher by another goal is not attributed here.
    """
    levels: dict[int, int] = {
        sid: lvl for sid, lvl in seed.items() if sid in skills and lvl >= 1
    }
    frontier = list(levels)
    while frontier:
        type_id = frontier.pop()
        for prereq_id, required in skills[type_id].prereqs:
            if prereq_id not in skills:
                continue
            new_level = max(levels.get(prereq_id, 0), required)
            if new_level != levels.get(prereq_id):
                levels[prereq_id] = new_level
                frontier.append(prereq_id)
    return levels


# A single trainable step: one level of one skill, e.g. Gunnery III.
Unit = tuple[int, int]  # (skill_id, level)


def _unit_deps(
    units: Sequence[Unit], skills: Mapping[int, Skill], levels: Mapping[int, int]
) -> dict[Unit, set[Unit]]:
    """What each (skill, level) step must be trained after.

    Level L needs level L-1 of the same skill; level 1 needs each prerequisite
    skill trained to its required level first.
    """
    deps: dict[Unit, set[Unit]] = {}
    for skill_id, level in units:
        needs: set[Unit] = set()
        if level > 1:
            needs.add((skill_id, level - 1))
        else:
            for prereq_id, required in skills[skill_id].prereqs:
                if prereq_id in levels:
                    needs.add((prereq_id, min(required, levels[prereq_id])))
        deps[(skill_id, level)] = needs
    return deps


def _schedule_greedy(
    units: Sequence[Unit],
    deps: Mapping[Unit, set[Unit]],
    skills: Mapping[int, Skill],
    unit_goal: Mapping[Unit, int],
    minutes: Mapping[Unit, float],
    n_goals: int,
    goal_first: bool,
    longest: bool,
) -> list[Unit]:
    """List-schedule steps under the prerequisite constraint.

    ``goal_first`` ranks by goal priority then training time; otherwise training
    time leads and goal priority only breaks ties. ``longest`` flips shortest to
    longest first.
    """
    remaining: dict[Unit, set[Unit]] = {unit: set(deps[unit]) for unit in units}
    sign = -1 if longest else 1
    placed: set[Unit] = set()
    order: list[Unit] = []
    while len(order) < len(units):
        ready = [u for u in units if u not in placed and remaining[u] <= placed]
        if not ready:
            stuck = sorted({skills[u[0]].name for u in units if u not in placed})
            raise PlanError(f"cyclic prerequisites among: {', '.join(stuck)}")
        def key(u: Unit) -> tuple[float, float, str, int]:
            goal = float(unit_goal.get(u, n_goals))
            time = sign * minutes[u]
            name = skills[u[0]].name
            return (goal, time, name, u[1]) if goal_first else (time, goal, name, u[1])

        chosen = min(ready, key=key)
        placed.add(chosen)
        order.append(chosen)
    return order


def _schedule_entered(
    units: Sequence[Unit],
    deps: Mapping[Unit, set[Unit]],
    skills: Mapping[int, Skill],
    goals: Sequence[Goal],
    levels: Mapping[int, int],
) -> list[Unit]:
    """Keep the order skills were added/pasted, inserting prerequisites first.

    A prerequisite-first walk seeded by goal priority then insertion order, so a
    skill's prerequisite levels appear immediately before it and your input
    sequence is otherwise preserved.
    """
    seeds: list[Unit] = []
    seen_seed: set[Unit] = set()
    for goal in goals:
        for skill_id in goal.skills:
            top = (skill_id, levels[skill_id]) if skill_id in levels else None
            if top is not None and top not in seen_seed:
                seen_seed.add(top)
                seeds.append(top)
    for skill_id in levels:  # stray skills reached only as prerequisites
        top = (skill_id, levels[skill_id])
        if top not in seen_seed:
            seen_seed.add(top)
            seeds.append(top)

    order: list[Unit] = []
    done: set[Unit] = set()
    visiting: set[Unit] = set()

    def visit(unit: Unit) -> None:
        if unit in done:
            return
        if unit in visiting:
            raise PlanError(f"cyclic prerequisites involving {skills[unit[0]].name!r}")
        visiting.add(unit)
        for dep in sorted(deps[unit]):
            visit(dep)
        visiting.discard(unit)
        done.add(unit)
        order.append(unit)

    for seed in seeds:
        visit(seed)
    return order


def build_plan(
    goals: Sequence[Goal],
    skills: Mapping[int, Skill],
    attrs: Attributes,
    sort: SortMode = SortMode.OPTIMIZED,
) -> list[PlanEntry]:
    """Expand prerequisites and schedule the plan one skill level at a time.

    Each skill is split into per-level steps (Gunnery I, then Gunnery II, …) at
    the highest level any goal or prerequisite needs. Prerequisite levels always
    precede the step that needs them; `sort` chooses the order within that
    constraint (see SortMode).
    """
    # Expand each goal's own requirements separately, then take the global max.
    # Keeping them separate is what lets a level pushed higher by a later goal be
    # attributed to that goal, not to an earlier goal that only needed it lower.
    per_goal: list[dict[int, int]] = [_expand_levels(goal.skills, skills) for goal in goals]
    levels: dict[int, int] = {}
    for req in per_goal:
        for skill_id, level in req.items():
            levels[skill_id] = max(levels.get(skill_id, 0), level)
    if not levels:
        return []

    # One step per skill level; SP + minutes are the increment for that level.
    units: list[Unit] = [(tid, lvl) for tid in levels for lvl in range(1, levels[tid] + 1)]
    deps = _unit_deps(units, skills, levels)
    sp: dict[Unit, int] = {
        (tid, lvl): sp_for_level(lvl, skills[tid].rank) - sp_for_level(lvl - 1, skills[tid].rank)
        for tid, lvl in units
    }
    minutes: dict[Unit, float] = {
        unit: _training_minutes(skills[unit[0]], sp[unit], attrs) for unit in units
    }

    # Attribute each level to the highest-priority goal that needs it AT that level.
    unit_goal: dict[Unit, int] = {}
    for skill_id, level in units:
        for index, req in enumerate(per_goal):
            if req.get(skill_id, 0) >= level:
                unit_goal[(skill_id, level)] = index
                break

    if sort is SortMode.ENTERED:
        order = _schedule_entered(units, deps, skills, goals, levels)
    else:
        order = _schedule_greedy(
            units,
            deps,
            skills,
            unit_goal,
            minutes,
            len(goals),
            goal_first=sort is not SortMode.SHORTEST,
            longest=sort is SortMode.LONGEST,
        )

    return [
        PlanEntry(
            skill=skills[tid],
            level=lvl,
            sp=sp[(tid, lvl)],
            minutes=minutes[(tid, lvl)],
            goal_index=unit_goal.get((tid, lvl), -1),
        )
        for tid, lvl in order
    ]


def total_minutes(entries: Sequence[PlanEntry]) -> float:
    return sum(entry.minutes for entry in entries)


# --------------------------------------------------------------------------- #
# Remap optimisation (advisory)
# --------------------------------------------------------------------------- #
@cache
def _remap_offsets() -> tuple[tuple[int, ...], ...]:
    """All ways to spread the spare-point pool across the 5 attributes."""
    candidates: list[tuple[int, ...]] = []
    for combo in product(range(_REMAP_CAP + 1), repeat=len(_ATTR_ORDER)):
        if sum(combo) == _REMAP_POOL:
            candidates.append(combo)
    return tuple(candidates)


def _attrs_from_offsets(offsets: Sequence[int], implant: int) -> Attributes:
    by_id = {aid: _REMAP_BASE + off for aid, off in zip(_ATTR_ORDER, offsets, strict=True)}
    return Attributes(
        charisma=by_id[164],
        intelligence=by_id[165],
        memory=by_id[166],
        perception=by_id[167],
        willpower=by_id[168],
        implant=implant,
    )


def _plan_minutes_under(entries: Sequence[PlanEntry], attrs: Attributes) -> float:
    return sum(_training_minutes(entry.skill, entry.sp, attrs) for entry in entries)


@dataclass(frozen=True)
class RemapAdvice:
    attributes: Attributes
    saved_minutes: float  # vs the current attributes; <= 0 means no improvement


def recommend_remap(entries: Sequence[PlanEntry], current: Attributes) -> RemapAdvice:
    """Best legal remap for this plan and the time it saves over `current`.

    Advisory only: it never mutates the plan. The implant bonus is held fixed.
    """
    current_minutes = _plan_minutes_under(entries, current)
    best_attrs = current
    best_minutes = current_minutes
    for offsets in _remap_offsets():
        candidate = _attrs_from_offsets(offsets, current.implant)
        candidate_minutes = _plan_minutes_under(entries, candidate)
        if candidate_minutes < best_minutes:
            best_minutes = candidate_minutes
            best_attrs = candidate
    return RemapAdvice(attributes=best_attrs, saved_minutes=current_minutes - best_minutes)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def roman(level: int) -> str:
    return _ROMAN_OUT[level]


def format_duration(minutes: float) -> str:
    days, rem = divmod(round(minutes), MINUTES_PER_DAY)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def format_importable(entries: Sequence[PlanEntry]) -> str:
    return "\n".join(f"{entry.skill.name} {entry.level}" for entry in entries)
