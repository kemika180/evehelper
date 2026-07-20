"""Opportunity model, the OpportunitySource Protocol, and the station-trading
source. Pure.

A source turns (market snapshot, character state, config) into ranked Opportunities.
Implement the Protocol structurally — do not subclass it. The advisor engine
consumes any list of sources.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from evetrader.config import Config
from evetrader.market.snapshot import MarketSnapshot
from evetrader.market.station_trading import StationTrade, rank_station_trades

from .state import CharacterState


class Opportunity(BaseModel):
    """A single ranked money-making suggestion."""

    model_config = ConfigDict(frozen=True)

    kind: str  # e.g. "station_trade"
    type_id: int
    station_id: int
    buy_price: float
    sell_price: float
    quantity: int
    capital_required: float
    profit_per_unit: float
    expected_isk_per_hour: float
    reasoning: str


class OpportunitySource(Protocol):
    """Produces Opportunities from a snapshot + character state + config."""

    def opportunities(
        self, snapshot: MarketSnapshot, character: CharacterState, config: Config
    ) -> list[Opportunity]: ...


class StationTradingSource:
    """Station-trading source: adapts StationTrade results into Opportunities."""

    def opportunities(
        self, snapshot: MarketSnapshot, character: CharacterState, config: Config
    ) -> list[Opportunity]:
        capital = min(config.total_capital_isk, character.wallet_balance)
        trades = rank_station_trades(
            snapshot,
            station_id=character.station_id,
            fees=character.fees,
            risk=config.risk,
            total_capital_isk=capital,
            max_capital_per_order_isk=config.risk.max_capital_per_order_isk,
            max_orders=character.free_order_slots,
        )
        return [self._to_opportunity(trade, character.station_id) for trade in trades]

    @staticmethod
    def _to_opportunity(trade: StationTrade, station_id: int) -> Opportunity:
        reasoning = (
            f"Buy {trade.recommended_units} @ {trade.buy_price:,.2f}, "
            f"sell @ {trade.sell_price:,.2f}; margin {trade.margin:.1%}, "
            f"~{trade.daily_volume:,.0f}/day"
        )
        return Opportunity(
            kind="station_trade",
            type_id=trade.type_id,
            station_id=station_id,
            buy_price=trade.buy_price,
            sell_price=trade.sell_price,
            quantity=trade.recommended_units,
            capital_required=trade.capital_required,
            profit_per_unit=trade.profit_per_unit,
            expected_isk_per_hour=trade.expected_isk_per_hour,
            reasoning=reasoning,
        )
