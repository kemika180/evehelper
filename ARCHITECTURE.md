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
│       ├── config.py          # pydantic models: home region/station, capital, risk,
│       │                      #   fee rates, watchlist (pure data — no I/O)
│       ├── configload.py      # load Config from TOML (impure; kept out of the core)
│       ├── cli.py             # `evetrader` entry point: `login` + run the TUI
│       ├── session.py         # persistent set of logged-in characters (id + name)
│       ├── pipeline.py        # composition root: fetch -> build inputs -> run advisor
│       ├── esi/               # ALL network I/O lives here
│       │   ├── auth.py        # OAuth2 SSO (PKCE, native-app flow); token store + refresh
│       │   ├── client.py      # async HTTP; ETag/Expires cache; error-limit backoff; paging
│       │   ├── endpoints.py   # typed per-endpoint fetches (bytes in -> pydantic out)
│       │   └── models.py      # pydantic models for ESI payloads (the I/O boundary)
│       ├── data/              # ingestion: ESI/SDE -> normalized plain data
│       │   ├── market.py      # market orders + history -> polars MarketSnapshot
│       │   ├── character.py   # wallet/skills/standings/orders -> CharacterState
│       │   └── universe.py    # type/region/station names (cached POST /universe/names/)
│       ├── market/            # PURE analysis core (no I/O)
│       │   ├── snapshot.py    # MarketSnapshot: polars frames + region + capture time
│       │   ├── fees.py        # broker fee + sales tax from skills/standings
│       │   ├── station_trading.py  # margins, competition/undercut, ISK/hr scoring
│       │   └── hauling.py     # (milestone 6) cross-region arbitrage
│       ├── advisor/           # PURE: rank opportunities against character state
│       │   ├── state.py       # CharacterState (pure hand-off type) + order-slot calc
│       │   ├── source.py      # OpportunitySource Protocol + Opportunity + station source
│       │   └── engine.py      # gather from sources, rank under capital/slot/risk limits
│       └── tui/
│           └── app.py         # Textual app: character picker -> per-character trading screen
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
> Note (2026-07-20): the active strategy is **value investing** (see § Resolved
> decisions and § Roadmap). Station trading below was the original v1 vertical and
> is retired from the pipeline; the description is kept as design history.

Original plan — v1 focuses on **station trading**; **hauling** is a later milestone.
- **Station trading**: place buy orders below market, sell orders above, profit on
  the spread at one station. Needs: best-buy/best-sell per type, fee/tax-adjusted
  margin, competition & 0.01-ISK undercut pressure, daily volume (velocity) from
  market history for ISK/hr, and your capital + order-slot limits.
- **Hauling / regional arbitrage** (milestone 6): item cheaper in region A than it
  sells for in region B; suggest buy/haul/sell accounting for cargo volume, route
  jumps, and price impact.

## Milestones (original plan — shipped, then the strategy pivoted)
Milestones 1–5 landed as written; milestone 4's station-trading engine was then
**retired in favour of value investing** (see § Resolved decisions and § Roadmap).
Kept here as the build history.

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

## Roadmap (planned)
The tool is a set of location-aware money-making **modules**; each character uses
whichever fit their situation (a Forge trader vs a null resident get different
advice). Value investing (`market/investment.py`) is the first module. Planned, in
rough order:

**Near-term (small, high value)**
- **Active-listings overlay** — show the character's own open market orders inside
  the buy/sell lists, coloured green if still best price, red if undercut (sell) /
  overcut (buy).
- **Skill-info popup** — select a skill in the queue (later the full skill list) to
  see its details.
- **Downtrend guard** — distinguish a real dip from a structural decline (compare a
  short vs long average) so buys don't chase items that won't revert.

**Modules — crafting/PI before hauling**
- **Crafting / industry** tracking & assistance, and **PI (planetary interaction)**
  tracking & assistance. Self-contained (blueprints, jobs, PI setups) and don't
  depend on volatile live conditions — so they come first.
- **Hauling / regional arbitrage** (Jita ↔ your hub) — **after** crafting/PI, because
  it depends heavily on in-game context: routes, gate/route safety, cargo volume,
  what the alliance is actually bidding for. Must be route- and security-aware so it
  never suggests a dangerous trip. Needs the SDE (cargo volumes) + ESI route data.
- **Asset browser + containers** — browse assets and look inside containers/ships
  (rebuild the asset tree from ESI's flat list).

**Persistence / data (enables several of the above)**
- **Advisor state persistence** (sqlite vs polars/parquet) — remember past
  suggestions and realized P&L to tune scoring.
- **Self-recorded structure history** — for *private* structures with no public
  history, record observed prices over time to build a channel. (Not needed for a
  public hub like the 4-HWWF Keepstar, which has region history.)

**Deferred / optional**
- "Follow current location" toggle (analyze wherever docked) — decided against for
  hub trading, but available if wanted.
- `esi-markets.structure_markets.v1` scope — only for *private* structures; public
  hubs work through the region endpoints.

## Open decisions
- **Advisor state persistence**: sqlite vs polars/parquet for suggestion history
  and realized P&L (see Roadmap § Persistence).

## Resolved decisions
- **Strategy: value investing, not station trading** (2026-07-20). The active
  analysis is mean-reversion (`market/investment.py`): buy items trading below their
  moving average / near the bottom of their Donchian channel, and flag held items
  trading above it. Uses ESI market history (daily average/high/low/volume). Long
  horizon — no instant turnaround. `market/station_trading.py` is retired from the
  pipeline but kept for a future station-to-station transfer / hauling source; the
  old `advisor/source.py`+`engine.py` (OpportunitySource Protocol / station-trade
  Opportunity) were removed and will be reintroduced when a second source exists.
- **Multi-character**: implemented — the keyring stores a refresh token per
  character id, `session.py` persists the set of logged-in characters (id + name),
  and the TUI opens on a picker (add via SSO login / remove) before the per-
  character trading screen. Per-character view, not aggregated.
- **Token storage** (milestone 2): OS keyring via the `keyring` library — the
  system secret service holds refresh tokens; no secret material in our files. It
  is never logged.
- **ESI app registration** (milestone 2): native application, PKCE, no client
  secret. Callback `http://localhost:8765/callback` (port configurable in `Config`);
  a local loopback server catches the redirect. Scopes requested up front:
  `esi-wallet.read_character_wallet.v1`, `esi-assets.read_assets.v1`,
  `esi-markets.read_character_orders.v1`, `esi-location.read_location.v1`,
  `esi-skills.read_skills.v1`, `esi-characters.read_standings.v1` (skills and
  standings included now so fee/tax computation needs no re-auth).
- **Universe reference source** (milestone 3): live ESI `POST /universe/names/`
  (bulk id→name, up to 1000/call) plus a persistent local name cache in `data/`,
  since names are immutable and the client's Expires-cache does not cover POST. The
  static SDE is deferred to hauling (milestone 6), where item volumes are needed —
  `/universe/names/` returns names only. ESI has no per-day request quota; the only
  budget is the error-limit (errors, not successes), so volume is not a constraint.
- **Fee/tax constants** (milestone 3): `market/fees.py` stays pure and takes skill
  levels + standings + a `FeeRates` config block (base rates and per-level/standing
  reductions) as inputs. No CCP rate constants are baked into code; `FeeRates`
  defaults are user-owned and documented as needing confirmation against the live
  game after balance patches.
