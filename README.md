# evetrader

A terminal (TUI) money-making advisor for EVE Online. It reads your character
state and live market data from ESI (the official EVE REST API), analyses trading
opportunities, and tells you what to do while you play. It **never** automates
transactions — EVE forbids that — it only advises; you execute the trades in-game.

Pure Python. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design and
milestones, and [`WORKFLOW.md`](WORKFLOW.md) for the day-to-day dev process.

## Status

Pre-milestone-1: planning docs only. Milestone 1 is the workspace scaffold — a
`Config` pydantic model and an `evetrader` entry point that launches an empty
Textual app, with all three checks green.

## How it works

```
ESI (REST API)                pure analysis core              you
─────────────                 ──────────────────             ───
assets / wallet / orders  ┐                              ┌─ "place a buy order for
location / skills         ├─► CharacterState ─┐          │   X at price P — expected
                          │                   ├─► advisor ┤   margin M, ~ISK/hr H"
public market orders +    ┘   MarketSnapshot ─┘   engine  └─ (you do it in-game)
history (per region)
```

All network I/O is confined to `esi/` and `data/`; `market/` and `advisor/` are a
pure, deterministic, unit-testable core.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/). You'll also need to register an
application at <https://developers.eveonline.com> to get an SSO client id and the
scopes for reading your character (assets, wallet, orders, location, skills).

```
uv sync
uv run evetrader
```

## Checks

All four must pass before any change is considered complete:

```
uv run pytest tests/python -v
uv run mypy --strict python/
uv run ruff check python/
uv run lint-imports          # pure-core import boundary
```
