"""Active-listings overlay: is each of the character's open orders still best? Pure.

Given the character's own open market orders and the public order book, decide for
each order whether it is still the best price at its station — the lowest ask for a
sell, the highest bid for a buy — or has been beaten (undercut on a sell, overcut on
a buy). Competition excludes the character's own orders, so two of their own orders
on the same item don't count against each other. Deterministic given its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class OwnOrder:
    """One of the character's open orders, as plain data for the pure core."""

    order_id: int
    type_id: int
    location_id: int
    is_buy: bool
    price: float
    volume_remain: int


@dataclass(frozen=True)
class ListingStatus:
    """Whether one own order still leads its market, and by comparison to whom."""

    order_id: int
    type_id: int
    location_id: int
    is_buy: bool
    price: float
    volume_remain: int
    # Best competing price at the same station (None if no one else is on this book).
    best_competing: float | None
    # True if the order still leads: lowest ask (sell) or highest bid (buy), ties count.
    is_best: bool


def classify_listings(orders: pl.DataFrame, own: list[OwnOrder]) -> list[ListingStatus]:
    """Classify each own order against the book, actionable (beaten) orders first.

    `orders` must cover the station+type of every own order (the public book includes
    the character's own orders; they're excluded by id so an order never competes with
    itself)."""
    own_ids = [order.order_id for order in own]
    statuses: list[ListingStatus] = []
    for order in own:
        competitors = orders.filter(
            (pl.col("location_id") == order.location_id)
            & (pl.col("type_id") == order.type_id)
            & (pl.col("is_buy_order") == order.is_buy)
            & (~pl.col("order_id").is_in(own_ids))
        )
        prices = [float(price) for price in competitors["price"].to_list()]
        if not prices:
            best: float | None = None
        elif order.is_buy:
            best = max(prices)  # the highest bid is the one to beat
        else:
            best = min(prices)  # the lowest ask is the one to beat

        if best is None:
            is_best = True
        elif order.is_buy:
            is_best = order.price >= best  # highest bid wins; tie still leads
        else:
            is_best = order.price <= best  # lowest ask wins; tie still leads

        statuses.append(
            ListingStatus(
                order_id=order.order_id,
                type_id=order.type_id,
                location_id=order.location_id,
                is_buy=order.is_buy,
                price=order.price,
                volume_remain=order.volume_remain,
                best_competing=best,
                is_best=is_best,
            )
        )

    # Beaten orders (is_best False) first — they need re-pricing — then by ISK at stake.
    statuses.sort(key=lambda status: (status.is_best, -(status.price * status.volume_remain)))
    return statuses
