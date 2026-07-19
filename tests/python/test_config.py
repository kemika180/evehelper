"""Config model: construction, validation bounds, immutability, extra-field ban."""

import pytest
from pydantic import ValidationError

from evetrader.config import Config, RiskPreferences


def _valid_config() -> Config:
    return Config(
        esi_client_id="abc123",
        contact="jane@example.com",
        home_region_id=10000002,  # The Forge
        home_station_id=60003760,  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
        total_capital_isk=1_000_000_000.0,
        risk=RiskPreferences(
            min_margin=0.05,
            min_daily_isk_volume=50_000_000.0,
            max_capital_per_order_isk=100_000_000.0,
        ),
    )


def test_valid_config_constructs() -> None:
    config = _valid_config()
    assert config.home_region_id == 10000002
    assert config.risk.min_margin == 0.05


@pytest.mark.parametrize("margin", [0.0, 1.0, 1.5, -0.1])
def test_margin_must_be_a_strict_fraction(margin: float) -> None:
    with pytest.raises(ValidationError):
        RiskPreferences(
            min_margin=margin,
            min_daily_isk_volume=0.0,
            max_capital_per_order_isk=1.0,
        )


def test_capital_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Config(
            esi_client_id="abc123",
            contact="jane@example.com",
            home_region_id=1,
            home_station_id=1,
            total_capital_isk=0.0,
            risk=RiskPreferences(
                min_margin=0.05,
                min_daily_isk_volume=0.0,
                max_capital_per_order_isk=1.0,
            ),
        )


def test_config_is_frozen() -> None:
    config = _valid_config()
    with pytest.raises(ValidationError):
        config.total_capital_isk = 5.0  # type: ignore[misc]


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RiskPreferences(
            min_margin=0.05,
            min_daily_isk_volume=0.0,
            max_capital_per_order_isk=1.0,
            unexpected=True,  # type: ignore[call-arg]
        )
