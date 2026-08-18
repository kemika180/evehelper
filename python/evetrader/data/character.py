"""Assemble CharacterState from live ESI data. Impure.

Fetches the wallet, derives trade skills and free order slots, and bridges live ESI
into the pure core's CharacterState.
"""

from __future__ import annotations

from evetrader.advisor.state import CharacterState, TradeSkills, total_order_slots
from evetrader.config import Config
from evetrader.esi.client import EsiClient
from evetrader.esi.endpoints import fetch_wallet_balance
from evetrader.esi.models import CharacterOrder, CharacterSkills

# EVE skill type ids (stable game constants; sanity-check against live data).
_TRADE = 3443
_RETAIL = 3444
_WHOLESALE = 16596
_TYCOON = 18580


def _trade_skills(skills: CharacterSkills) -> TradeSkills:
    levels = {skill.skill_id: skill.active_skill_level for skill in skills.skills}
    return TradeSkills(
        trade=levels.get(_TRADE, 0),
        retail=levels.get(_RETAIL, 0),
        wholesale=levels.get(_WHOLESALE, 0),
        tycoon=levels.get(_TYCOON, 0),
    )


async def build_character_state(
    client: EsiClient,
    config: Config,
    character_id: int,
    token: str,
    skills: CharacterSkills,
    open_orders: list[CharacterOrder],
) -> CharacterState:
    """Fetch character data and build the pure CharacterState for the advisor.

    `skills` and `open_orders` are passed in (already fetched by the caller, which
    also surfaces them to the TUI) so they aren't fetched twice.
    """
    wallet = await fetch_wallet_balance(client, character_id, token)
    trade_skills = _trade_skills(skills)
    free_order_slots = max(0, total_order_slots(trade_skills) - len(open_orders))
    return CharacterState(
        wallet_balance=wallet,
        trade_skills=trade_skills,
        free_order_slots=free_order_slots,
    )
