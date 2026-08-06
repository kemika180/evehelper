"""classify_listings decides whether each own order still leads its market."""

import polars as pl

from evetrader.market.listings import OwnOrder, classify_listings

_STATION = 60003760

# classify_listings reads only these columns of the public book.
_BOOK_SCHEMA: dict[str, pl.DataType] = {
    "order_id": pl.Int64(),
    "type_id": pl.Int64(),
    "location_id": pl.Int64(),
    "is_buy_order": pl.Boolean(),
    "price": pl.Float64(),
}


def _book(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=_BOOK_SCHEMA, orient="row")


def _order(
    order_id: int, type_id: int, is_buy: bool, price: float, location_id: int = _STATION
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "type_id": type_id,
        "location_id": location_id,
        "is_buy_order": is_buy,
        "price": price,
    }


def test_sell_still_best_when_cheapest() -> None:
    # Own sell at 5.0; a competitor sells higher at 6.0 -> still the best (lowest) ask.
    book = _book([_order(1, 34, False, 5.0), _order(2, 34, False, 6.0)])
    own = [OwnOrder(1, 34, _STATION, False, 5.0, 100)]
    [status] = classify_listings(book, own)
    assert status.is_best
    assert status.best_competing == 6.0


def test_sell_undercut_when_competitor_cheaper() -> None:
    book = _book([_order(1, 34, False, 5.0), _order(2, 34, False, 4.5)])
    own = [OwnOrder(1, 34, _STATION, False, 5.0, 100)]
    [status] = classify_listings(book, own)
    assert not status.is_best
    assert status.best_competing == 4.5


def test_buy_overcut_when_competitor_bids_higher() -> None:
    book = _book([_order(1, 34, True, 11.0), _order(2, 34, True, 12.0)])
    own = [OwnOrder(1, 34, _STATION, True, 11.0, 100)]
    [status] = classify_listings(book, own)
    assert not status.is_best
    assert status.best_competing == 12.0


def test_alone_on_book_is_best() -> None:
    # The public book contains only the character's own order -> no competitor.
    book = _book([_order(1, 34, False, 5.0)])
    own = [OwnOrder(1, 34, _STATION, False, 5.0, 100)]
    [status] = classify_listings(book, own)
    assert status.is_best
    assert status.best_competing is None


def test_own_orders_do_not_compete_with_each_other() -> None:
    # Two of the character's own sells; the higher one is still "best" because the
    # only cheaper order is also theirs and is excluded.
    book = _book([_order(1, 34, False, 5.0), _order(2, 34, False, 4.5)])
    own = [OwnOrder(1, 34, _STATION, False, 5.0, 100), OwnOrder(2, 34, _STATION, False, 4.5, 100)]
    statuses = {s.order_id: s for s in classify_listings(book, own)}
    assert statuses[1].is_best and statuses[1].best_competing is None
    assert statuses[2].is_best and statuses[2].best_competing is None


def test_competition_is_station_and_side_scoped() -> None:
    # A cheaper sell at a different station, and a buy order, must not count as
    # competition for a sell at _STATION.
    book = _book(
        [
            _order(1, 34, False, 5.0),
            _order(2, 34, False, 3.0, location_id=60000001),  # other station
            _order(3, 34, True, 5.5),  # buy side
        ]
    )
    own = [OwnOrder(1, 34, _STATION, False, 5.0, 100)]
    [status] = classify_listings(book, own)
    assert status.is_best
    assert status.best_competing is None


def test_beaten_orders_sort_first() -> None:
    book = _book(
        [
            _order(1, 34, False, 5.0),
            _order(2, 34, False, 4.0),  # undercuts order 1
            _order(3, 35, False, 9.0),
        ]
    )
    own = [
        OwnOrder(3, 35, _STATION, False, 9.0, 100),  # best
        OwnOrder(1, 34, _STATION, False, 5.0, 100),  # undercut -> should sort first
    ]
    statuses = classify_listings(book, own)
    assert [s.order_id for s in statuses] == [1, 3]
