"""The advisor engine: gather Opportunities from sources and rank them. Pure.

Capital and order-slot limits are applied within each source (they need the
source's own economics); the engine's job is to merge and globally order by
expected ISK/hr. Cross-source capital allocation is a later concern.
"""

from collections.abc import Sequence

from evetrader.config import Config
from evetrader.market.snapshot import MarketSnapshot

from .source import Opportunity, OpportunitySource
from .state import CharacterState


def rank(
    sources: Sequence[OpportunitySource],
    snapshot: MarketSnapshot,
    character: CharacterState,
    config: Config,
) -> list[Opportunity]:
    """Merge opportunities from all sources, ranked by expected ISK/hr."""
    opportunities = [
        opportunity
        for source in sources
        for opportunity in source.opportunities(snapshot, character, config)
    ]
    opportunities.sort(key=lambda opportunity: opportunity.expected_isk_per_hour, reverse=True)
    return opportunities
