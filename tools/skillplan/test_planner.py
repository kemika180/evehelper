"""Tests for the pure skill-plan core.

Logic tests use small hand-built graphs so they don't depend on the bundled
data; a couple of end-to-end checks load the real JSON to confirm it parses and
resolves.
"""

from __future__ import annotations

import planner
import pytest
from planner import (
    Attributes,
    Goal,
    PlanError,
    Skill,
    SortMode,
    build_plan,
    parse_wishlist,
    recommend_remap,
    sp_for_level,
    sp_per_min,
    total_minutes,
)

# character-attribute ids
CHA, INT, MEM, PER, WIL = 164, 165, 166, 167, 168


def _skills() -> dict[int, Skill]:
    # Gunnery (per/wil), Small Hybrid Turret -> Gunnery II,
    # Caldari Cruiser -> Caldari Frigate III + Spaceship Command III.
    defs = [
        (100, "Gunnery", 1, PER, WIL, ()),
        (101, "Small Hybrid Turret", 1, PER, WIL, ((100, 2),)),
        (200, "Spaceship Command", 1, PER, WIL, ()),
        (201, "Caldari Frigate", 2, PER, WIL, ()),
        (202, "Caldari Cruiser", 3, PER, WIL, ((201, 3), (200, 3))),
    ]
    return {
        tid: Skill(type_id=tid, name=name, group="G", rank=rank, prim=prim, sec=sec, prereqs=pre)
        for tid, name, rank, prim, sec, pre in defs
    }


def _goal(label: str, **skills: int) -> Goal:
    name_to_id = {s.name: s.type_id for s in _skills().values()}
    resolved = {name_to_id[n.replace("_", " ")]: lvl for n, lvl in skills.items()}
    return Goal(label=label, skills=resolved)


# --- SP / time maths ------------------------------------------------------- #
def test_sp_formula_anchors() -> None:
    assert sp_for_level(0, 5) == 0
    assert sp_for_level(1, 1) == 250
    assert sp_for_level(5, 1) == 256000
    assert sp_for_level(5, 3) == 768000  # scales linearly with rank


def test_sp_per_min_uses_primary_plus_half_secondary() -> None:
    skill = _skills()[100]  # prim=perception, sec=willpower
    attrs = Attributes(perception=20, willpower=16)
    assert sp_per_min(skill, attrs) == pytest.approx(20 + 16 / 2)


# --- expansion + ordering -------------------------------------------------- #
def test_prerequisites_precede_dependent_at_max_level() -> None:
    skills = _skills()
    plan = build_plan([_goal("cruiser", Caldari_Cruiser=4)], skills, Attributes())
    names = [f"{e.skill.name} {e.level}" for e in plan]
    assert names[-1] == "Caldari Cruiser 4"
    order = [e.skill.type_id for e in plan]
    assert order.index(201) < order.index(202)  # frigate before cruiser
    assert order.index(200) < order.index(202)  # spaceship command before cruiser


def test_target_level_is_max_of_requested_and_required() -> None:
    skills = _skills()
    plan = build_plan([_goal("mix", Caldari_Frigate=2, Caldari_Cruiser=4)], skills, Attributes())
    frigate_levels = [e.level for e in plan if e.skill.type_id == 201]
    # cruiser requires it at III even though II was asked; split into I, II, III
    assert frigate_levels == [1, 2, 3]


def test_goal_priority_orders_before_lower_goal_and_shares_prereqs() -> None:
    skills = _skills()
    # Goal 0 wants the cruiser; goal 1 wants a gunnery turret. Cruiser's prereqs
    # (goal 0) should schedule before the independent goal-1 skills.
    plan = build_plan(
        [_goal("cruiser", Caldari_Cruiser=3), _goal("guns", Small_Hybrid_Turret=1)],
        skills,
        Attributes(),
    )
    order = [e.skill.type_id for e in plan]
    # Every goal-0 skill comes before Gunnery(100)/Small Hybrid Turret(101).
    goal0 = {200, 201, 202}
    last_goal0 = max(order.index(t) for t in goal0)
    assert last_goal0 < order.index(100)
    assert order.index(100) < order.index(101)  # prereq still respected within goal 1


