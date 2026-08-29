"""The `evehelper` entry point: `evehelper login` then `evehelper`. Composition root.

Builds the I/O resources (shared across characters so the client cache is reused)
and wires the pipeline, login, and character store into the TUI. Not unit-tested —
it is the live-only glue; the pieces it composes are tested.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evehelper.config import Config
from evehelper.configload import (
    default_config_path,
    default_data_dir,
    load_config,
    migrate_legacy_dirs,
)
from evehelper.data.access import LastAccessStore
from evehelper.data.sde import SdeDatabase, SdeError
from evehelper.data.sde_download import (
    ProgressFn,
    SdeState,
    check_sde_freshness,
    download_sde,
)
from evehelper.data.structures import StructureCache
from evehelper.data.universe import NameCache
from evehelper.data.wealth import WealthSample, WealthStore
from evehelper.esi.auth import (
    Authenticator,
    CharacterIdentity,
    KeyringTokenStore,
    character_identity,
    login,
    migrate_legacy_tokens,
)
from evehelper.esi.client import EsiClient
from evehelper.pipeline import (
    CharacterReport,
    MarketReport,
    fetch_character,
    fetch_market_report,
)
from evehelper.session import CharacterRecord, CharacterStore
from evehelper.tui.app import EveHelperApp, RefreshFeed


@dataclass
class _Resources:
    http: httpx.AsyncClient
    client: EsiClient
    authenticator: Authenticator
    name_cache: NameCache
    sde: SdeDatabase | None


def _load_sde() -> SdeDatabase | None:
    """The local SDE if it's been downloaded, else None — build-vs-buy is optional and
    the rest of the app runs without it."""
    try:
        return SdeDatabase(sde_path())
    except SdeError:
        return None


def _build_resources(config: Config) -> _Resources:
    http = httpx.AsyncClient()
    client = EsiClient(config, http, cache_path=default_data_dir() / "esi_cache.pickle")
    return _Resources(
        http=http,
        client=client,
        authenticator=Authenticator(config, http, KeyringTokenStore()),
        name_cache=NameCache(default_data_dir() / "names.json", client),
        sde=_load_sde(),
    )


def _filename_slug(name: str) -> str:
    """A filesystem-safe slug of a character name for the exported TSV filename."""
    slug = "".join(char if char.isalnum() else "_" for char in name).strip("_")
    return slug or "character"


def _characters_path() -> Path:
    return default_data_dir() / "characters.json"


def sde_path() -> Path:
    return default_data_dir() / "sde.sqlite"


def _print_sde_progress(downloaded: int, total: int | None) -> None:
    """Redraw a one-line download progress readout for the `sde` command."""
    mb = downloaded / 1_000_000
    if total:
        pct, total_mb = downloaded / total, total / 1_000_000
        sys.stdout.write(f"\r  {pct:5.1%}  ({mb:,.0f} / {total_mb:,.0f} MB)")
    else:
        sys.stdout.write(f"\r  {mb:,.0f} MB")
    sys.stdout.flush()


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
    # Carry pre-rename SSO tokens over for characters already set up as "evetrader".
    migrate_legacy_tokens(record.character_id for record in store.records())
    asyncio.run(_migrate_legacy_character(config, store))
    resources = _build_resources(config)
    last_access = LastAccessStore(default_data_dir() / "last_access.json")
    wealth = WealthStore(default_data_dir() / "wealth_history.json")

    def make_feed(character_id: int) -> RefreshFeed:
        # Per-character: structure access (and its negative cache) is character-scoped.
        structure_cache = StructureCache(resources.client)
        # Record this session's access once when the character is opened; the prior value
        # is the cutoff for the Overview "since your last visit" feed, held constant across
        # this session's refreshes (each refresh re-uses it, not an ever-advancing "now").
        since = last_access.record_access(character_id, datetime.now(UTC))

        async def character() -> CharacterReport:
            return await fetch_character(
                resources.client,
                resources.authenticator,
                config,
                character_id,
                resources.name_cache,
                structure_cache,
                resources.sde,
                since=since,
            )

        async def market(report: CharacterReport) -> MarketReport:
            return await fetch_market_report(
                resources.client, config, report, resources.name_cache, resources.sde
            )

        def record_wealth(sample: WealthSample) -> None:
            wealth.record(character_id, sample)

        def wealth_history() -> list[WealthSample]:
            return wealth.history(character_id)

        def export_wealth() -> Path:
            # Land the TSV in the working directory so it's easy to find and open.
            name = next(
                (r.name for r in store.records() if r.character_id == character_id),
                str(character_id),
            )
            dest = Path.cwd() / f"wealth_{_filename_slug(name)}.tsv"
            wealth.export_tsv(character_id, dest)
            return dest

        return RefreshFeed(
            character=character,
            market=market,
            record_wealth=record_wealth,
            wealth_history=wealth_history,
            export_wealth=export_wealth,
        )

    async def login_fn() -> CharacterIdentity:
        return await login(config, resources.http, KeyringTokenStore())

    def remove_token_fn(character_id: int) -> None:
        with contextlib.suppress(Exception):
            KeyringTokenStore().delete(character_id)

    async def download_sde_fn(on_progress: ProgressFn) -> bool:
        # Blocking download off the event loop; then reload so the next scan sees it.
        # Returns False on any failure so the launch prompt can report it, not crash.
        try:
            await asyncio.to_thread(
                download_sde, sde_path(), contact=config.contact, on_progress=on_progress
            )
        except Exception:  # network/IO failure is surfaced in the UI, not fatal
            return False
        resources.sde = _load_sde()
        return resources.sde is not None

    async def sde_check_fn() -> SdeState:
        # One cheap HEAD to see if the local SDE is missing or a newer dump exists.
        return await asyncio.to_thread(check_sde_freshness, sde_path(), contact=config.contact)

    try:
        EveHelperApp(
            store,
            make_feed,
            login_fn,
            remove_token_fn,
            config.refresh_interval_seconds,
            theme=config.theme,
            download_sde_fn=download_sde_fn,
            sde_check_fn=sde_check_fn,
        ).run()
    finally:
        # Persist the ESI response cache so the next launch reuses still-fresh data.
        resources.client.save_cache()


def main() -> None:
    parser = argparse.ArgumentParser(prog="evehelper")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "login", "sde"])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    # Rename fallout: move any pre-rename evetrader config/data into place before anything
    # reads them, so an existing install keeps its characters and settings.
    migrate_legacy_dirs()

    # A missing DEFAULT config is fine — the app runs on built-in defaults (shared client
    # id). But an explicitly requested --config that doesn't exist is almost certainly a
    # typo, so fail loudly rather than silently ignoring it.
    if args.config is not None and not args.config.exists():
        parser.error(f"config file not found: {args.config}")
    config = load_config(args.config or default_config_path())

    if args.command == "login":
        identity = asyncio.run(_run_login(config))
        CharacterStore(_characters_path()).add(
            CharacterRecord(identity.character_id, identity.name)
        )
        print(f"Logged in as {identity.name} ({identity.character_id}).")
    elif args.command == "sde":
        dest = sde_path()
        print(f"Downloading the EVE SDE to {dest} … (~250 MB, one-time; re-run to update)")
        download_sde(dest, contact=config.contact, on_progress=_print_sde_progress)
        print("\nSDE ready.")
    else:
        _run_tui(config)
