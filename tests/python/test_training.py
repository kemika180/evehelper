"""The pure quick-train tips engine: SP/time formula plus horizon filtering and ranking."""

from evetrader.market.training import (
    TrainingCandidate,
    max_level_within,
    skill_points_for_level,
    training_seconds,
    training_tips,
)


def _candidate(**overrides: object) -> TrainingCandidate:
    base: dict[str, object] = dict(
        skill_id=3389,
        current_level=4,
        target_level=5,
        rank=1,
        primary_attribute=20,
        secondary_attribute=20,
        current_sp=0,
        kind="refine",
        ore_reduction=0.02,
        unlocks_type_id=None,
    )
    base.update(overrides)
    return TrainingCandidate(**base)  # type: ignore[arg-type]


def test_skill_points_for_level_follows_the_eve_curve() -> None:
    assert skill_points_for_level(1, 0) == 0
    assert skill_points_for_level(1, 1) == 250
    assert skill_points_for_level(1, 5) == 256_000  # rank-1 level 5
    assert skill_points_for_level(3, 5) == 768_000  # scales with rank


def test_training_seconds_from_remaining_sp_and_rate() -> None:
    candidate = _candidate(rank=1, target_level=1, current_sp=0)
    # 250 SP at 20 + 20/2 = 30 SP/min -> 250/30 min = 500 seconds.
    assert training_seconds(candidate) == 250 / 30 * 60


def test_training_seconds_none_when_rate_is_zero() -> None:
    assert training_seconds(_candidate(primary_attribute=0, secondary_attribute=0)) is None


def test_tips_keep_quick_beneficial_ones_unlocks_first_then_ore_savings() -> None:
    near = 250 * 2**10  # rank-1 skill already at level 4 -> level 5 trains instantly
    unlock = _candidate(skill_id=1, current_sp=near, kind="unlock", ore_reduction=0.0, unlocks_type_id=7)
    big_save = _candidate(skill_id=2, current_sp=near, ore_reduction=0.05)
    small_save = _candidate(skill_id=3, current_sp=near, ore_reduction=0.01)
    slow = _candidate(skill_id=4, rank=50, current_sp=0, ore_reduction=0.09)  # huge but far off
    no_help = _candidate(skill_id=5, current_sp=near, ore_reduction=0.0)  # neither ore-cut nor unlock
    tips = training_tips([small_save, big_save, unlock, slow, no_help], horizon_seconds=3 * 3600)
    # Unlock first, then ore savings by size; the slow and the no-help ones are dropped.
    assert [tip.skill_id for tip in tips] == [1, 2, 3]


def test_tips_drop_a_maxed_or_non_advancing_level() -> None:
    maxed = _candidate(current_level=5, target_level=6)
    assert training_tips([maxed], horizon_seconds=3 * 3600) == []


def test_tips_drop_a_negligible_ore_reduction() -> None:
    near = 250 * 2**10  # trains instantly
    negligible = _candidate(current_sp=near, ore_reduction=0.003)  # rounds to ~0%
    worthwhile = _candidate(skill_id=9, current_sp=near, ore_reduction=0.04)
    tips = training_tips([negligible, worthwhile], horizon_seconds=3 * 3600)
    assert [tip.skill_id for tip in tips] == [9]


def test_max_level_within_reaches_several_levels_from_scratch() -> None:
    # rank-1 skill from 0 SP at 30 SP/min: L1=250 (~8m), L2=1,414 (~47m), L3=8,000 (~4.4h).
    assert max_level_within(1, 0, 0, sp_per_minute=30.0, horizon_seconds=3 * 3600) == 2
    assert max_level_within(1, 0, 0, sp_per_minute=30.0, horizon_seconds=10 * 3600) == 3
    # A slow rank never gets past the current level within the horizon.
    assert max_level_within(50, 0, 0, sp_per_minute=30.0, horizon_seconds=3 * 3600) == 0
    # No training rate -> no progress.
    assert max_level_within(1, 2, 0, sp_per_minute=0.0, horizon_seconds=3 * 3600) == 2
