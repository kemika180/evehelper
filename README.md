# evetrader

A terminal (TUI) money-making advisor for EVE Online. It reads your character
state and live market data from ESI (the official EVE REST API), analyses trading
opportunities, and tells you what to do while you play. It **never** automates
transactions — EVE forbids that — it only advises; you execute the trades in-game.

Pure Python.

## Status

Working TUI. It reads your character and live market data and shows: net worth (assets
valued at Jita reference prices), a crafting **build-vs-buy** analysis with quick-train
skill tips, an asset browser (into containers and ships), your full skill list and training
queue, and your open orders flagged best-price or undercut. Multi-character. It never
touches the game economy — you execute every trade in-game.

## How it works

```
ESI + SDE                       pure analysis core        you
─────────                       ──────────────────        ───
wallet / assets / orders   ┐                          ┌─ "build X yourself: mats
skills / location / ship   │                          │   cost C vs buy at P — and
public market prices       ├─►  analysis (market/) ──►│   train skill S to save more"
blueprint recipes (SDE)    ┘                          └─ (you do it in-game)
```

All network I/O is confined to `esi/` and `data/`; `market/` and `advisor/` are a
pure, deterministic, unit-testable core (enforced by import-linter).

## Setup

Requires [`uv`](https://docs.astral.sh/uv/). That's the only prerequisite — there's
**nothing to register**. evetrader ships with a shared ESI application, so it runs with
no config file at all:

```
uv sync
uv run evetrader
```

On first launch, press **`a`** to add your character: your browser opens EVE's official
SSO, you log in with your own EVE account and authorize the read-only scopes, and the
token is stored in your OS keyring (never in the repo). Then select the character to open
the advisor.

Configuration is optional. To customise, create `~/.config/evetrader/config.toml` — every
field has a default, so include only what you want to change:

```toml
refresh_interval_seconds = 60   # how often the advisor re-runs (default 300)
theme = "nord"                  # any built-in Textual theme (default "kemika-purple")
# esi_client_id = "..."         # only if self-hosting your own ESI app registration
# contact = "you@example.com"   # ESI User-Agent contact (URL or email); only for self-hosting
```

For the crafting build-vs-buy analysis, download the EVE SDE (static data — blueprint
recipes; ~250 MB, one-time, re-run to update after a patch):

```
uv run evetrader sde
```

It's fetched from the [Fuzzwork](https://www.fuzzwork.co.uk/dump/) SQLite mirror into
your local data dir (gitignored, never committed). The rest of the app works without
it; only the build-vs-buy view needs it. You can also grab it from the launch prompt.

## Checks

All four must pass before any change is considered complete:

```
uv run pytest tests/python -v
uv run mypy --strict python/
uv run ruff check python/
uv run lint-imports          # pure-core import boundary
```
