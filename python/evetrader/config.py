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


class HomeMarket(BaseModel):
    """The region and market location a character trades at.

    `station_id` is an NPC station id OR a public structure id (e.g. an alliance
    Keepstar) — both appear in region orders and share the region's price history.
    `label` is a display name for structures, which don't resolve via /universe/names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    region_id: int = Field(gt=0)
    station_id: int = Field(gt=0)
    label: str | None = None


class InvestmentParams(BaseModel):
    """Mean-reversion tuning: the history window and how extreme a price must be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Days of market history used for the moving average and Donchian channel.
    window_days: int = Field(default=30, gt=1)
    # Buy when the ask sits at/below this fraction of the channel (0 = the low).
    buy_below_position: float = Field(default=0.15, ge=0.0, le=1.0)
    # Sell held items when the bid sits at/above this fraction (1 = the high).
    sell_above_position: float = Field(default=0.85, ge=0.0, le=1.0)
    # Downtrend guard: the recent (short-window) average of the last this-many days,
    # compared with the full-window fair value, tells a temporary dip apart from a
    # structural decline. A sharp dip barely moves the short average; a sustained
    # slide drags it well below fair value.
    trend_days: int = Field(default=7, gt=0)
    # Skip a buy when the short-window average sits more than this fraction below the
    # fair value — the price is trending down, not dipping, so it won't revert.
    max_downtrend: float = Field(default=0.10, ge=0.0, le=1.0)


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
    # Home market for characters without a specific entry below.
    default_home: HomeMarket
    # Per-character home markets, keyed by character name.
    homes: dict[str, HomeMarket] = Field(default_factory=dict)
    # Total ISK available to allocate across all suggested orders.
    total_capital_isk: float = Field(gt=0.0)
    # Type ids to always analyse for station trading (in addition to discovery).
    watchlist_type_ids: tuple[int, ...] = ()
    # Discovery: scan the whole station order book and analyse the this-many
    # best-spread items (0 = watchlist only). The full region fetch is slow the
    # first time, then cached.
    scan_candidates: int = Field(default=50, ge=0)
    # How often the TUI re-runs the pipeline; the client cache gates real fetches.
    refresh_interval_seconds: int = Field(default=30, gt=0)
    # TUI colour theme: "kemika-purple" (default) or any built-in Textual theme.
    theme: str = "kemika-purple"
    risk: RiskPreferences
    fees: FeeRates = Field(default_factory=FeeRates)
    investment: InvestmentParams = Field(default_factory=InvestmentParams)

    def home_for(self, character_name: str) -> HomeMarket:
        return self.homes.get(character_name, self.default_home)
