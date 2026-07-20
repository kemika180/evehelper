"""Load Config from a TOML file. Impure (file I/O) — kept out of the pure core.

config.py stays pure data; this is where a config file becomes a validated Config.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from evetrader.config import Config


def default_config_path() -> Path:
    return Path.home() / ".config" / "evetrader" / "config.toml"


def default_data_dir() -> Path:
    """Local state dir (name cache, session) — gitignored, never committed."""
    return Path.home() / ".local" / "share" / "evetrader"


def load_config(path: Path) -> Config:
    """Read and validate a TOML config file into a Config."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config.model_validate(data)
