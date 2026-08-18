# evetrader

A terminal (TUI) companion for EVE Online: a **read-only dashboard** for one or more
of your characters — net worth, assets, skills, industry jobs, and your live market
orders — plus a crafting **build-vs-buy** advisor. It reads everything from ESI (the
official EVE API) and the static data export, and shows you where you stand. It
**never** writes to the game or automates anything (EVE forbids that) — you act
in-game; evetrader only informs.

Pure Python.

## What it shows

Multi-character — a picker on launch, then a per-character view with these tabs:

- **Overview** — net worth (your assets valued at Jita reference prices), wallet, skill
  points, free order/industry slots, and an activity digest: trades since your last
  visit, industry jobs ready to deliver, and skills that finished training.
- **Trading** — your own open buy/sell orders, each flagged best-price or undercut. (It
  does *not* scan the market to suggest new trades — it tracks the orders you've placed.)
- **Crafting** — for a blueprint you own, a build-vs-buy analysis: the full self-source
  production tree and its shopping/mining plan, priced against Jita, with quick-train
  skill tips that would lower the build cost. Needs the SDE (below).
- **Industry** — your running and ready-to-deliver industry jobs.
- **Skills** / **Skill Queue** — your full skill list (with level pips) and the training
  queue with live SP progress; select any skill for details.
- **Assets** — a searchable browser of everything you own, expandable into containers
  and ships.

## How it works

```
ESI + SDE (read-only)               evetrader (TUI)             you
─────────────────────               ───────────────            ───
wallet · assets · orders        ┌─ dashboard: net worth,   ┌─ decide what to
skills · industry · location ─► │  assets, skills, jobs,    │  train / build / list,
prices · blueprint recipes      └─ your live orders  +      │  then act in-game
                                   crafting build-vs-buy  ──┘
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
its dashboard.

Configuration is optional. To customise, create `~/.config/evetrader/config.toml` — every
field has a default, so include only what you want to change:

```toml
refresh_interval_seconds = 60   # how often it re-reads ESI (default 300)
theme = "nord"                  # any built-in Textual theme (default "kemika-purple")
# esi_client_id = "..."         # only if self-hosting your own ESI app registration
# contact = "you@example.com"   # ESI User-Agent contact (URL or email); only for self-hosting
```

The **Crafting** tab needs the EVE SDE (static data — blueprint recipes; ~250 MB). You
don't have to do anything: evetrader offers to download it on launch (with a progress
bar) whenever it's missing or a newer version has been published — just accept the
prompt. It's pulled from the [Fuzzwork](https://www.fuzzwork.co.uk/dump/) SQLite mirror
into your local data dir (gitignored, never committed). The rest of the app works
without it; only build-vs-buy needs it.
