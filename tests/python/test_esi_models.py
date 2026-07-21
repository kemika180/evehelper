"""ESI boundary models parse representative payloads and honour ESI quirks."""

from datetime import UTC, datetime

from evetrader.esi.models import (
    Asset,
    Blueprint,
    CharacterOrder,
    Location,
    MarketOrder,
    TokenResponse,
)


def test_token_response_parses() -> None:
    token = TokenResponse.model_validate(
        {
            "access_token": "aaa.bbb.ccc",
            "token_type": "Bearer",
            "expires_in": 1199,
            "refresh_token": "r-e-f",
            "extra_field_esi_might_add": True,
        }
    )
    assert token.expires_in == 1199
    assert token.refresh_token == "r-e-f"


def test_market_order_parses_issued_as_aware_datetime() -> None:
    order = MarketOrder.model_validate(
        {
            "order_id": 5500000000,
            "type_id": 34,
            "location_id": 60003760,
            "system_id": 30000142,
            "is_buy_order": False,
            "price": 5.02,
            "volume_remain": 1_000_000,
            "volume_total": 2_000_000,
            "min_volume": 1,
            "range": "region",
            "duration": 90,
            "issued": "2016-09-03T05:12:25Z",
        }
    )
    assert order.issued == datetime(2016, 9, 3, 5, 12, 25, tzinfo=UTC)
    assert order.is_buy_order is False


def test_character_sell_order_defaults_is_buy_order_false() -> None:
    # ESI omits is_buy_order on sell orders entirely.
    order = CharacterOrder.model_validate(
        {
            "order_id": 1,
            "type_id": 34,
            "region_id": 10000002,
            "location_id": 60003760,
            "price": 6.0,
            "volume_remain": 100,
            "volume_total": 100,
            "duration": 90,
            "issued": "2016-09-03T05:12:25Z",
            "range": "station",
        }
    )
    assert order.is_buy_order is False
    assert order.min_volume == 1


def test_location_allows_station_only() -> None:
    loc = Location.model_validate({"solar_system_id": 30000142, "station_id": 60003760})
    assert loc.station_id == 60003760
    assert loc.structure_id is None


def test_asset_parses() -> None:
    asset = Asset.model_validate(
        {
            "item_id": 1000000016835,
            "type_id": 34,
            "quantity": 42,
            "location_id": 60003760,
            "location_flag": "Hangar",
            "location_type": "station",
            "is_singleton": False,
        }
    )
    assert asset.quantity == 42


def _blueprint(**overrides: object) -> Blueprint:
    payload: dict[str, object] = {
        "item_id": 1000000016836,
        "type_id": 938,
        "location_id": 60003760,
        "location_flag": "Hangar",
        "quantity": -1,
        "material_efficiency": 10,
        "time_efficiency": 20,
        "runs": -1,
    }
    payload.update(overrides)
    return Blueprint.model_validate(payload)


def test_blueprint_original_has_unlimited_runs() -> None:
    original = _blueprint(quantity=-1, runs=-1)
    assert original.runs == -1  # -1 marks a BPO (unlimited)
    assert original.material_efficiency == 10


def test_blueprint_copy_carries_finite_runs() -> None:
    copy = _blueprint(quantity=-2, runs=42, material_efficiency=4, time_efficiency=8)
    assert copy.runs == 42
    assert copy.material_efficiency == 4
    assert copy.time_efficiency == 8
