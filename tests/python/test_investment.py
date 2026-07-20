"""Mean-reversion source: buy below the median, sell holdings above it, else nothing."""

from evetrader.data.market import history_to_frame, orders_to_frame
from evetrader.esi.models import MarketHistoryDay, MarketOrder
from evetrader.market.fees import EffectiveFees
from evetrader.market.investment import find_opportunities

_STATION = 60003760
_FEES = EffectiveFees(sales_tax=0.05, broker_fee=0.02)


def _history(type_id: int, averages: list[float]) -> dict[int, list[MarketHistoryDay]]:
    days = [
        MarketHistoryDay.model_validate(
            {
                "date": f"2020-01-{index:02d}",
                "average": average,
                "highest": average,
                "lowest": average,
                "order_count": 10,
                "volume": 1000,
            }
        )
        for index, average in enumerate(averages, start=1)
    ]
    return {type_id: days}


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
            "range": "region",
            "duration": 90,
            "issued": "2020-01-01T00:00:00Z",
        }
    )


# median 1000, non-zero volatility
_SWINGING = [900.0, 1100.0] * 5


def _find(orders: list[MarketOrder], history: dict[int, list[MarketHistoryDay]], holdings: dict[int, int]):
    return find_opportunities(
        orders=orders_to_frame(orders),
        history=history_to_frame(history),
        station_id=_STATION,
        holdings=holdings,
        fees=_FEES,
        window=10,
        buy_position=0.2,
        sell_position=0.8,
        min_daily_isk_volume=0.0,
        max_capital_per_item=1_000_000.0,
    )


def test_buy_signal_when_ask_is_near_channel_bottom() -> None:
    signals = _find([_order(1, 100, is_buy=False, price=700.0)], _history(100, _SWINGING), {})
    assert len(signals) == 1
    assert signals[0].action == "BUY" and signals[0].type_id == 100
    assert signals[0].channel_position <= 0.2


def test_sell_signal_for_holding_bid_above_median() -> None:
    signals = _find([_order(1, 200, is_buy=True, price=1300.0)], _history(200, _SWINGING), {200: 50})
    assert len(signals) == 1
    assert signals[0].action == "SELL" and signals[0].quantity == 50


def test_overvalued_item_not_held_is_ignored() -> None:
    # Bid above median but we don't own it -> nothing to sell.
    signals = _find([_order(1, 200, is_buy=True, price=1300.0)], _history(200, _SWINGING), {})
    assert signals == []


def test_price_within_normal_range_gives_nothing() -> None:
    signals = _find([_order(1, 100, is_buy=False, price=1000.0)], _history(100, _SWINGING), {})
    assert signals == []
