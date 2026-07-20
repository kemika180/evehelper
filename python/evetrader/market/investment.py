"""Mean-reversion / value-investing opportunity source. Pure.

Uses the same picture EVE's market history shows in-game — daily average, high, low
and volume — to build a moving average and a Donchian channel (the high/low
envelope over the window). Then:
- BUY: the current ask sits near the bottom of the channel and below the moving
  average (cheap versus its own recent range) — buy and hold for reversion.
- SELL: something you already hold has its bid near the top of the channel and
  above the moving average — a good time to sell.

Long-horizon; no instant turnaround assumed. Deterministic given its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from evetrader.market.fees import EffectiveFees


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


def _channels(history: pl.DataFrame, window: int) -> pl.DataFrame:
    recent = (
        history.sort("date", descending=True).group_by("type_id", maintain_order=True).head(window)
    )
    return recent.group_by("type_id").agg(
        pl.col("average").mean().alias("fair_value"),
        pl.col("lowest").min().alias("low_band"),
        pl.col("highest").max().alias("high_band"),
        pl.col("volume").mean().alias("avg_volume"),
        pl.len().alias("days"),
    )


def _current_prices(orders: pl.DataFrame, station_id: int) -> pl.DataFrame:
    at_station = orders.filter(pl.col("location_id") == station_id)
    asks = (
        at_station.filter(~pl.col("is_buy_order"))
        .group_by("type_id")
        .agg(pl.col("price").min().alias("ask"))
    )
    bids = (
        at_station.filter(pl.col("is_buy_order"))
        .group_by("type_id")
        .agg(pl.col("price").max().alias("bid"))
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
    min_daily_volume: float,
    max_capital_per_item: float,
) -> list[InvestmentSignal]:
    """Buy signals (cheap) and sell signals (held and dear), best expected profit first."""
    book = _channels(history, window).join(
        _current_prices(orders, station_id), on="type_id", how="inner"
    )

    signals: list[InvestmentSignal] = []
    for row in book.iter_rows(named=True):
        type_id = int(row["type_id"])
        fair = float(row["fair_value"]) if row["fair_value"] is not None else 0.0
        low = float(row["low_band"]) if row["low_band"] is not None else 0.0
        high = float(row["high_band"]) if row["high_band"] is not None else 0.0
        avg_volume = float(row["avg_volume"]) if row["avg_volume"] is not None else 0.0
        if fair <= 0.0 or high <= low or int(row["days"]) < window // 2:
            continue
        if avg_volume < min_daily_volume:
            continue
        width = high - low

        ask = float(row["ask"]) if row["ask"] is not None else None
        if ask is not None and ask > 0.0:
            position = (ask - low) / width
            if position <= buy_position and ask < fair:
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