def test_split_into_per_level_steps() -> None:
    skills = _skills()
    plan = build_plan([_goal("gun", Small_Hybrid_Turret=1)], skills, Attributes())
    # Gunnery II is a prereq of the turret; every level must appear as its own step.
    assert [(e.skill.name, e.level) for e in plan] == [
        ("Gunnery", 1),
        ("Gunnery", 2),
        ("Small Hybrid Turret", 1),
    ]
    # per-level SP is the increment, not the cumulative to that level.
    gunnery_2 = next(e for e in plan if e.skill.name == "Gunnery" and e.level == 2)
    assert gunnery_2.sp == sp_for_level(2, 1) - sp_for_level(1, 1)


def test_shortest_first_tie_break() -> None:
    # Two independent zero-prereq skills: the shortest next step trains first.
    skills = {
        1: Skill(type_id=1, name="Slow", rank=8, prim=PER, sec=WIL, group="G", prereqs=()),
        2: Skill(type_id=2, name="Quick", rank=1, prim=PER, sec=WIL, group="G", prereqs=()),
    }
    plan = build_plan([Goal("both", {1: 3, 2: 3})], skills, Attributes())
    assert (plan[0].skill.name, plan[0].level) == ("Quick", 1)  # cheapest step first


def test_sort_as_entered_preserves_input_order_with_prereqs_inserted() -> None:
    skills = _skills()
    # Enter Small Hybrid Turret (needs Gunnery II) then Spaceship Command.
    goal = Goal("plan", {101: 3, 200: 2})  # dict preserves insertion order
    plan = build_plan([goal], skills, Attributes(), sort=SortMode.ENTERED)
    steps = [(e.skill.name, e.level) for e in plan]
    # Gunnery I-II pulled in before the turret's levels; Spaceship Command last.
    assert steps == [
        ("Gunnery", 1),
        ("Gunnery", 2),
        ("Small Hybrid Turret", 1),
        ("Small Hybrid Turret", 2),
        ("Small Hybrid Turret", 3),
        ("Spaceship Command", 1),
        ("Spaceship Command", 2),
    ]


def test_level_attributed_to_goal_that_needs_that_level() -> None:
    # Goal 0 needs Shield at IV (via a hull that requires it); goal 1 wants V.
    shield = Skill(type_id=10, name="Shield", rank=1, prim=PER, sec=WIL, group="G", prereqs=())
    hull = Skill(type_id=20, name="Hull", rank=1, prim=PER, sec=WIL, group="G", prereqs=((10, 4),))
    skills = {10: shield, 20: hull}
    goals = [Goal("Corax III", {20: 1}), Goal("Magic 14", {10: 5})]
    plan = build_plan(goals, skills, Attributes())
    by_unit = {(e.skill.type_id, e.level): e.goal_index for e in plan}
    assert by_unit[(10, 4)] == 0  # levels I-IV belong to the higher-priority hull goal
    assert by_unit[(10, 3)] == 0
    assert by_unit[(10, 5)] == 1  # only level V is attributed to Magic 14


def test_sort_shortest_ignores_goals_with_goal_tiebreak() -> None:
    # Goal 0 is a long skill; goal 1 is a short skill. Goal-priority ordering puts
    # the long one first; time-first ordering puts the short one first.
    skills = {
        1: Skill(type_id=1, name="Long", rank=8, prim=PER, sec=WIL, group="G", prereqs=()),
        2: Skill(type_id=2, name="Short", rank=1, prim=PER, sec=WIL, group="G", prereqs=()),
    }
    goals = [Goal("g0", {1: 1}), Goal("g1", {2: 1})]
    optimized = build_plan(goals, skills, Attributes(), sort=SortMode.OPTIMIZED)
    assert optimized[0].skill.name == "Long"  # goal 0 first
    shortest = build_plan(goals, skills, Attributes(), sort=SortMode.SHORTEST)
    assert shortest[0].skill.name == "Short"  # cheapest first, regardless of goal


