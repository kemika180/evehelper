"""Normalize ESI market payloads into the pure core's MarketSnapshot (polars).

Impure only in that it depends on the ESI boundary models; it does no network I/O
itself (the caller fetches via esi/). Explicit schemas keep column dtypes stable
and produce correctly-typed *empty* frames when a list is empty.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl

from evetrader.esi.models import MarketHistoryDay, MarketOrder
from evetrader.market.snapshot import MarketSnapshot

_ORDER_SCHEMA: dict[str, pl.DataType] = {
    "order_id": pl.Int64(),
    "type_id": pl.Int64(),
    "location_id": pl.Int64(),
    "system_id": pl.Int64(),
    "is_buy_order": pl.Boolean(),
    "price": pl.Float64(),
    "volume_remain": pl.Int64(),
    "volume_total": pl.Int64(),
    "min_volume": pl.Int64(),
    "range": pl.String(),
    "duration": pl.Int64(),
    "issued": pl.Datetime(time_unit="us", time_zone="UTC"),
}

_HISTORY_SCHEMA: dict[str, pl.DataType] = {
    # ESI history is fetched per type and omits the type id; we attach it here so
    # one frame can hold history for many types.
    "type_id": pl.Int64(),
    "date": pl.Date(),
    "average": pl.Float64(),
    "highest": pl.Float64(),
    "lowest": pl.Float64(),
    "order_count": pl.Int64(),
    "volume": pl.Int64(),
}


def orders_to_frame(orders: list[MarketOrder]) -> pl.DataFrame:
    rows = [order.model_dump() for order in orders]
    return pl.DataFrame(rows, schema=_ORDER_SCHEMA, orient="row")


def history_to_frame(history_by_type: dict[int, list[MarketHistoryDay]]) -> pl.DataFrame:
    rows = [
        {"type_id": type_id, **day.model_dump()}
        for type_id, days in history_by_type.items()
        for day in days
    ]
    return pl.DataFrame(rows, schema=_HISTORY_SCHEMA, orient="row")


def build_market_snapshot(
    *,
    region_id: int,
    captured_at: datetime,
    orders: list[MarketOrder],
    history_by_type: dict[int, list[MarketHistoryDay]],
) -> MarketSnapshot:
    """Assemble a MarketSnapshot from fetched orders and per-type history."""
    return MarketSnapshot(
        region_id=region_id,
        captured_at=captured_at,
        orders=orders_to_frame(orders),
        history=history_to_frame(history_by_type),
    )
