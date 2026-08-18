"""Normalize ESI market-order payloads into a polars frame for the pure core.

Impure only in that it depends on the ESI boundary; it does no network I/O itself
(the caller fetches via esi/). The explicit schema keeps column dtypes stable and
produces a correctly-typed *empty* frame when there are no orders.
"""

from __future__ import annotations

import io

import polars as pl

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


def orders_frame_from_pages(pages: list[bytes]) -> pl.DataFrame:
    """Parse raw market-order JSON pages straight into a polars frame.

    For the region-wide discovery scan this avoids building ~100k pydantic objects;
    the bulk order data never needs to cross into the core as models.
    """
    frames = [pl.read_json(io.BytesIO(page)) for page in pages if page and page != b"[]"]
    if not frames:
        return pl.DataFrame(schema=_ORDER_SCHEMA)
    raw = pl.concat(frames, how="vertical_relaxed")
    return raw.select(
        pl.col("order_id").cast(pl.Int64),
        pl.col("type_id").cast(pl.Int64),
        pl.col("location_id").cast(pl.Int64),
        pl.col("system_id").cast(pl.Int64),
        pl.col("is_buy_order").cast(pl.Boolean),
        pl.col("price").cast(pl.Float64),
        pl.col("volume_remain").cast(pl.Int64),
        pl.col("volume_total").cast(pl.Int64),
        pl.col("min_volume").cast(pl.Int64),
        pl.col("range").cast(pl.String),
        pl.col("duration").cast(pl.Int64),
        pl.col("issued").str.to_datetime(time_unit="us", time_zone="UTC", strict=False),
    )
