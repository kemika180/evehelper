"""Mean-reversion / value-investing opportunity source. Pure.

Uses the same picture EVE's market history shows in-game — daily average, high, low
and volume — to build a moving average and a Donchian channel (the high/low
envelope over the window). Then:
- BUY: the current ask sits near the bottom of the channel and below the moving
  average (cheap versus its own recent range) — buy and hold for reversion. A
  downtrend guard suppresses this when a short-window average has fallen well below
  the full-window fair value: that is a structural decline, not a revertible dip.
- SELL: something you already hold has its bid near the top of the channel and
  above the moving average — a good time to sell.

Long-horizon; no instant turnaround assumed. Deterministic given its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from evetrader.market.fees import EffectiveFees

# Robustness to one-off spikes: trim the extreme tails of the price range, and read
# the current price at a small slice of book depth rather than the single best quote.
_BAND_QUANTILE = 0.05
_DEPTH_FRACTION = 0.02


@dataclass(frozen=True)
class InvestmentSignal:
    """A value suggestion for one item."""

    type_id: int
    action: str  # "BUY" | "SELL"
    current_price: float  # today's ask (buys) or bid (sells)
    fair_value: float  # moving average over the window (reversion target)
    low_band: float  # Donchian low (min daily low over the window)
    high_band: float  # Donchian high (max daily high over the window)
    channel_position: float  # where current sits in the channel: 0 = low, 1 = high
    quantity: int  # units to buy, or units held to sell
    expected_profit: float  # fee-adjusted ISK from reverting to the moving average
    reasoning: str


def liquid_types(orders: pl.DataFrame, station_id: int, limit: int) -> list[int]:
    """The type ids with the most sell-side ISK in the book — the most liquid items,
    worth pulling history for as buy candidates."""
    sells = orders.filter((pl.col("location_id") == station_id) & (~pl.col("is_buy_order")))
    ranked = (
        sells.group_by("type_id")
        .agg((pl.col("price") * pl.col("volume_remain")).sum().alias("isk"))
        .sort("isk", descending=True)
        .head(limit)
    )
    return [int(type_id) for type_id in ranked["type_id"].to_list()]


def _channels(history: pl.DataFrame, window: int, trend_days: int) -> pl.DataFrame:
    recent = (
        history.sort("date", descending=True).group_by("type_id", maintain_order=True).head(window)
    )
    # Rank days most-recent-first within each type so the short-window average can
    # read just the latest `trend_days` for the downtrend guard.
    ranked = recent.with_columns(pl.int_range(pl.len()).over("type_id").alias("_rank"))
    return ranked.group_by("type_id").agg(
        # Median + trimmed bands are robust to one-off price spikes; a raw mean and
        # absolute min/max would swing on a single outlier day.
        pl.col("average").median().alias("fair_value"),
        pl.col("average").filter(pl.col("_rank") < trend_days).mean().alias("short_avg"),
        pl.col("lowest").quantile(_BAND_QUANTILE).alias("low_band"),
        pl.col("highest").quantile(1.0 - _BAND_QUANTILE).alias("high_band"),
        pl.col("volume").mean().alias("avg_volume"),
        pl.len().alias("days"),
    )


def _depth_price(side: pl.DataFrame, *, best_first_descending: bool, alias: str) -> pl.DataFrame:
    """The price to trade a small slice (`_DEPTH_FRACTION`) of the resting volume,
    ignoring tiny lone lowball/highball orders at the very top of the book."""
    ordered = side.sort(["type_id", "price"], descending=[False, best_first_descending])
    cumulative = ordered.with_columns(
        pl.col("volume_remain").cum_sum().over("type_id").alias("_cum"),
        pl.col("volume_remain").sum().over("type_id").alias("_total"),
    )
    reached = cumulative.filter(pl.col("_cum") >= _DEPTH_FRACTION * pl.col("_total"))
    price = pl.col("price").max() if best_first_descending else pl.col("price").min()
    return reached.group_by("type_id").agg(price.alias(alias))


def _current_prices(orders: pl.DataFrame, station_id: int) -> pl.DataFrame:
    at_station = orders.filter(pl.col("location_id") == station_id)
    asks = _depth_price(
        at_station.filter(~pl.col("is_buy_order")), best_first_descending=False, alias="ask"
    )
    bids = _depth_price(
        at_station.filter(pl.col("is_buy_order")), best_first_descending=True, alias="bid"
    )
    return asks.join(bids, on="type_id", how="full", coalesce=True)


def find_opportunities(
    *,
    orders: pl.DataFrame,
    history: pl.DataFrame,
    station_id: int,
    holdings: dict[int, int],
    fees: EffectiveFees,
    window: int,
    buy_position: float,
    sell_position: float,
    trend_days: int,
    max_downtrend: float,
    min_daily_isk_volume: float,
    max_capital_per_item: float,
) -> list[InvestmentSignal]:
    """Buy signals (cheap) and sell signals (held and dear), best expected profit first."""
    book = _channels(history, window, trend_days).join(
        _current_prices(orders, station_id), on="type_id", how="inner"
    )

    signals: list[InvestmentSignal] = []
    for row in book.iter_rows(named=True):
        type_id = int(row["type_id"])
        fair = float(row["fair_value"]) if row["fair_value"] is not None else 0.0
        short_avg = float(row["short_avg"]) if row["short_avg"] is not None else fair
        low = float(row["low_band"]) if row["low_band"] is not None else 0.0
        high = float(row["high_band"]) if row["high_band"] is not None else 0.0
        avg_volume = float(row["avg_volume"]) if row["avg_volume"] is not None else 0.0
        if fair <= 0.0 or high <= low or int(row["days"]) < window // 2:
            continue
        if avg_volume * fair < min_daily_isk_volume:  # daily ISK traded, a liquidity floor
            continue
        width = high - low

        ask = float(row["ask"]) if row["ask"] is not None else None
        if ask is not None and ask > 0.0:
            position = (ask - low) / width
            # Downtrend guard: a structural slide drags the short-window average well
            # below fair value, so the price won't revert — only a real dip qualifies.
            reverting = short_avg >= fair * (1.0 - max_downtrend)
            if position <= buy_position and ask < fair and reverting:
                units = int(max_capital_per_item // ask)
                if units > 0:
                    # Buying from asks costs no broker fee; the eventual sale pays both.
                    per_unit = fair * (1.0 - fees.sales_tax - fees.broker_fee) - ask
                    signals.append(
                        InvestmentSignal(
                            type_id=type_id,
                            action="BUY",
                            current_price=ask,
                            fair_value=fair,
                            low_band=low,
                            high_band=high,
                            channel_position=position,
                            quantity=units,
                            expected_profit=per_unit * units,
                            reasoning=(
                                f"ask at {position:.0%} of its {window}d range "
                                f"({low:,.0f} to {high:,.0f}); reverts toward {fair:,.0f}"
                            ),
                        )
                    )

        held = holdings.get(type_id, 0)
        bid = float(row["bid"]) if row["bid"] is not None else None
        if held > 0 and bid is not None and bid > 0.0:
            position = (bid - low) / width
            if position >= sell_position and bid > fair:
                per_unit = (bid - fair) * (1.0 - fees.sales_tax)
                signals.append(
                    InvestmentSignal(
                        type_id=type_id,
                        action="SELL",
                        current_price=bid,
                        fair_value=fair,
                        low_band=low,
                        high_band=high,
                        channel_position=position,
                        quantity=held,
                        expected_profit=per_unit * held,
                        reasoning=(
                            f"you hold {held:,}; bid at {position:.0%} of its {window}d "
                            f"range; median {fair:,.0f}"
                        ),
                    )
                )

    signals.sort(key=lambda signal: signal.expected_profit, reverse=True)
    return signals
