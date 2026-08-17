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
    longer. The response cache is **persisted between sessions** (`esi/client.py`,
    pickled to `<data dir>/esi_cache.pickle`, loaded on construct, saved on exit):
    a relaunch reuses entries still fresh per their `Expires` (no fetch) and
    revalidates the rest with `If-None-Match` (cheap 304, no re-download), so a
    quick restart doesn't re-pull everything. A corrupt/old cache degrades to a
    cold start, never a crash.
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
│       │                      #   fee rates, trading item list (pure data — no I/O)
│       ├── configload.py      # load Config from TOML (impure; kept out of the core)
│       ├── cli.py             # `evetrader` entry point: `login` + run the TUI
│       ├── session.py         # persistent set of logged-in characters (id + name)
│       ├── pipeline.py        # composition root: fetch -> build inputs -> run advisor
│       ├── esi/               # ALL network I/O lives here
│       │   ├── auth.py        # OAuth2 SSO (PKCE, native-app flow); token store + refresh
│       │   ├── client.py      # async HTTP; ETag/Expires cache (persisted between sessions); error-limit backoff; paging
│       │   ├── endpoints.py   # typed per-endpoint fetches (bytes in -> pydantic out)
│       │   └── models.py      # pydantic models for ESI payloads (the I/O boundary)
│       ├── data/              # ingestion: ESI/SDE -> normalized plain data
│       │   ├── market.py      # market orders + history -> polars MarketSnapshot
│       │   ├── character.py   # wallet/skills/standings/orders -> CharacterState
│       │   ├── universe.py    # type/region/station names (cached POST /universe/names/)
│       │   ├── skills.py      # bundled static skills reference (skills.json, offline)
│       │   ├── assets.py      # rebuild the nested asset tree from ESI's flat list (pure)
│       │   └── structures.py  # resolve player-structure names (negative-cached 403s)
│       ├── market/            # PURE analysis core (no I/O)
│       │   ├── snapshot.py    # MarketSnapshot: polars frames + region + capture time
│       │   ├── fees.py        # broker fee + sales tax from skills/standings
│       │   ├── station_trading.py  # margins, competition/undercut, ISK/hr scoring
│       │   ├── listings.py    # active-listings overlay: is each own order still best?
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
- ~~**Active-listings overlay**~~ — DONE. Two overlay tables on the Advisor tab —
  "YOUR BUY ORDERS" under the BUY signals, "YOUR SELL ORDERS" under the SELL signals —
  list the character's own open market orders (`GET /characters/{id}/orders/`, already
  fetched for the order-slot count and previously discarded), each coloured green if it
  still leads its market (✓ best) or red if beaten (✗ undercut on a sell / overcut on a
  buy), showing your price vs the best competing price. Tracks orders in **all regions**:
  the home-region book is reused, and each order in another region prices its competition
  with a cheap per-`type_id` fetch there (not a whole foreign book), so the error-limit
  posture is respected. The best/beaten decision is pure — `market/listings.py`
  (`classify_listings` over the book + a plain `OwnOrder` list; competition excludes the
  character's own order ids so two of their orders don't beat each other), fed by the
  pipeline converting the ESI `CharacterOrder` payloads to plain data at the boundary.
  Beaten orders sort to the top (they need re-pricing). Each order's station is named via
  the same resolvable/structure-cache path as asset places.
- ~~**Skill-info popup**~~ — DONE. Select a skill-queue row to see its level, SP
  progress (a bar, interpolated by time for the training skill), timing, and static
  facts — rank, primary/secondary attribute, description — from a bundled skills
  reference (`evetrader/data/skills.json`), read offline with no ESI call.
- ~~**Full skill view**~~ — DONE. A "Skills" tab with a collapsible tree of all
  trained skills grouped by category (level shown as pips); selecting a skill opens
  the same detail popup — the live training detail if it's in the queue, otherwise
  its trained level and static facts. Level pips mirror the in-game squares — a cyan ■
  per trained level, a dim □ per untrained level — and levels queued for training are
  highlighted magenta (a ◪ for the one part-trained now, a □ for levels queued but not
  yet started), so a skill not in the queue reads plainly while a queued one lights up
  exactly the levels it will train.
- ~~**Downtrend guard**~~ — DONE. `market/investment.py` suppresses a BUY when the
  short-window average (last `trend_days`) sits more than `max_downtrend` below the
  full-window fair value: a sharp dip barely moves the short average, a sustained
  slide drags it well below, so only revertible dips pass. Tunable in
  `InvestmentParams` (`trend_days=7`, `max_downtrend=0.10`).

