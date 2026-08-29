"""load_config parses a TOML file, and falls back to defaults when there's no file."""

from pathlib import Path

from evehelper.configload import load_config


def test_load_config_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'esi_client_id = "abc123"\nrefresh_interval_seconds = 60\ntheme = "nord"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.esi_client_id == "abc123"  # override applied
    assert config.refresh_interval_seconds == 60
    assert config.theme == "nord"


def test_load_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    # A fresh install with no config file must still run — on the shared defaults.
    config = load_config(tmp_path / "does-not-exist.toml")

    assert config.esi_client_id  # the shared client id default is present
    assert config.contact  # a User-Agent contact default is present
    assert config.refresh_interval_seconds == 300  # schema default
    assert config.theme == "kemika-purple"
