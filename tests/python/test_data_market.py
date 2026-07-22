"""data.market normalizes ESI orders/history into typed polars frames."""

from datetime import UTC, datetime

import polars as pl

from evetrader.data.market import (
    best_ask_prices,
    build_market_snapshot,
    history_to_frame,
    orders_frame_from_pages,
    orders_to_frame,
)
from evetrader.esi.models import MarketHistoryDay, MarketOrder


def _typed_order(
    order_id: int, type_id: int, location_id: int, *, is_buy: bool, price: float
) -> MarketOrder:
    return MarketOrder.model_validate(
        {
            "order_id": order_id,
            "type_id": type_id,
            "location_id": location_id,
            "system_id": 30000142,
            "is_buy_order": is_buy,
            "price": price,
            "volume_remain": 10,
            "volume_total": 20,
            "min_volume": 1,
            "range": "region",
            "duration": 90,
            "issued": "2020-01-01T00:00:00Z",
        }
    )


def test_best_ask_prices_takes_lowest_sell_per_type_at_the_station() -> None:
    frame = orders_to_frame(
        [
            _typed_order(1, 34, 60003760, is_buy=False, price=700.0),
            _typed_order(2, 34, 60003760, is_buy=False, price=650.0),  # lower ask wins
            _typed_order(3, 34, 60003760, is_buy=True, price=800.0),  # a buy order, ignored
            _typed_order(4, 35, 60003760, is_buy=False, price=1400.0),
            _typed_order(5, 34, 99999999, is_buy=False, price=1.0),  # other station, ignored
        ]
    )
    assert best_ask_prices(frame, 60003760) == {34: 650.0, 35: 1400.0}


def test_best_ask_prices_empty_without_sell_orders() -> None:
    frame = orders_to_frame([_typed_order(1, 34, 60003760, is_buy=True, price=700.0)])
    assert best_ask_prices(frame, 60003760) == {}


def _order(order_id: int, *, is_buy: bool, price: float) -> MarketOrder:
    return MarketOrder.model_validate(
        {
            "order_id": order_id,
            "type_id": 34,
            "location_id": 60003760,
            "system_id": 30000142,
            "is_buy_order": is_buy,
            "price": price,
            "volume_remain": 10,
            "volume_total": 20,
            "min_volume": 1,
            "range": "region",
            "duration": 90,
            "issued": "2020-01-01T00:00:00Z",
        }
    )


def test_orders_to_frame_has_typed_columns_and_values() -> None:
    frame = orders_to_frame([_order(1, is_buy=True, price=5.0), _order(2, is_buy=False, price=6.0)])
    assert frame.height == 2
    assert frame.schema["price"] == pl.Float64
    assert frame.schema["is_buy_order"] == pl.Boolean
    assert frame["price"].to_list() == [5.0, 6.0]


def test_empty_orders_yield_empty_typed_frame() -> None:
    frame = orders_to_frame([])
    assert frame.height == 0
    assert set(frame.columns) == {
        "order_id",
        "type_id",
        "location_id",
        "system_id",
        "is_buy_order",
        "price",
        "volume_remain",
        "volume_total",
        "min_volume",
        "range",
        "duration",
        "issued",
    }
    assert frame.schema["order_id"] == pl.Int64


def test_history_to_frame_parses_dates() -> None:
    day = MarketHistoryDay.model_validate(
        {
            "date": "2020-01-01",
            "average": 5.1,
            "highest": 5.5,
            "lowest": 4.9,
            "order_count": 120,
            "volume": 1_000_000,
        }
    )
    frame = history_to_frame({34: [day]})
    assert frame.schema["date"] == pl.Date
    assert frame["type_id"].to_list() == [34]
    assert frame["volume"].to_list() == [1_000_000]


def test_orders_frame_from_pages_parses_raw_json() -> None:
    import json

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


def test_build_market_snapshot_carries_region_and_time() -> None:
    captured = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
    snapshot = build_market_snapshot(
        region_id=10000002,
        captured_at=captured,
        orders=orders_to_frame([_order(1, is_buy=True, price=5.0)]),
        history_by_type={},
    )
    assert snapshot.region_id == 10000002
    assert snapshot.captured_at == captured
    assert snapshot.orders.height == 1
    assert snapshot.history.height == 0
