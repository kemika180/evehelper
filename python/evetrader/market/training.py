"""Quick-train recommendations for crafting. PURE — no I/O.

Some skills make self-sourcing easier: reprocessing skills raise refine yield (so you mine
*less ore*), and a manufacturing skill can *unlock* building a sub-component you'd otherwise
buy. This module ranks such level-ups that both help and train fast (within a horizon), so
the UI can nudge "train this, it's quick". No ISK — the benefit is less mining or a new
build option, not a price.

The benefit per candidate (how much less ore, or which component it unlocks) is computed by
the caller; this module owns the training-time formula and the horizon/ranking policy, so it
stays deterministic given its inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TrainingCandidate:
    """A skill level-up that helps self-source a build. ``primary_attribute`` /
    ``secondary_attribute`` are the character's point values for the skill's attributes (they
    set the SP/min rate). ``kind`` is ``refine`` (``ore_reduction`` = fraction less ore to
    mine) or ``unlock`` (``unlocks_type_id`` = the component it enables building)."""

    skill_id: int
    current_level: int
    target_level: int
    rank: int
    primary_attribute: int
    secondary_attribute: int
    current_sp: int
    kind: str
    ore_reduction: float = 0.0
    unlocks_type_id: int | None = None


@dataclass(frozen=True)
class TrainingTip:
    """A recommended level-up: the skill, the jump, how long it trains, and its benefit."""

    skill_id: int
    current_level: int
    target_level: int
    train_seconds: float
    kind: str
    ore_reduction: float = 0.0
    unlocks_type_id: int | None = None


def skill_points_for_level(rank: int, level: int) -> int:
    """Cumulative skill points to have trained a skill of this ``rank`` to ``level`` (0 at
    level 0), per EVE's formula ``250 · rank · √32^(level-1)``."""
    if level <= 0:
        return 0
    return math.ceil(250 * rank * 2 ** (2.5 * (level - 1)))


def max_level_within(
    rank: int, current_level: int, current_sp: int, sp_per_minute: float, horizon_seconds: float
) -> int:
    """The highest level (≤5) reachable from ``current_sp`` within ``horizon_seconds`` — often
    more than one level from scratch (a rank-1 skill can reach L2-L3 in a couple of hours).
    Returns ``current_level`` when not even the next level fits."""
    if sp_per_minute <= 0:
        return current_level
    best = current_level
    for level in range(current_level + 1, 6):
        remaining = max(0, skill_points_for_level(rank, level) - current_sp)
        if remaining / sp_per_minute * 60.0 <= horizon_seconds:
            best = level
        else:
            break
    return best


def training_seconds(candidate: TrainingCandidate) -> float | None:
    """Wall-clock seconds to train ``candidate``'s next level, from the SP still needed and
    the ``primary + secondary/2`` SP-per-minute rate. None if the rate is non-positive.

    Starts from the character's current SP in the skill, so partial progress counts.
    Implant bonuses aren't included (ESI omits them), so estimates run slightly long."""
    sp_per_minute = candidate.primary_attribute + candidate.secondary_attribute / 2
    if sp_per_minute <= 0:
        return None
    target_sp = skill_points_for_level(candidate.rank, candidate.target_level)
    remaining = max(0, target_sp - candidate.current_sp)
    return remaining / sp_per_minute * 60.0


# Below this an ore-cut is negligible for the build (a skill for an ore that's a tiny slice
# of the plan) — not worth a training slot, and it would just round to "~0% less ore".
_MIN_ORE_REDUCTION = 0.01


def _beneficial(candidate: TrainingCandidate) -> bool:
    """Whether a candidate actually helps: it unlocks a build, or meaningfully cuts ore."""
    return candidate.unlocks_type_id is not None or candidate.ore_reduction >= _MIN_ORE_REDUCTION


def training_tips(
    candidates: list[TrainingCandidate], *, horizon_seconds: float
) -> list[TrainingTip]:
    """The candidates that both help and train within ``horizon_seconds`` — unlocks first,
    then the biggest ore savings. Level-ups past 5 or that don't advance the skill are
    dropped."""
    tips: list[TrainingTip] = []
    for candidate in candidates:
        if not _beneficial(candidate) or not candidate.current_level < candidate.target_level <= 5:
            continue
        seconds = training_seconds(candidate)
        if seconds is None or seconds > horizon_seconds:
            continue
        tips.append(
            TrainingTip(
                candidate.skill_id,
                candidate.current_level,
                candidate.target_level,
                seconds,
                candidate.kind,
                candidate.ore_reduction,
                candidate.unlocks_type_id,
            )
        )
    tips.sort(key=lambda tip: (tip.unlocks_type_id is None, -tip.ore_reduction))
    return tips
