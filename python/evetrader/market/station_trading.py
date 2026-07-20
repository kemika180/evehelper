"""Station-trading engine. Pure.

Ranks per-type station-trading opportunities from a MarketSnapshot and selects the
best under capital and order-slot limits. Deterministic given its inputs — no I/O,
no wall clock.

Modeling choices (all tunable via parameters — flagged for calibration):
- You place a buy at best_buy + tick and a sell at best_sell - tick (the standard
  0.01-ISK undercut); profit is the fee/tax-adjusted spread between those.
- Committed inventory is bounded by capital-per-order AND by a `volume_capture`
  share of mean daily volume (don't stock more than sells). It is assumed to turn
  over about once per day, so expected ISK/hr = profit_per_unit * units / 24.
- Candidates are ranked by expected ISK/hr, then taken top-down while capital and
  order slots remain.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from evetrader.config import RiskPreferences
from evetrader.market.fees import EffectiveFees
from evetrader.market.snapshot import MarketSnapshot

_ISK_TICK = 0.01
_HOURS_PER_DAY = 24
# A fee-adjusted margin above this is a data artefact, not a trade — e.g. a lone
# 0.01-ISK placeholder buy order makes profit/buy_price explode to millions of %.
# Real station-trade spreads never approach 100%.
_MAX_MARGIN = 1.0


@dataclass(frozen=True)
class StationTrade:
    """A ranked station-trading suggestion for one type at one station."""

    type_id: int
    buy_price: float  # price to place your buy order at (best_buy + tick)
    sell_price: float  # price to place your sell order at (best_sell - tick)
    profit_per_unit: float  # net ISK/unit after broker fee (both sides) + sales tax
    margin: float  # profit_per_unit / buy_price
    daily_volume: float  # mean units/day from history
    competing_buy_orders: int
    competing_sell_orders: int
    recommended_units: int
    capital_required: float
    expected_isk_per_hour: float


def _profit_per_unit(buy_price: float, sell_price: float, fees: EffectiveFees) -> float:
    proceeds = sell_price * (1.0 - fees.sales_tax - fees.broker_fee)
    outlay = buy_price * (1.0 + fees.broker_fee)
    return proceeds - outlay


def _order_book(orders: pl.DataFrame, station_id: int) -> pl.DataFrame:
    at_station = orders.filter(pl.col("location_id") == station_id)
    buys = (
        at_station.filter(pl.col("is_buy_order"))
        .group_by("type_id")
        .agg(
            pl.col("price").max().alias("best_buy"),
            pl.len().alias("competing_buy_orders"),
            pl.col("volume_remain").sum().alias("buy_volume"),
        )
    )
    sells = (
        at_station.filter(~pl.col("is_buy_order"))
        .group_by("type_id")
        .agg(
            pl.col("price").min().alias("best_sell"),
            pl.len().alias("competing_sell_orders"),
            pl.col("volume_remain").sum().alias("sell_volume"),
        )
    )
    return buys.join(sells, on="type_id", how="inner")


def candidate_types(
    orders: pl.DataFrame,
    *,
    station_id: int,
    fees: EffectiveFees,
    min_margin: float,
    limit: int,
    tick: float = _ISK_TICK,
) -> list[int]:
    """Type ids at the station with the best fee-adjusted spread, best first.

    Discovery step: pick which items are worth pulling history for, from the full
    station order book, without a fixed watchlist. Pure.

    Ranked by margin * order-book depth (the smaller of the two sides' resting
    volume) — a liquidity proxy standing in for the daily volume we don't have yet.
    Ranking by margin alone surfaces expensive illiquid items that then fail the
    volume filter; weighting by depth surfaces items that are profitable AND liquid.
    """
    book = _order_book(orders, station_id)
    scored: list[tuple[float, int]] = []
    for row in book.iter_rows(named=True):
        buy_price = float(row["best_buy"]) + tick
        sell_price = float(row["best_sell"]) - tick
        if buy_price <= 0.0 or sell_price <= 0.0:
            continue
        profit = _profit_per_unit(buy_price, sell_price, fees)
        margin = profit / buy_price
        if profit <= 0.0 or not (min_margin <= margin <= _MAX_MARGIN):
            continue
        depth = min(int(row["buy_volume"]), int(row["sell_volume"]))
        scored.append((margin * depth, int(row["type_id"])))
    scored.sort(reverse=True)
    return [type_id for _score, type_id in scored[:limit]]


def _daily_volumes(history: pl.DataFrame) -> pl.DataFrame:
    return history.group_by("type_id").agg(
        pl.col("volume").mean().alias("daily_volume"),
        pl.col("average").mean().alias("avg_price"),
    )


def rank_station_trades(
    snapshot: MarketSnapshot,
    *,
    station_id: int,
    fees: EffectiveFees,
    risk: RiskPreferences,
    total_capital_isk: float,
    max_capital_per_order_isk: float,
    max_orders: int,
    volume_capture: float = 0.1,
    tick: float = _ISK_TICK,
) -> list[StationTrade]:
    """Rank viable station trades and select the best under capital/slot limits."""
    book = _order_book(snapshot.orders, station_id).join(
        _daily_volumes(snapshot.history), on="type_id", how="left"
    )

    candidates: list[StationTrade] = []
    for row in book.iter_rows(named=True):
        buy_price = float(row["best_buy"]) + tick
        sell_price = float(row["best_sell"]) - tick
        if buy_price <= 0.0 or sell_price <= 0.0:
            continue

        profit = _profit_per_unit(buy_price, sell_price, fees)
        margin = profit / buy_price
        if profit <= 0.0 or not (risk.min_margin <= margin <= _MAX_MARGIN):
            continue

        raw_volume = row["daily_volume"]
        raw_price = row["avg_price"]
        daily_volume = float(raw_volume) if raw_volume is not None else 0.0
        avg_price = float(raw_price) if raw_price is not None else 0.0
        if daily_volume * avg_price < risk.min_daily_isk_volume:
            continue
        # The average traded price must be capturable between our orders. If it
        # sits at/above the ask, trades happen only on the sell side and the "best
        # buy" is a lowball nobody fills — the spread is fake (see ARCHITECTURE).
        if not (buy_price <= avg_price <= sell_price):
            continue

        affordable = int(max_capital_per_order_isk // buy_price)
        fillable = int(daily_volume * volume_capture)
        recommended_units = min(affordable, fillable)
        if recommended_units <= 0:
            continue

        candidates.append(
            StationTrade(
                type_id=int(row["type_id"]),
                buy_price=buy_price,
                sell_price=sell_price,
                profit_per_unit=profit,
                margin=margin,
                daily_volume=daily_volume,
                competing_buy_orders=int(row["competing_buy_orders"]),
                competing_sell_orders=int(row["competing_sell_orders"]),
                recommended_units=recommended_units,
                capital_required=recommended_units * buy_price,
                expected_isk_per_hour=profit * recommended_units / _HOURS_PER_DAY,
            )
        )

    candidates.sort(key=lambda trade: trade.expected_isk_per_hour, reverse=True)
    return _select_under_constraints(candidates, total_capital_isk, max_orders)


def _select_under_constraints(
    candidates: list[StationTrade], total_capital_isk: float, max_orders: int
) -> list[StationTrade]:
    selected: list[StationTrade] = []
    remaining = total_capital_isk
    for trade in candidates:
        if len(selected) >= max_orders:
            break
        if trade.capital_required <= remaining:
            selected.append(trade)
            remaining -= trade.capital_required
    return selected
