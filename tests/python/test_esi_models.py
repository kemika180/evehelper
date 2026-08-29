"""ESI boundary models parse representative payloads and honour ESI quirks."""


from evehelper.esi.models import (
    Asset,
    Blueprint,
    CharacterOrder,
    IndustryJob,
    Location,
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


def test_industry_job_manufacturing_parses_with_product() -> None:
    job = IndustryJob.model_validate(
        {
            "job_id": 5,
            "activity_id": 1,
            "blueprint_type_id": 938,
            "product_type_id": 587,
            "facility_id": 60003760,
            "runs": 10,
            "status": "active",
            "cost": 1234.5,
            "start_date": "2020-01-01T00:00:00Z",
            "end_date": "2020-01-02T00:00:00Z",
        }
    )
    assert job.activity_id == 1
    assert job.product_type_id == 587
    assert job.probability is None  # absent on a manufacturing job


def test_industry_job_research_omits_product() -> None:
    job = IndustryJob.model_validate(
        {
            "job_id": 6,
            "activity_id": 4,  # ME research — no product, the blueprint is the subject
            "blueprint_type_id": 938,
            "facility_id": 60003760,
            "runs": 1,
            "status": "active",
            "start_date": "2020-01-01T00:00:00Z",
            "end_date": "2020-01-02T00:00:00Z",
        }
    )
    assert job.product_type_id is None
