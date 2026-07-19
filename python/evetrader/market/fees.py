"""Effective broker fee and sales tax from skills + standings. Pure.

Deterministic given (skill levels, standings, FeeRates). No I/O. The mapping from
ESI skill/standing payloads to these plain inputs lives in the impure layer — this
module never imports the I/O side.

Formula (standard, linear, floored):
  sales_tax  = base_sales_tax * (1 - accounting_reduction_per_level * accounting)
  broker_fee = max(min_broker_fee,
                   base_broker_fee
                   - broker_relations_reduction_per_level * broker_relations
                   - faction_standing_reduction * faction_standing
                   - corp_standing_reduction * corp_standing)
"""

from dataclasses import dataclass

from evetrader.config import FeeRates


@dataclass(frozen=True)
class EffectiveFees:
    """Fee fractions to apply to a trade at one station."""

    # Fraction of sale value taken as sales tax when a sell order fills.
    sales_tax: float
    # Fraction of order value charged as broker fee when an order is placed.
    broker_fee: float


def _clamp_fraction(value: float) -> float:
    return min(1.0, max(0.0, value))


def compute_fees(
    *,
    accounting_level: int,
    broker_relations_level: int,
    faction_standing: float,
    corp_standing: float,
    rates: FeeRates,
) -> EffectiveFees:
    """Effective fees for a character with these skills and standings."""
    sales_tax = rates.base_sales_tax * (
        1.0 - rates.accounting_reduction_per_level * accounting_level
    )
    broker_fee = (
        rates.base_broker_fee
        - rates.broker_relations_reduction_per_level * broker_relations_level
        - rates.faction_standing_reduction * faction_standing
        - rates.corp_standing_reduction * corp_standing
    )
    broker_fee = max(rates.min_broker_fee, broker_fee)
    return EffectiveFees(
        sales_tax=_clamp_fraction(sales_tax),
        broker_fee=_clamp_fraction(broker_fee),
    )
