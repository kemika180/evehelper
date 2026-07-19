# Eve trading advisor — architecture spec

## Goal
A terminal (TUI) advisor for EVE Online that you run while you play. It reads your
character state and live market data from ESI (EVE Swagger Interface, the official
EVE REST API), analyses money-making opportunities, and tells you *what to do* —
which items to buy/sell, at what price, for what expected profit. EVE forbids
automating the actual transactions through the API, so this tool never acts; it
only advises, and you execute the trades in-game.

Pure Python. No Rust/PyO3 (unlike the sibling `quant_system` project) — the data
volumes are well within polars/numpy's reach.

## Constraints
- Strictly typed: `mypy --strict`, no `Any`, no untyped dicts crossing module
  boundaries. All ESI payloads and config are pydantic models.
- All linters pass before commit: `ruff check`, `mypy --strict`.
- **Analysis core is pure.** The `market/` and `advisor/` layers take a market
  snapshot + character state + config and return ranked suggestions. No network,
  no wall-clock reads, no token access. Deterministic given their inputs — so the
  scoring logic is unit-testable with hand-built fixtures and no live API.
- **All ESI I/O is confined to `esi/` and `data/`.** Nothing else touches the
  network. This is the pure-core boundary, and it is non-negotiable in the same
  way `quant_system`'s Rust-core purity is. It is enforced mechanically by
  import-linter (a `forbidden` contract in `pyproject.toml`): `market/` and
  `advisor/` may not import `esi/`, `data/`, or `httpx`. Run `uv run lint-imports`.
- **Respect ESI's rules** (this is also a ToS matter, not just politeness):
  - Honour cache headers — every response carries `Expires` and usually an
    `ETag`. Never re-fetch before `Expires`; use conditional (`If-None-Match`)
    requests. Assets cache ~1h, market orders ~5min, most reference data much
    longer.
  - Honour the error-limit budget (`X-Esi-Error-Limit-Remain` / `-Reset`
    headers): back off before it hits zero.
  - Set a descriptive `User-Agent` with contact info, per ESI guidelines.
  - Never issue an ESI call in response to a keystroke. Refresh is driven by
    cache expiry, not by the UI.
- No automation of transactions. The tool advises; the human executes. There is
  no write path to the game.

## Repo layout
```
eve_trading/
├── pyproject.toml
├── .ruff.toml
├── mypy.ini
├── README.md
├── python/
│   └── evetrader/
│       ├── __init__.py
│       ├── config.py          # pydantic settings: character(s), home region/station,
│       │                      #   trade skills, capital limits, risk prefs
│       ├── esi/               # ALL network I/O lives here
│       │   ├── auth.py        # OAuth2 SSO (PKCE, native-app flow); token store + refresh
│       │   ├── client.py      # async HTTP; ETag/Expires cache; error-limit backoff; paging
│       │   └── models.py      # pydantic models for ESI payloads (the I/O boundary)
│       ├── data/              # ingestion: ESI/SDE -> normalized polars frames
│       │   ├── market.py      # market orders + history -> polars
│       │   └── universe.py    # type/region/station/system reference data
│       ├── market/            # PURE analysis core (no I/O)
│       │   ├── fees.py        # broker fee + sales tax from skills/standings
│       │   ├── station_trading.py  # margins, competition/undercut, ISK/hr scoring
│       │   └── hauling.py     # (milestone 6) cross-region arbitrage
│       ├── advisor/           # PURE: rank opportunities against character state
│       │   ├── source.py      # OpportunitySource Protocol + Opportunity model
│       │   └── engine.py      # gather from sources, rank under capital/slot/risk limits
│       └── tui/
│           ├── app.py         # Textual app; refresh loop driven by cache expiry
│           └── widgets.py
├── tests/
│   └── python/
└── data/                      # local cache: SDE, token store (gitignored)
```

## Internal boundary contract (I/O layer <-> analysis core)
The parallel to `quant_system`'s PyO3 boundary. One direction of data flow:

```
esi/ + data/   -->   MarketSnapshot + CharacterState   -->   advisor/engine.rank()
(impure, cached)     (pydantic / polars, plain data)         (pure) -> list[Opportunity]
```

- `MarketSnapshot`: normalized polars frames (orders, daily history) + the region
  and timestamp they were captured at. Plain data, no client handle.
- `CharacterState`: wallet balance, assets, open orders, relevant skill levels,
  current location. Pydantic. Built in `esi/`, consumed read-only by the core.
- `Opportunity`: a single suggestion — action (place buy order / place sell order /
  haul), type, station(s), quantity, price, capital required, expected profit,
  expected ISK/hr, confidence, and the reasoning. Pydantic.
- Extensibility point (the analogue of `quant_system`'s `StrategyProtocol`):
  implement the `OpportunitySource` **Protocol** in `advisor/source.py`; don't
  subclass a base. `station_trading` and `hauling` are each a source. The advisor
  engine consumes any list of sources.

## Money-making verticals
v1 focuses on **station trading**; **hauling** is a later milestone on the same core.
- **Station trading**: place buy orders below market, sell orders above, profit on
  the spread at one station. Needs: best-buy/best-sell per type, fee/tax-adjusted
  margin, competition & 0.01-ISK undercut pressure, daily volume (velocity) from
  market history for ISK/hr, and your capital + order-slot limits.
- **Hauling / regional arbitrage** (milestone 6): item cheaper in region A than it
  sells for in region B; suggest buy/haul/sell accounting for cargo volume, route
  jumps, and price impact.

## Milestones
1. Scaffold: `pyproject.toml`, package skeleton, `Config` pydantic model, an
   `evetrader` entry point that launches an empty Textual app. `pytest` +
   `mypy --strict` + `ruff` all green.
2. ESI client + auth: OAuth2 SSO (PKCE) login, token store + auto-refresh, async
   client with ETag/Expires caching, error-limit backoff, and paging. Fetch
   wallet/assets/open-orders/location + public market orders for one region.
   Pydantic models at the boundary.
3. Data + reference layer: normalize orders/history into polars; universe
   reference (type/region/station names) — resolve SDE-vs-ESI (see open
   decisions). Fee/tax computation from skills + standings.
4. Station-trading engine (pure core): per-type fee-adjusted margins, competition/
   undercut detection, ISK/hr from history, ranked by expected profit under
   capital and order-slot constraints. Unit-tested on fixtures, no live API.
5. Advisor + TUI: `OpportunitySource` Protocol wired; advisor ranks against live
   `CharacterState`; Textual TUI lists suggestions, refreshes on cache expiry,
   lets you mark an action taken. First end-to-end usable version.
6. Hauling / regional arbitrage: second `OpportunitySource` — cross-region diffs
   with cargo-volume and route constraints.
7. (later) Fresher state + feedback: in-game clipboard ingestion (paste EVE's
   copied inventory/market exports for instant state, bypassing asset cache lag);
   persist past suggestions and realized P&L to tune scoring.

## Open decisions to resolve in Claude Code
- **Universe reference source**: the static SDE (large static export, fast local
  lookups, periodically stale) vs live ESI `/universe/*` endpoints (always current,
  slower, more calls). Likely SDE for names/volumes, ESI for prices.
- **Token storage**: OS keyring vs an encrypted local file. Whichever, it is
  gitignored and never logged.
- **Advisor state persistence**: sqlite vs polars/parquet for suggestion history
  and realized P&L.
- **Multi-character / multi-account** handling in `Config` and the TUI (defer past
  v1 unless trivial).
- **ESI app registration**: scopes to request, callback URL for the native SSO
  flow. Minimize requested scopes to what each milestone needs.
