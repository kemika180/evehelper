"""CharacterState: the character-side hand-off to the pure advisor. Pure.

Plain data consumed read-only by the engine. Built by data/character.py from live
ESI data; this module never imports the I/O layer, so the engine stays testable
with hand-built state.
"""

from dataclasses import dataclass

# Market order-slot bonuses per skill level (base 5 orders). Game constants.
_BASE_ORDER_SLOTS = 5
_TRADE_PER_LEVEL = 4
_RETAIL_PER_LEVEL = 8
_WHOLESALE_PER_LEVEL = 16
_TYCOON_PER_LEVEL = 32


@dataclass(frozen=True)
class TradeSkills:
    """Levels of the skills that grant market order slots."""

    trade: int
    retail: int
    wholesale: int
    tycoon: int


def total_order_slots(skills: TradeSkills) -> int:
    """Maximum simultaneous market orders granted by the trade skills."""
    return (
        _BASE_ORDER_SLOTS
        + _TRADE_PER_LEVEL * skills.trade
        + _RETAIL_PER_LEVEL * skills.retail
        + _WHOLESALE_PER_LEVEL * skills.wholesale
        + _TYCOON_PER_LEVEL * skills.tycoon
    )


@dataclass(frozen=True)
class CharacterState:
    """What the advisor needs to know about the character, right now."""

    wallet_balance: float
    trade_skills: TradeSkills
    # Order slots not currently occupied by open orders.
    free_order_slots: int