def test_sort_longest_first() -> None:
    skills = {
        1: Skill(type_id=1, name="Slow", rank=8, prim=PER, sec=WIL, group="G", prereqs=()),
        2: Skill(type_id=2, name="Quick", rank=1, prim=PER, sec=WIL, group="G", prereqs=()),
    }
    plan = build_plan([Goal("both", {1: 3, 2: 3})], skills, Attributes(), sort=SortMode.LONGEST)
    # de-duplicated order: all of Slow's levels come before Quick's.
    seen = [e.skill.name for e in plan]
    unique = list(dict.fromkeys(seen))
    assert unique == ["Slow", "Quick"]


def test_entered_sort_detects_cycle() -> None:
    skills = {
        1: Skill(type_id=1, name="A", rank=1, prim=PER, sec=WIL, group="G", prereqs=((2, 1),)),
        2: Skill(type_id=2, name="B", rank=1, prim=PER, sec=WIL, group="G", prereqs=((1, 1),)),
    }
    with pytest.raises(PlanError, match="cyclic"):
        build_plan([Goal("g", {1: 1})], skills, Attributes(), sort=SortMode.ENTERED)


def test_level_zero_requests_are_dropped() -> None:
    skills = _skills()
    # A goal asking for a skill at level 0 (as some mastery certs encode "not yet
    # required") must not produce a level-0 plan entry.
    plan = build_plan([Goal("g", {100: 0, 200: 3})], skills, Attributes())
    assert all(entry.level >= 1 for entry in plan)
    assert {e.skill.type_id for e in plan} == {200}


def test_masteries_never_resolve_below_level_one() -> None:
    masteries = planner.load_masteries()
    condor = masteries.find_ship("Condor")
    assert condor is not None
    for tier in range(1, 6):
        wanted = masteries.resolve(condor.type_id, tier)
        assert all(level >= 1 for level in wanted.values())


def test_cycle_detected() -> None:
    skills = {
        1: Skill(type_id=1, name="A", rank=1, prim=PER, sec=WIL, group="G", prereqs=((2, 1),)),
        2: Skill(type_id=2, name="B", rank=1, prim=PER, sec=WIL, group="G", prereqs=((1, 1),)),
    }
    with pytest.raises(PlanError, match="cyclic"):
        build_plan([Goal("g", {1: 1})], skills, Attributes())


# --- parsing --------------------------------------------------------------- #
def test_parse_roman_and_arabic() -> None:
    skills = _skills()
    wanted = parse_wishlist("Gunnery IV\nSpaceship Command 5", skills)
    assert wanted == {100: 4, 200: 5}


def test_parse_reports_all_bad_lines() -> None:
    skills = _skills()
    with pytest.raises(PlanError) as excinfo:
        parse_wishlist("Gunnery 9\nNonexistent Skill 3", skills)
    message = str(excinfo.value)
    assert "line 1" in message and "line 2" in message


def test_parse_unknown_skill_suggests_close_match() -> None:
    skills = _skills()
    with pytest.raises(PlanError, match="Gunnery"):
        parse_wishlist("Gunery 3", skills)


def test_comments_and_blanks_ignored() -> None:
    skills = _skills()
    assert parse_wishlist("# plan\n\nGunnery 3  # turrets\n", skills) == {100: 3}


# --- remap optimiser ------------------------------------------------------- #
def test_remap_prefers_the_dominant_attribute_pair_and_saves_time() -> None:
    # A big memory/intelligence skill: the optimiser should pour points there.
    skills = {1: Skill(type_id=1, name="Big", rank=20, prim=MEM, sec=INT, group="G", prereqs=())}
    plan = build_plan([Goal("g", {1: 5})], skills, Attributes())
    advice = recommend_remap(plan, Attributes())
    assert advice.attributes.memory >= advice.attributes.perception
    assert advice.attributes.intelligence >= advice.attributes.charisma
    assert advice.saved_minutes > 0


def test_remap_reports_no_savings_when_already_optimal() -> None:
    skills = {1: Skill(type_id=1, name="Big", rank=20, prim=MEM, sec=INT, group="G", prereqs=())}
    plan = build_plan([Goal("g", {1: 5})], skills, Attributes())
    advice = recommend_remap(plan, Attributes())
    # Feeding the recommendation back in should leave nothing on the table.
    again = recommend_remap(plan, advice.attributes)
    assert again.saved_minutes == pytest.approx(0.0, abs=1e-6)


