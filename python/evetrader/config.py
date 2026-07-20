"""User configuration for a trading session.

Pydantic models only, never raw dicts. This module is pure data — no I/O — so the
analysis core may import it freely. Loading config from disk lives elsewhere.
"""

from pydantic import BaseModel, ConfigDict, Field


class FeeRates(BaseModel):
    """CCP fee/tax constants for the (parameterized) fee formula.

    These are user-owned inputs, not code constants: CCP changes them with balance
    patches. The seed defaults reflect commonly-cited mechanics and should be
    CONFIRMED against the live game — a wrong rate silently skews every profit
    estimate. The reduction formula shape is standard (linear, floored).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Base rates before skills/standings.
    base_sales_tax: float = Field(default=0.08, ge=0.0, le=1.0)
    base_broker_fee: float = Field(default=0.03, ge=0.0, le=1.0)
    min_broker_fee: float = Field(default=0.01, ge=0.0, le=1.0)
    # Per-level skill reductions (fraction of the base removed per skill level).
    accounting_reduction_per_level: float = Field(default=0.11, ge=0.0, le=1.0)
    broker_relations_reduction_per_level: float = Field(default=0.003, ge=0.0, le=1.0)
    # Per-standing-point broker-fee reductions (applied to raw standing value).
    faction_standing_reduction: float = Field(default=0.0003, ge=0.0, le=1.0)
    corp_standing_reduction: float = Field(default=0.0002, ge=0.0, le=1.0)


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
    # Loopback port for the PKCE SSO callback; must match the registered redirect.
    callback_port: int = Field(default=8765, gt=0, lt=65536)
    # Region the advisor analyses for station trading.
    home_region_id: int = Field(gt=0)
    # Station the advisor trades from.
    home_station_id: int = Field(gt=0)
    # Total ISK available to allocate across all suggested orders.
    total_capital_isk: float = Field(gt=0.0)
    # Type ids to analyse for station trading (bounds per-type history fetches).
    watchlist_type_ids: tuple[int, ...] = ()
    # How often the TUI re-runs the pipeline; the client cache gates real fetches.
    refresh_interval_seconds: int = Field(default=30, gt=0)
    risk: RiskPreferences
    fees: FeeRates = Field(default_factory=FeeRates)
