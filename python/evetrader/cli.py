"""The `evetrader` entry point: `evetrader login` then `evetrader`. Composition root.

Builds the real I/O resources (kept alive across refreshes so the client cache
works) and wires the pipeline into the TUI. Not unit-tested — it is the live-only
glue; the pipeline, app rendering, and auth pieces it composes are tested.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx

from evetrader.config import Config
from evetrader.configload import default_config_path, default_data_dir, load_config
from evetrader.data.universe import NameCache
from evetrader.esi.auth import Authenticator, KeyringTokenStore, login
from evetrader.esi.client import EsiClient
from evetrader.pipeline import AdvisorReport, refresh
from evetrader.tui.app import EveTraderApp


@dataclass
class _Resources:
    http: httpx.AsyncClient
    client: EsiClient
    authenticator: Authenticator
    name_cache: NameCache


def _build_resources(config: Config) -> _Resources:
    http = httpx.AsyncClient()
    client = EsiClient(config, http)
    return _Resources(
        http=http,
        client=client,
        authenticator=Authenticator(config, http, KeyringTokenStore()),
        name_cache=NameCache(default_data_dir() / "names.json", client),
    )


def _character_id_path() -> Path:
    return default_data_dir() / "character_id"


def _save_character_id(character_id: int) -> None:
    path = _character_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(character_id), encoding="utf-8")


def _load_character_id() -> int:
    return int(_character_id_path().read_text(encoding="utf-8"))


async def _run_login(config: Config) -> int:
    async with httpx.AsyncClient() as http:
        return await login(config, http, KeyringTokenStore())


def _run_tui(config: Config, character_id: int) -> None:
    resources = _build_resources(config)

    async def refresh_fn() -> AdvisorReport:
        return await refresh(
            resources.client, resources.authenticator, config, character_id, resources.name_cache
        )

    EveTraderApp(refresh_fn, config.refresh_interval_seconds).run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="evetrader")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "login"])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    config = load_config(args.config or default_config_path())

    if args.command == "login":
        character_id = asyncio.run(_run_login(config))
        _save_character_id(character_id)
        print(f"Logged in as character {character_id}; refresh token stored in the keyring.")
    else:
        _run_tui(config, _load_character_id())
