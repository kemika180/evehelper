"""User configuration for a trading session.

Pydantic models only, never raw dicts. This module is pure data — no I/O — so the
analysis core may import it freely. Loading config from disk lives elsewhere.
"""

from pydantic import BaseModel, ConfigDict, Field


class RiskPreferences(BaseModel):
    """Thresholds the advisor uses to filter and rank opportunities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Minimum fee-adjusted profit margin, as a fraction (0.05 == 5%).
    min_margin: float = Field(gt=0.0, lt=1.0)
    # Minimum daily ISK traded for a type to count as liquid enough to trade.
    min_daily_isk_volume: float = Field(ge=0.0)
    # Cap on ISK committed to any single suggested order.
    max_capital_per_order_isk: float = Field(gt=0.0)


class Config(BaseModel):
    """Top-level user configuration for a trading session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # SSO application client id from developers.eveonline.com (public, not a secret).
    esi_client_id: str = Field(min_length=1)
    # Contact (email or URL) sent in the ESI User-Agent, as ESI's rules require.
    contact: str = Field(min_length=1)
    # Region the advisor analyses for station trading.
    home_region_id: int = Field(gt=0)
    # Station the advisor trades from.
    home_station_id: int = Field(gt=0)
    # Total ISK available to allocate across all suggested orders.
    total_capital_isk: float = Field(gt=0.0)
    risk: RiskPreferences