**Modules — crafting/PI before hauling**
- **Crafting / industry** tracking & assistance.
  - ~~Jobs tracker~~ — DONE (increment 1). An "Industry" tab lists running/ready jobs
    (`GET /characters/{id}/industry/jobs/`, `esi-industry.read_character_jobs.v1`):
    Activity / Item / Runs / Time left / Where, ready-to-deliver rows sorted to the top
    and coloured by state; selecting a row opens `IndustryJobScreen` (timing, facility,
    cost, invention success chance). Item names the product for manufacturing/invention/
    reactions, the blueprint for research/copying. Facilities are named via the same
    resolvable/structure-cache path as asset places. SDE-free.
  - ~~Build-vs-buy engine~~ — DONE (increment 2). A "Manufacturing" tab ranks owned
    blueprints by margin, and the blueprint popup shows a per-run build-vs-buy block;
    both priced against **Jita** (`config.reference_market`, The Forge / Jita 4-4),
    independent of where the character is docked. The tab is searchable (by product)
    and sortable (click a column header, click again to reverse), collapses duplicate
    copies of a blueprint at the same ME into one row showing the count, and offers an
    in-app SDE download button (hidden once installed; `evetrader sde` also works). It
    explains an empty state (missing SDE / no blueprints) rather than blanking. The pure `market/production.py`
    engine (`analyze_build` → `BuildAnalysis`, ME-adjusted material cost vs fee-adjusted
    sale value) is a **shared, generic component** — `Recipe` is activity-agnostic, so
    reactions and PI feed their own recipes through the same engine. It brought the SDE
    forward: `evetrader sde` downloads the Fuzzwork SQLite (gitignored), `data/sde.py`
    reads a blueprint's bill of materials, and the pipeline prices materials/product
    from the reference region's sell orders (`data/market.best_ask_prices`) — no new
    keystroke ESI calls, computed in the market phase. Pricing is **hybrid**: a product
    the market prices shows margin + BUILD/BUY; one it can't (capitals sell by contract,
    never on a hub, and ESI exposes no alliance-contract prices) still lists with its
    build cost, marked unvalued rather than dropped. Selecting a row opens
    `MaterialsScreen` — the bill of materials (each input's ME-adjusted quantity, unit
    Jita price, line cost). Reactions and invention are future increments on the same
    engine (invention needs a probability/expected-value extension). Documented
    simplifications: material formula ignores structure/rig bonuses; the character's
    home-station broker fee is used for the (Jita) sale.
  - ~~Craft-cost + self-source-vs-buy reframe~~ — DONE (increment 3). The tab (renamed
    **Crafting**) no longer leads with build-to-sell margin; it answers "what does this
    cost to craft, and is self-sourcing the materials cheaper than buying them?" Columns
    are Buy-mats (every material bought at Jita) · Self-source (each input at the cheaper
    of buying or building it) · Savings, default-sorted by savings; `MaterialsScreen`
    shows per-material buy vs build with the chosen source marked. The engine gained a
    pure, depth-capped, cycle-guarded recursion (`build_unit_cost` over a product→recipe
    map): a material with its own manufacturing blueprint is costed by building it in
    turn, else bought. `data/sde.py.recipe_for_product` is the reverse (product→blueprint)
    lookup, and the pipeline expands each owned blueprint into the transitive closure of
    **owned** sub-component blueprints (`_expand_recipes`, filtered by the character's
    blueprint types) and prices the whole tree from the reference market's asks — still
    no new keystroke ESI calls. A sub-component whose blueprint the character doesn't own
    stays buy-only: self-building it would mean acquiring the blueprint too, a cost this
    doesn't model. `BuildAnalysis` keeps the
    old sell/margin fields (the Assets-tab blueprint popup still shows build-vs-buy) but
    adds `craft_cost`/`savings`. Documented simplifications: sub-builds assume ME 0 (the
    sub-blueprint's researched ME isn't threaded through); reaction/PI intermediates
    aren't treated as buildable yet (only manufacturing, activityID 1).
  - ~~Refining as a third source~~ — DONE (increment 4). A material can now also be
    obtained by mining-and-refining ore, so each bill line is costed at the cheapest of
    **buy / build / refine** and `MaterialLine.source` says which (the `MaterialsScreen`
    gained a Refine column). `data/sde.py.ore_sources` reverse-looks-up the asteroid ores
    (`invTypeMaterials` ⋈ `invTypes.portionSize` ⋈ `invGroups`, category 25 only — modules/
    ships that also reprocess into minerals are excluded) that yield each wanted mineral
    and their per-unit yield; the pipeline prices those ores in the same reference-market
    pass and `_refine_prices` turns them into a per-mineral unit cost. The refine model is
    **naive** (`config.refining.efficiency`, a user-owned reprocessing yield like the fee
    rates): a mineral's refine cost = cheapest ore's ask ÷ (its yield of that mineral ×
    efficiency), **ignoring the other minerals the ore also produces** — chosen (2026-07-26)
    over byproduct-credited/value-weighted allocation for simplicity, so an ore is only
    cheap for the mineral it's densest in and no byproduct prices are needed. Refining
    also feeds the recursive sub-build costing (`build_unit_cost` threads `refine_prices`),
    so a component's mineral inputs can be refined too.
  - ~~Self-source recipe + quick-train tips~~ — DONE (increment 5). The Crafting tab lists owned,
    manufacturable blueprints **alphabetically** (Product · ME, no ISK — costs are in the popup) and
    a row opens a self-source recipe that answers *"how do I make this myself, and is it worth my
    time?"* The mining side carries **no ISK on purpose**: a refined mineral's real cost is time,
    travel and risk, not a market price, and quoting the ore's Jita price ÷ yield was misleading. So
    the popup reads like a good industry calculator, then goes further. Sections: (a) **materials
    required** — the recipe's direct materials with **Jita buy price** and **volume** each, a
    buy-all + total-volume line, and how each is self-sourced (mine / build / buy); (b) **to
    self-source** — the recipe **expands through *every* manufacturable component**, owned blueprint
    or not (`_expand_all_recipes`), so the whole tree is visible at once: the **byproduct-aware mining
    plan** (ore · units · **m³ to haul** · rough **location** highsec→moon) and the sub-components to
    **build**, aggregated by product and ordered dependencies-first, each with its material inputs
    (`BuildStep`/`BuildInput`, one material per line with quantity + volume — not a wall); (c) **skills
    to build this** — the manufacturing skills every blueprint in the tree requires
    (`sde.manufacturing_skills`, deduped to the highest level any needs), each with the character's
    current level and the **time to train** the gap (plus a **total training time**); (d) **blueprints
    needed** — each component whose blueprint the character doesn't own (materials tagged `build*`),
    priced as an **estimated copy-job cost** (EIV × `config.industry.copy_cost_index` × runs) — a proxy
    for the BPC's contract price, since BPCs aren't market-sold and scraping public contracts would
    violate the ESI posture; every priced list carries a total;
    (e) **buy or produce** — items with no manufacturing recipe, each tagged how it could otherwise be
    made: `reaction` (`sde.reaction_products`, activityID 11) or `pi` (`sde.pi_products`,
    `planetSchematicsTypeMap`) — those chains aren't modelled yet, so they're a soft buy, not hard;
    (f) **recommended skills** (reprocessing quick-wins); and (g) **export to Markdown**
    (`e` writes `<product>-recipe.md`). Pure `market/production.py`: `analyze_build` costs only
    buy-vs-sell (the asset browser's blueprint popup still uses its margin); `build_source_tree` →
    cycle-guarded, depth-capped `SourceNode` (build / mine / buy — the ore is *not* chosen here);
    `collect_needs` → `RecipeNeeds`; `plan_ore_mining` → `MineLine`s; and the assembled
    `SelfSourcePlan` (`RequiredMaterial`s, `MineLine`s, `BuildStep`s, `BuyItem`s) is built in the
    pipeline (prices/volumes from `data/market` asks + `sde.volumes`). The **byproduct model**:
    minerals are gathered **rarest-first** (`refining.mineral_commonness`, Tritanium common →
    Morphite rare) — mining a rare mineral's ore also yields the common minerals, so those are
    credited *before* dedicated common-mineral ore is planned (you mine only the shortfall Veldspar
    the other ores' byproducts didn't already cover). `recipe_markdown` is a pure TUI-layer formatter.
    **Refining is skill-based** (for ore *quantity*, not cost): `market/refining.reprocessing_yield`
    multiplies a config **base station rate** by Reprocessing (+3%/lvl), Reprocessing Efficiency
    (+2%/lvl) and the ore-specific processing skill (+2%/lvl, read per-ore from SDE attribute 790).
    **Ore selection** returns base ores only (`sde.ore_sources` excludes compressed/batch/graded
    (`0-Grade`/`II`..`IV-Grade`)/`Isotope`; the pipeline collapses same-family quality prefixes like
    Brimful/Glistening by name suffix) and offers a few options per mineral (`_MAX_REFINE_OPTIONS`),
    **ordered by accessibility**: each ore has a rank+location from its reprocessing skill
    (`ORE_SOURCE_INFO`, Simple→highsec … Abyssal/moon), sorted by distance from the **home-security
    band** (`sde.station_security` → `security_target_rank`) then by least ore to mine — so a highsec
    character sees Veldspar/Scordite/Plagioclase first, a null character its local ores. Moon and
    abyssal ores are shown (tagged), not excluded — the player decides. Sub-builds are **skill-gated**
    (`_expand_recipes` + `sde.manufacturing_skills`), so an owned-but-skill-locked component stays a
    buy. **Quick-train tips** (pure `market/training.py`, ≤ `config.training.quick_horizon_hours`,
    default 3h) are ISK-free too: a reprocessing skill shows *"~X% less ore to mine"* at the **highest
    level reachable within the horizon** (`training.max_level_within` — from scratch that's often
    several levels, e.g. 0→3 in a couple of hours), and a manufacturing skill one level short shows
    *"unlocks building <component>"*. Training time comes from the character's attributes
    (`GET /characters/{id}/attributes/`, existing `esi-skills.read_skills.v1` scope) and the skill's
    rank via EVE's SP curve. Documented simplifications: structure/rig/implant reprocessing bonuses
    aren't modelled (fold into the config base rate); implant training bonuses aren't (attributes
    endpoint omits them), so time estimates run slightly long; sub-builds assume ME 0; the naive
    densest-mineral ore credit from increment 4 carries over. NOTE: `config.refining.efficiency` was
    renamed to `refining.base_rate`, meaning changed from *total effective yield* (~0.70) to the *base
    station rate* skills multiply on (~0.50) — a config migration for existing users.
- **PI (planetary interaction)** tracking & assistance. Self-contained (colony setups,
  extractors); its profitability view consumes the same build-vs-buy engine above.
- **Hauling / regional arbitrage** (Jita ↔ your hub) — **after** crafting/PI, because
  it depends heavily on in-game context: routes, gate/route safety, cargo volume,
  what the alliance is actually bidding for. Must be route- and security-aware so it
  never suggests a dangerous trip. Needs the SDE (cargo volumes) + ESI route data.
- ~~**Asset browser + containers**~~ — DONE. An "Assets" tab with a tree grouped by
  place (station/structure/system), expandable into containers and ships. The nested
  hierarchy is rebuilt from ESI's flat list by the pure `data/assets.py` (an item
  whose `location_id` is another item's `item_id` sits inside it; cycle-safe). A
  ship/container's contents are grouped by compartment (Fit / Cargo / Drone Bay /
  Fleet Hangar / …, derived from each item's `location_flag`); fitted modules read
  "<slot> <item>" (slot name, no number). Containers and ships show their
  player-assigned name (POST `/characters/{id}/assets/names/`) next to their type.
  Places open by default, containers stay closed until opened; an item search
  filters the tree to the path of matches (matching type or assigned name). NPC stations/systems are named via `/universe/names`;
  player structures are named via `GET /universe/structures/{id}` (needs the
  `esi-universe.read_structures.v1` scope + docking access) with a negative cache
  (`data/structures.py`) so an inaccessible one — which 403s, counting against the
  error-limit budget — isn't re-asked each refresh, falling back to its id. Rows are
  tinted by depth for readability (`DepthTree`). Blueprint leaves are tagged BPO/BPC
  and open a detail popup (`BlueprintInfoScreen`) — original vs copy, ME/TE savings,
  and runs remaining — from `GET /characters/{id}/blueprints/`
  (`esi-characters.read_blueprints.v1`), keyed by asset item_id so two copies of one
  type read their own research. Only blueprints have a popup: an ordinary item has
  nothing solid to show without the SDE (volume/group, deferred to hauling) or a
  per-click ESI fetch (which the cache rules forbid), so those rows are inert.

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
- **Trading scope: a fixed set of long-horizon items as a watchlist** (2026-07-26).
  The broad whole-market discovery scan (`liquid_types` + `scan_candidates`) was
  retired: the value engine now runs only over `config.trading_type_ids`, which
  defaults to PLEX, the Skill Extractor, and the Large + Small Skill Injector — the
  items worth holding and trading across patches. Nothing else scanned. `liquid_types`
  and the threshold-only `find_opportunities` are kept (still unit-tested) for a future
  re-widening but are no longer called by the pipeline.
- **Tracked-item watchlist on the Overview tab** (2026-07-26). With only a handful of
  tracked items, the old BUY/SELL signal tables (which showed a row *only* when a price
  crossed a threshold, so mostly sat empty) were replaced by a compact watchlist on the
  **Overview** tab that always lists all tracked items: Item · Now · Trend · Fair ·
  Range · Advice, one row each with a BUY / SELL / HOLD verdict and the item's current
  trend against its window median. The pure `summarize_tracked` → `TrackedStatus`
  (`market/investment.py`) produces one status per tracked type — including the HOLD
  majority and a `has_data`-flagged placeholder for a type with no live quote/history —
  reusing the same channel/threshold logic as `find_opportunities` but never dropping an
  item. Selecting a row still opens its price-history chart. The **Trading** tab now
  holds only the own-order overlays (YOUR BUY/SELL ORDERS).
- **PLEX trades on the global market, not the home station book** (2026-07-26). PLEX
  (type 44992) has *zero* orders on any regional station order book: it trades on EVE's
  special region-wide global PLEX market (region **19000001**). The Forge's regional
  history for PLEX is stale legacy data (reads ~6m while the live global price is ~4.5m),
  so both orders and history must come from 19000001. `config.special_markets` maps such
  type ids → a region; the pipeline (`_special_market_book`) fetches each special type's
  orders + history from that region, **relabels the orders' `location_id` to the home
  station**, and folds them into the book handed to `summarize_tracked` — so the pure,
  station-scoped engine prices PLEX uniformly with everything else and never has to know
  about the global market. Without this PLEX reads "no data". The three skill items
  (extractor/injectors) are ordinary station-traded items and need no override.
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
  `esi-skills.read_skills.v1`, `esi-skills.read_skillqueue.v1`,
  `esi-characters.read_standings.v1`, and `esi-universe.read_structures.v1` (added
  2026-07-21 to name player structures in the asset browser). `esi-characters.read_blueprints.v1`
  is now consumed too — the asset browser's item-detail popup reads blueprint ME/TE and
  runs. `GET /characters/{id}/attributes/` is also consumed now (under the already-granted
  `esi-skills.read_skills.v1` scope, no re-login) — the Crafting quick-train tips estimate
  training time from the character's learning attributes. Also **pre-provisioned** (2026-07-21) so upcoming modules need no further
  re-login, though nothing requests them yet: `esi-industry.read_character_jobs.v1`
  (crafting), `esi-planets.manage_planets.v1` (PI), `esi-industry.read_character_mining.v1`,
  `esi-contracts.read_character_contracts.v1` (hauling), `esi-markets.structure_markets.v1`
  (private-structure order books). The registered app must enable every requested
  scope or SSO rejects the login ("scope not valid"); a scope change needs a re-login
  because a refresh token's scopes are fixed at authorization time.
- **Universe reference source** (milestone 3): live ESI `POST /universe/names/`
  (bulk id→name, up to 1000/call) plus a persistent local name cache in `data/`,
  since names are immutable and the client's Expires-cache does not cover POST. The
  static SDE is deferred to hauling (milestone 6), where item volumes are needed —
  `/universe/names/` returns names only. ESI has no per-day request quota; the only
  budget is the error-limit (errors, not successes), so volume is not a constraint.
- **Bundled skills reference** (2026-07-20): static skill facts (name, group,
  training rank, primary/secondary attribute, description) ship as a checked-in
  `evetrader/data/skills.json` and are read offline — no runtime ESI call — by
  `data/skills.py` (a `@cache`d loader) for the skill-info popup and the full skill
  view (grouped by `group`). Generated once by
  `scripts/build_skills_reference.py` (walks the public ESI universe endpoints:
  skill category → groups → types), regenerated only when CCP adds/rebalances
  skills. This is a small, self-contained slice of static data, distinct from the
  full SDE (item volumes etc.) still deferred to hauling — the popup's "what is this
  skill" need did not justify pulling in the whole SDE or hitting ESI per keystroke.
- **Fee/tax constants** (milestone 3): `market/fees.py` stays pure and takes skill
  levels + standings + a `FeeRates` config block (base rates and per-level/standing
  reductions) as inputs. No CCP rate constants are baked into code; `FeeRates`
  defaults are user-owned and documented as needing confirmation against the live
  game after balance patches.
