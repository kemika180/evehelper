# skillplan

A standalone **Textual TUI** that builds EVE Online skill-training plans. Add
prioritised goals — a ship's *mastery tier* (e.g. Raptor Mastery V) or an
individual *skill* — and it produces a valid training order: every prerequisite
trained before the skill that needs it, higher-priority goals scheduled earlier,
ties broken by shortest training time. It estimates training time from your
character attributes and recommends an optimal attribute remap.

Independent of the main `evetrader` app: it imports nothing from that package and
makes no network calls. Its only runtime inputs are two small bundled JSON files.

## Run

```sh
uv run python tools/skillplan/tui.py
```

- **Add goal**: pick a goal type —
  *Ship mastery* / *Skill* (type the name), *Core skills (Magic 14)* (all fourteen core
  support skills at the chosen level), or *AIR / career plan* (choose one of the game's
  built-in AIR/career plans from the dropdown). The inputs shown adapt to the type. Goals
  stack in a priority list you can reorder (**Up**/**Down**) or **Remove**.
- **Paste a skill list**: paste a block of `Skill Name <level>` lines (1-5 or Roman, `#`
  comments and blanks ignored) into the text box and press **Add pasted list** — the whole
  block becomes one goal. Bad lines are reported instead of being added.
- **Attributes**: edit perception/willpower/intelligence/memory/charisma and an
  optional flat implant bonus. The plan's times update as you type.
- **Recommend remap** (`Ctrl+R`): shows the best legal remap for the current plan and
  how much time it saves. It does **not** change the plan until you press **Apply remap**.
- **Sort**: choose how the plan is ordered —
  *Goal priority, then shortest* (default), *Shortest time (ignore goals)* — pure quick-wins
  order with goals only breaking ties, *As entered / pasted* (keep the order you added skills,
  with prerequisites slotted in just before the skill that needs them), or *Goal priority, then
  longest*. Prerequisites are always respected. Each skill **level** is attributed to the
  highest-priority goal that actually needs it at that level (so if one goal needs Shield
  Management IV and a lower-priority goal pushes it to V, levels I–IV count toward the first
  goal and V toward the second).
- **Export** (`Ctrl+E`): copies the plan as an EVE-importable `Skill Level` block to the
  clipboard.

The plan is split into **one step per skill level** — to reach Gunnery III you train
Gunnery I, then II, then III, each as its own row with its own SP and time — because that's
how training works in game. The table shows each step's level, SP, individual and cumulative
training time, and which goal it serves (prerequisites are marked `(pre)`). The footer totals
SP and time.

## Architecture

- `planner.py` — **pure** core (stdlib only): models, the SP/time maths, prerequisite
  expansion, the schedulers (`SortMode`: optimized / as-entered / longest), and the remap
  optimiser. Deterministic and unit-tested without Textual.
- `tui.py` — the Textual app; all UI, no analysis logic.
- `skills.json` / `masteries.json` — bundled data (see below).

Training-time model: SP to complete level *L* of a rank-*R* skill is
`round(250·R·2^(2.5·(L−1)))`; training rate is `primary + secondary/2` attribute points
per minute. The remap optimiser brute-forces the legal attribute allocations (14 spare
points over 5 attributes, each capped at +10) to minimise the plan's total time.

## Data

Two files ship with the tool, generated from the local EVE SDE dump the main project
downloads (`~/.local/share/evetrader/sde.sqlite`). Regenerate them when the SDE updates:

```sh
python tools/skillplan/build_data.py
python tools/skillplan/build_data.py --sde /path/to/sde.sqlite
```

- `skills.json` (~59 KB) — every skill's name, group, rank, primary/secondary attribute
  and prerequisites.
- `masteries.json` (~126 KB) — ship mastery tiers, the certificate skill sets they resolve
  to, and each hull's fly-requirements.
- `air_plans.json` (~12 KB) — the built-in AIR / career skill plans.

The ~500 MB SDE dump is needed only for skills/masteries. The AIR plans are parsed from the
EVE client's static data (`skillplans.fsdbinary`); `build_data.py` auto-detects a local
install or takes `--eve-client PATH`, and skips them (keeping the bundled file) if no client
is found. Both are build-time only, never touched at runtime.

## Tests

```sh
uv run pytest tools/skillplan/test_planner.py -v
```
