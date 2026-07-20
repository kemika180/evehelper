"""Station-trading engine: fee-adjusted margins, filtering, ISK/hr, and selection
under capital and order-slot limits, on hand-built order books."""

from datetime import UTC, datetime

import pytest

from evetrader.config import RiskPreferences
from evetrader.data.market import build_market_snapshot
from evetrader.esi.models import MarketHistoryDay, MarketOrder
from evetrader.market.fees import EffectiveFees
from evetrader.market.snapshot import MarketSnapshot
from evetrader.market.station_trading import rank_station_trades

_STATION = 60003760
_FEES = EffectiveFees(sales_tax=0.05, broker_fee=0.02)


def _risk(*, min_margin: float = 0.05, min_daily_isk_volume: float = 1000.0) -> RiskPreferences:
    return RiskPreferences(
        min_margin=min_margin,
        min_daily_isk_volume=min_daily_isk_volume,
        max_capital_per_order_isk=1.0,  # unused by the engine; supplied explicitly
    )


def _order(order_id: int, type_id: int, *, is_buy: bool, price: float) -> MarketOrder:
    return MarketOrder.model_validate(
        {
            "order_id": order_id,
            "type_id": type_id,
            "location_id": _STATION,
            "system_id": 30000142,
            "is_buy_order": is_buy,
            "price": price,
            "volume_remain": 100,
            "volume_total": 100,
            "min_volume": 1,
            "range": "station",
            "duration": 90,
            "issued": "2020-01-01T00:00:00Z",
        }
    )


def _hist(volume: int, average: float) -> MarketHistoryDay:
    return MarketHistoryDay.model_validate(
        {
            "date": "2020-01-01",
            "average": average,
            "highest": average,
            "lowest": average,
            "order_count": 10,
            "volume": volume,
        }
    )


def _snapshot(
    orders: list[MarketOrder], history: dict[int, list[MarketHistoryDay]]
) -> MarketSnapshot:
    return build_market_snapshot(
        region_id=10000002,
        captured_at=datetime(2020, 1, 1, tzinfo=UTC),
        orders=orders,
        history_by_type=history,
    )


def test_single_trade_math_is_exact() -> None:
    snapshot = _snapshot(
        [
            _order(1, 34, is_buy=True, price=100.0),
            _order(2, 34, is_buy=False, price=150.0),
        ],
        {34: [_hist(1000, 120.0)]},
    )

    trades = rank_station_trades(
        snapshot,
        station_id=_STATION,
        fees=_FEES,
        risk=_risk(),
        total_capital_isk=1_000_000.0,
        max_capital_per_order_isk=1_000_000.0,
        max_orders=5,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.type_id == 34
    assert trade.buy_price == pytest.approx(100.01)
    assert trade.sell_price == pytest.approx(149.99)
    # 149.99*(1-0.07) - 100.01*(1.02) = 139.4907 - 102.0102
    assert trade.profit_per_unit == pytest.approx(37.4805)
    assert trade.margin == pytest.approx(37.4805 / 100.01)
    # fillable = int(1000 * 0.1) = 100; affordable is far larger
    assert trade.recommended_units == 100
    assert trade.capital_required == pytest.approx(100 * 100.01)
    assert trade.expected_isk_per_hour == pytest.approx(37.4805 * 100 / 24)
    assert trade.competing_buy_orders == 1
    assert trade.competing_sell_orders == 1


def test_filters_negative_margin_and_missing_history() -> None:
    snapshot = _snapshot(
        [
            # type 35: spread too thin -> negative after fees
            _order(1, 35, is_buy=True, price=100.0),
            _order(2, 35, is_buy=False, price=101.0),
            # type 36: healthy spread but no history -> nothing fillable
            _order(3, 36, is_buy=True, price=50.0),
            _order(4, 36, is_buy=False, price=200.0),
        ],
        {35: [_hist(1000, 100.0)]},  # only 35 has history
    )

    trades = rank_station_trades(
        snapshot,
        station_id=_STATION,
        fees=_FEES,
        risk=_risk(),
        total_capital_isk=1_000_000.0,
        max_capital_per_order_isk=1_000_000.0,
        max_orders=5,
    )

    assert trades == []


def test_ranks_by_isk_per_hour_and_respects_slot_limit() -> None:
    snapshot = _snapshot(
        [
            _order(1, 34, is_buy=True, price=100.0),
            _order(2, 34, is_buy=False, price=150.0),
            _order(3, 40, is_buy=True, price=100.0),
            _order(4, 40, is_buy=False, price=150.0),
        ],
        {34: [_hist(1000, 120.0)], 40: [_hist(5000, 120.0)]},  # 40 has 5x volume
    )

    trades = rank_station_trades(
        snapshot,
        station_id=_STATION,
        fees=_FEES,
        risk=_risk(),
        total_capital_isk=1_000_000.0,
        max_capital_per_order_isk=1_000_000.0,
        max_orders=1,
    )

    assert [t.type_id for t in trades] == [40]  # higher volume -> higher ISK/hr, wins the one slot


def test_capital_limit_skips_unaffordable_and_takes_next() -> None:
    snapshot = _snapshot(
        [
            _order(1, 34, is_buy=True, price=100.0),
            _order(2, 34, is_buy=False, price=150.0),
            _order(3, 40, is_buy=True, price=100.0),
            _order(4, 40, is_buy=False, price=150.0),
        ],
        {34: [_hist(1000, 120.0)], 40: [_hist(5000, 120.0)]},
    )

    # 40 ranks first but needs ~50k capital; 34 needs ~10k. Budget fits only 34.
    trades = rank_station_trades(
        snapshot,
        station_id=_STATION,
        fees=_FEES,
        risk=_risk(),
        total_capital_isk=20_000.0,
        max_capital_per_order_isk=1_000_000.0,
        max_orders=5,
    )

    assert [t.type_id for t in trades] == [34]


def test_other_stations_are_ignored() -> None:
    orders = [
        _order(1, 34, is_buy=True, price=100.0),
        _order(2, 34, is_buy=False, price=150.0),
    ]
    snapshot = _snapshot(orders, {34: [_hist(1000, 120.0)]})

    trades = rank_station_trades(
        snapshot,
        station_id=99999999,  # a different station
        fees=_FEES,
        risk=_risk(),
        total_capital_isk=1_000_000.0,
        max_capital_per_order_isk=1_000_000.0,
        max_orders=5,
    )

    assert trades == []
