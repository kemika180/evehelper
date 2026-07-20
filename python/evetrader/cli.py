"""The `evetrader` entry point: `evetrader login` then `evetrader`. Composition root.

Builds the I/O resources (shared across characters so the client cache is reused)
and wires the pipeline, login, and character store into the TUI. Not unit-tested —
it is the live-only glue; the pieces it composes are tested.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from evetrader.config import Config
from evetrader.configload import default_config_path, default_data_dir, load_config
from evetrader.data.universe import NameCache
from evetrader.esi.auth import (
    Authenticator,
    CharacterIdentity,
    KeyringTokenStore,
    character_identity,
    login,
)
from evetrader.esi.client import EsiClient
from evetrader.pipeline import AdvisorReport, refresh
from evetrader.session import CharacterRecord, CharacterStore
from evetrader.tui.app import EveTraderApp, RefreshFn


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


def _characters_path() -> Path:
    return default_data_dir() / "characters.json"


async def _run_login(config: Config) -> CharacterIdentity:
    async with httpx.AsyncClient() as http:
        return await login(config, http, KeyringTokenStore())


async def _migrate_legacy_character(config: Config, store: CharacterStore) -> None:
    """Import a pre-multi-character `character_id` file into the store, resolving the
    name from the stored token (no re-login needed)."""
    legacy = default_data_dir() / "character_id"
    if store.records() or not legacy.exists():
        return
    character_id = int(legacy.read_text(encoding="utf-8"))
    async with httpx.AsyncClient() as http:
        authenticator = Authenticator(config, http, KeyringTokenStore())
        token = await authenticator.access_token(character_id)
    store.add(CharacterRecord(character_id, character_identity(token).name))


def _run_tui(config: Config) -> None:
    store = CharacterStore(_characters_path())
    asyncio.run(_migrate_legacy_character(config, store))
    resources = _build_resources(config)

    def make_refresh_fn(character_id: int) -> RefreshFn:
        async def refresh_fn() -> AdvisorReport:
            return await refresh(
                resources.client,
                resources.authenticator,
                config,
                character_id,
                resources.name_cache,
            )

        return refresh_fn

    async def login_fn() -> CharacterIdentity:
        return await login(config, resources.http, KeyringTokenStore())

    def remove_token_fn(character_id: int) -> None:
        with contextlib.suppress(Exception):
            KeyringTokenStore().delete(character_id)

    EveTraderApp(
        store, make_refresh_fn, login_fn, remove_token_fn, config.refresh_interval_seconds
    ).run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="evetrader")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "login"])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    config = load_config(args.config or default_config_path())

    if args.command == "login":
        identity = asyncio.run(_run_login(config))
        CharacterStore(_characters_path()).add(
            CharacterRecord(identity.character_id, identity.name)
        )
        print(f"Logged in as {identity.name} ({identity.character_id}).")
    else:
        _run_tui(config)
