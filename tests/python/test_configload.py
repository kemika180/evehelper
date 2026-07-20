"""load_config parses and validates a TOML file into a Config."""

from pathlib import Path

from evetrader.configload import load_config

_TOML = """
esi_client_id = "abc123"
contact = "jane@example.com"
total_capital_isk = 1000000000.0
refresh_interval_seconds = 60

default_home = { region_id = 10000002, station_id = 60003760 }

[homes.Rehvin]
region_id = 10000003
station_id = 1053970513596
label = "4-HWWF Keepstar"

[risk]
min_margin = 0.05
min_daily_isk_volume = 50000000.0
max_capital_per_order_isk = 100000000.0

[fees]
base_sales_tax = 0.075
"""


def test_load_config_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_TOML, encoding="utf-8")

    config = load_config(path)

    assert config.esi_client_id == "abc123"
    assert config.default_home.station_id == 60003760
    # A character with a specific home overrides the default; others fall back.
    assert config.home_for("Rehvin").label == "4-HWWF Keepstar"
    assert config.home_for("Rehvin").station_id == 1053970513596
    assert config.home_for("Someone Else").station_id == 60003760
    assert config.fees.base_sales_tax == 0.075
