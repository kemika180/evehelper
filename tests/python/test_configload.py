"""load_config parses and validates a TOML file into a Config."""

from pathlib import Path

from evetrader.configload import load_config

_TOML = """
esi_client_id = "abc123"
contact = "jane@example.com"
home_region_id = 10000002
home_station_id = 60003760
total_capital_isk = 1000000000.0
watchlist_type_ids = [34, 35, 36]
refresh_interval_seconds = 60

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
    assert config.watchlist_type_ids == (34, 35, 36)
    assert config.refresh_interval_seconds == 60
    assert config.risk.min_margin == 0.05
    assert config.fees.base_sales_tax == 0.075
    # Unspecified fee fields fall back to documented defaults.
    assert config.fees.base_broker_fee == 0.03
