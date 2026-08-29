"""Load Config from a TOML file. Impure (file I/O) — kept out of the pure core.

config.py stays pure data; this is where a config file becomes a validated Config.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from evehelper.config import Config


def default_config_dir() -> Path:
    return Path.home() / ".config" / "evehelper"


def default_config_path() -> Path:
    return default_config_dir() / "config.toml"


def default_data_dir() -> Path:
    """Local state dir (name cache, session) — gitignored, never committed."""
    return Path.home() / ".local" / "share" / "evehelper"


def migrate_legacy_dirs() -> None:
    """Move pre-rename ``evetrader`` config/data dirs to their ``evehelper`` names so an
    existing install keeps its characters, caches, and settings after the rename.

    One-time and best-effort: a directory is moved only when the old one exists and the
    new one does not (so a fresh evehelper install is never overwritten), and any failure
    is swallowed — the app falls back to a clean state rather than crashing on launch."""
    moves = (
        (Path.home() / ".config" / "evetrader", default_config_dir()),
        (Path.home() / ".local" / "share" / "evetrader", default_data_dir()),
    )
    for legacy, current in moves:
        if legacy.is_dir() and not current.exists():
            try:
                current.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(current))
            except OSError:
                continue


def load_config(path: Path) -> Config:
    """Read and validate a TOML config file into a Config.

    A missing file yields the all-defaults Config (shared client id and maintainer
    contact), so a fresh install runs with no setup — the config file is only needed to
    override defaults. Callers wanting to treat an *explicitly requested* path as an error
    should check ``path.exists()`` themselves first."""
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config.model_validate(data)