def test_remap_only_uses_legal_allocations() -> None:
    skills = {1: Skill(type_id=1, name="Big", rank=20, prim=MEM, sec=INT, group="G", prereqs=())}
    plan = build_plan([Goal("g", {1: 5})], skills, Attributes())
    a = recommend_remap(plan, Attributes()).attributes
    bases = [a.charisma, a.intelligence, a.memory, a.perception, a.willpower]
    assert sum(b - 17 for b in bases) == 14
    assert all(17 <= b <= 27 for b in bases)


# --- bundled data ---------------------------------------------------------- #
def test_bundled_data_loads() -> None:
    skills = planner.load_skills()
    masteries = planner.load_masteries()
    assert len(skills) > 400
    assert len(masteries.ships) > 300


def test_magic_14_resolves_all_core_skills() -> None:
    skills = planner.load_skills()
    core = planner.magic_14(skills, 4)
    assert len(core) == len(planner.MAGIC_14) == 14
    assert all(level == 4 for level in core.values())
    plan = build_plan([Goal("Magic 14 IV", core)], skills, Attributes())
    assert {e.skill.type_id for e in plan} >= set(core)  # includes any prerequisites


def test_mastery_includes_skills_to_fly_the_hull() -> None:
    skills = planner.load_skills()
    masteries = planner.load_masteries()
    tengu = masteries.find_ship("Tengu")
    assert tengu is not None
    assert tengu.fly, "hull fly-requirements should be recorded"
    strategic = planner.find_skill("Caldari Strategic Cruiser", skills)
    assert strategic is not None
    # the mastery certs omit the hull skill; resolve() must add it at every tier.
    for tier in range(1, 6):
        wanted = masteries.resolve(tengu.type_id, tier)
        assert strategic.type_id in wanted, f"tier {tier} missing the hull skill"
    plan = build_plan([Goal("Tengu V", masteries.resolve(tengu.type_id, 5))], skills, Attributes())
    names = {e.skill.name for e in plan}
    assert "Caldari Strategic Cruiser" in names
    assert "Caldari Cruiser" in names  # transitive prerequisite of the hull skill


def test_air_plans_load_and_resolve() -> None:
    plans = planner.load_air_plans()
    skills = planner.load_skills()
    assert len(plans) >= 20
    # every plan references only real skills at valid levels
    for name, plan in plans.items():
        assert plan, name
        for skill_id, level in plan.items():
            assert skill_id in skills and 1 <= level <= 5

    # substring matching resolves a friendly query to the full plan name
    matched = planner.match_air_plan("miner alpha", plans)
    assert matched == "Industrialist - Miner Alpha"
    plan = build_plan([Goal(matched, plans[matched])], skills, Attributes())
    names = {e.skill.name for e in plan}
    assert "Mining" in names  # a miner plan trains Mining


def test_air_plan_match_is_none_when_ambiguous() -> None:
    plans = planner.load_air_plans()
    # "enforcer" matches many plans -> ambiguous, no single match
    assert planner.match_air_plan("enforcer", plans) is None


def test_raptor_mastery_v_resolves_and_plans() -> None:
    skills = planner.load_skills()
    masteries = planner.load_masteries()
    raptor = masteries.find_ship("Raptor")
    assert raptor is not None
    wanted = masteries.resolve(raptor.type_id, 5)
    assert len(wanted) >= 43  # 43 cert skills + any hull fly-requirements
    plan = build_plan([Goal("Raptor V", wanted)], skills, Attributes())
    order = [e.skill.type_id for e in plan]
    present = {e.skill.type_id for e in plan}
    for entry in plan:
        for prereq_id, _ in entry.skill.prereqs:
            if prereq_id in present:
                # first level of the prerequisite precedes first level of the dependent
                assert order.index(prereq_id) < order.index(entry.skill.type_id)
    assert total_minutes(plan) > 0
