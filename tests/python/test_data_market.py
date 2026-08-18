"""data.market parses raw ESI order pages into a typed polars frame."""

import json

import polars as pl
from evetrader.data.market import orders_frame_from_pages


def test_orders_frame_from_pages_parses_raw_json() -> None:
    page = json.dumps(
        [
            {
                "order_id": 1,
                "type_id": 34,
                "location_id": 60003760,
                "system_id": 30000142,
                "is_buy_order": True,
                "price": 5.5,
                "volume_remain": 10,
                "volume_total": 20,
                "min_volume": 1,
                "range": "region",
                "duration": 90,
                "issued": "2020-01-01T00:00:00Z",
            }
        ]
    ).encode()
    frame = orders_frame_from_pages([page, b"[]", b""])  # empty pages ignored
    assert frame.height == 1
    assert frame["price"].to_list() == [5.5]
    assert frame.schema["is_buy_order"] == pl.Boolean


def test_orders_frame_from_pages_empty_is_typed() -> None:
    frame = orders_frame_from_pages([])
    assert frame.height == 0
    assert frame.schema["order_id"] == pl.Int64
