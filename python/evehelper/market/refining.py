"""The reprocessing (refining) yield model. PURE — no I/O.

How much of a mineral you recover from an ore is the station/structure's base
reprocessing rate raised by three skills: Reprocessing (all ores), Reprocessing
Efficiency (all ores), and the ore-specific processing skill. The base rate is user-owned
config — it also folds in the structure/rig/implant bonuses this tool can't detect — while
the skill multipliers are computed here from the character's trained levels. Deterministic
given (base rate, skill levels).
"""

from __future__ import annotations

# EVE reprocessing skill type ids — stable identifiers, like the item type ids in config,
# not balance-tuned rates. The ore-specific skill id is read per ore from the SDE.
REPROCESSING_SKILL_ID = 3385  # +3% yield/level, every ore
REPROCESSING_EFFICIENCY_SKILL_ID = 3389  # +2% yield/level, every ore

# Per-level yield bonuses — EVE game mechanics (structural, not tuned rates).
_REPROCESSING_PER_LEVEL = 0.03
_REPROCESSING_EFFICIENCY_PER_LEVEL = 0.02
_ORE_PROCESSING_PER_LEVEL = 0.02

# Where an ore is found + how accessible it is, keyed by its reprocessing skill (SDE
# attribute 790): a rough location band shown to the player, and an accessibility rank
# (0 = common highsec rock, rising to rarer/null/abyssal/moon). The rank sorts a recipe's
# ore options toward what's mineable near the character's home; the label just informs.
ORE_SOURCE_INFO: dict[int, tuple[int, str]] = {
    12195: (0, "highsec"),  # Veldspar Processing (legacy)
    60377: (0, "highsec"),  # Simple Ore Processing (Veldspar, Scordite, Plagioclase, …)
    60378: (1, "lowsec"),  # Coherent Ore Processing
    60379: (2, "lowsec"),  # Variegated Ore Processing
    12189: (3, "nullsec"),  # Mercoxit Ore Processing
    60380: (3, "nullsec"),  # Complex Ore Processing (Arkonor, Bistot, Spodumain, …)
    60381: (4, "abyssal"),  # Abyssal Ore Processing
    90040: (4, "abyssal"),  # Erratic Ore Processing (special)
    46152: (5, "moon"),  # Ubiquitous Moon Ore Processing
    46153: (5, "moon"),  # Common Moon Ore Processing
    46154: (5, "moon"),  # Uncommon Moon Ore Processing
    46155: (5, "moon"),  # Rare Moon Ore Processing
    46156: (5, "moon"),  # Exceptional Moon Ore Processing
}
# Assumed for an ore whose reprocessing skill isn't in the table above.
DEFAULT_ORE_SOURCE_INFO: tuple[int, str] = (3, "nullsec")


def ore_source_info(reprocessing_skill_id: int) -> tuple[int, str]:
    """The (accessibility rank, rough location label) for an ore, from its reprocessing
    skill; a sensible default for an unrecognised skill."""
    return ORE_SOURCE_INFO.get(reprocessing_skill_id, DEFAULT_ORE_SOURCE_INFO)


# The eight core minerals from most common (0) to rarest (7). Tritanium is in nearly every
# ore; the high-end minerals come only from scarce ores. Used to order a build's mining
# rarest-first, so the abundant common-mineral byproducts of the rare ores are credited
# before we mine dedicated common-mineral ore. Stable type ids, like the skill ids above.
MINERAL_COMMONNESS: dict[int, int] = {
    34: 0,  # Tritanium (most common)
    35: 1,  # Pyerite
    36: 2,  # Mexallon
    37: 3,  # Isogen
    38: 4,  # Nocxium
    39: 5,  # Zydrine
    40: 6,  # Megacyte
    11399: 7,  # Morphite (rarest)
}
# Non-core minerals (moon/ice/gas materials) — gathered first; their specialised ores rarely
# yield the core minerals, so their exact order doesn't affect byproduct crediting.
_DEFAULT_MINERAL_COMMONNESS = 8


def mineral_commonness(mineral_type_id: int) -> int:
    """How commonly a mineral is refined (0 = Tritanium-common, higher = rarer)."""
    return MINERAL_COMMONNESS.get(mineral_type_id, _DEFAULT_MINERAL_COMMONNESS)


def security_target_rank(security: float) -> int:
    """The ore accessibility rank most available at a system's security status — highsec
    favours the common rocks (0), lowsec the mid ranks, null the dense/rare ones. Biases a
    recipe's ore options toward what the character can actually mine near home."""
    if security >= 0.45:  # highsec (rounds to 0.5 in-game)
        return 0
    if security > 0.0:  # lowsec
        return 2
    return 3  # null / wormhole / other


def reprocessing_yield(
    base_rate: float,
    reprocessing_level: int,
    reprocessing_efficiency_level: int,
    ore_processing_level: int,
) -> float:
    """Effective fraction of an ore's minerals recovered when reprocessing it, capped at
    100% (you can't recover more than the ore holds).

    ``base_rate`` is the station/structure rate (config); the three skill levels raise it
    multiplicatively per EVE's formula. Implant bonuses aren't modelled (the ESI
    attributes endpoint omits them), so this is a slight, deliberate under-estimate."""
    effective = (
        base_rate
        * (1 + _REPROCESSING_PER_LEVEL * reprocessing_level)
        * (1 + _REPROCESSING_EFFICIENCY_PER_LEVEL * reprocessing_efficiency_level)
        * (1 + _ORE_PROCESSING_PER_LEVEL * ore_processing_level)
    )
    return min(effective, 1.0)
